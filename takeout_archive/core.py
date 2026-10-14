"""No third-party Python dependencies. Existing archive objects are immutable."""

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager

UTC = dt.timezone.utc
EXPORT_NAME = re.compile(r"takeout-(\d{8}T\d{6}Z)-(\d+(?:-\d+)?)\.zip\Z")
LABEL = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class ArchiveError(Exception):
    """An actionable failure; never include credentials or signed URLs."""


def utcnow():
    return dt.datetime.now(UTC)


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ArchiveError("Timestamp must include a timezone")
    return parsed.astimezone(UTC)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path, default=None):
    try:
        with open(path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as exc:
        raise ArchiveError("Cannot read local state: " + Path(path).name) from exc


def digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextmanager
def lock(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError("Another archive job is already running") from exc
        yield


def config_from_file(path):
    config = load_json(path)
    if not isinstance(config, dict):
        raise ArchiveError("Configuration must be a JSON object")
    for key in ("state_dir", "archive_dir", "library_dir"):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ArchiveError(key + " must be an absolute directory path")
    paths = [Path(config[k]).resolve() for k in ("state_dir", "archive_dir", "library_dir")]
    for i, left in enumerate(paths):
        for right in paths[i + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ArchiveError("State, archive and library directories must be separate")
    accounts = config.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ArchiveError("Configure at least one account")
    labels = set()
    for account in accounts:
        name = account.get("name", "")
        remote = account.get("remote", "")
        if not isinstance(name, str) or not LABEL.fullmatch(name) or name in labels:
            raise ArchiveError("Account names must be unique lowercase labels")
        labels.add(name)
        if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[^\r\n]*", remote):
            raise ArchiveError("Use a named rclone Drive remote, for example family-drive:Takeout")
        remote_path = remote.split(":", 1)[1]
        if ".." in PurePosixPath(remote_path).parts or remote_path.startswith("/"):
            raise ArchiveError("Use a relative path inside the rclone remote")
        try:
            dt.date.fromisoformat(account["schedule_expires_on"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ArchiveError("Each account needs Google's schedule expiry date (YYYY-MM-DD)") from exc
    for key, default in (("settle_hours", 24), ("stale_days", 75), ("reminder_days", 30)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ArchiveError(key + " must be positive")
        config[key] = value
    return config


class Rclone:
    def __init__(self, binary="rclone"):
        self.binary = binary

    def run(self, arguments, operation):
        try:
            process = subprocess.run(
                [self.binary] + arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=86400,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError("rclone " + operation + " could not run") from exc
        if process.returncode:
            auth = any(word in process.stderr.lower() for word in
                       ("invalid_grant", "unauthorized", "token expired", "401", "oauth"))
            if auth:
                raise ArchiveError("Google Drive authorization needs attention; reauthorize this account")
            raise ArchiveError("rclone " + operation + " failed; check Drive access, disk space and connectivity")
        return process.stdout

    def list_exports(self, remote):
        output = self.run(["lsjson", remote, "--recursive", "--files-only", "--hash",
                           "--include", "takeout-*.zip"], "listing")
        try:
            result = json.loads(output)
            if not isinstance(result, list):
                raise ValueError()
            return result
        except ValueError as exc:
            raise ArchiveError("rclone returned invalid export metadata") from exc

    def copy(self, remote, source_path, destination):
        source = remote.rstrip("/") + "/" + source_path
        self.run(["copyto", source, str(destination), "--immutable", "--retries", "3",
                  "--timeout", "5m"], "copy")


def safe_member(info):
    name = info.filename
    path = PurePosixPath(name)
    mode = info.external_attr >> 16
    if (not name or name.startswith("/") or "\\" in name or "\x00" in name or
            ".." in path.parts or re.match(r"^[A-Za-z]:", name) or stat.S_ISLNK(mode)):
        raise ArchiveError("Archive contains an unsafe member path")
    if info.flag_bits & 1:
        raise ArchiveError("Encrypted ZIP members are unsupported")
    if path.parts and path.parts[0] == "Takeout":
        path = PurePosixPath(*path.parts[1:])
    return str(path)


def store_object(stream, directory, suffix):
    """Install a content-addressed file without overwriting any existing object."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=str(directory))
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                output.write(block)
                digest.update(block)
                size += len(block)
            output.flush()
            os.fsync(output.fileno())
        sha = digest.hexdigest()
        target = directory / (sha + suffix)
        os.chmod(temporary, 0o440)
        try:
            os.link(temporary, str(target))
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or digest_file(target) != sha:
                raise ArchiveError("An existing archive object has changed; refusing to overwrite it")
        return target.name, sha, size
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_zip(source, library, account, archive_key, deep_verify=True):
    """Keep every member and its metadata; source ZIP is always retained."""
    source = Path(source)
    library = Path(library) / account
    manifest_dir = library / "manifests"
    manifest_path = manifest_dir / (archive_key + ".json")
    previous = load_json(manifest_path)
    if previous is not None:
        if (source.stat().st_size != previous["archive_bytes"] or
                (deep_verify and previous.get("archive_sha256") != digest_file(source))):
            raise ArchiveError("Previously imported ZIP has changed; refusing to overwrite its manifest")
        # A successful historical import is not proof that its objects still exist.
        for entry in previous["members"]:
            target = library / "objects" / entry["object"]
            if (target.is_symlink() or not target.is_file() or target.stat().st_size != entry["bytes"] or
                    (deep_verify and digest_file(target) != entry["sha256"])):
                raise ArchiveError("Previously archived content is missing or changed")
        return previous, False
    archive_sha = digest_file(source)
    members = []
    try:
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            # Validate all paths before publishing any objects.
            for info in infos:
                safe_member(info)
            for info in infos:
                if info.is_dir():
                    continue
                path = safe_member(info)
                suffix = PurePosixPath(path).suffix.lower()
                if not re.fullmatch(r"\.[a-z0-9]{1,12}", suffix):
                    suffix = ".bin"
                with archive.open(info) as stream:
                    name, sha, size = store_object(stream, library / "objects", suffix)
                if size != info.file_size:
                    raise ArchiveError("Archive member size mismatch")
                members.append({"path": path, "object": name, "sha256": sha, "bytes": size})
    except (zipfile.BadZipFile, RuntimeError, EOFError) as exc:
        raise ArchiveError("ZIP verification failed; source retained for recovery") from exc
    if not members:
        raise ArchiveError("Empty ZIP is not a verified photo export")
    manifest = {"schema_version": 1, "archive_sha256": archive_sha,
                "archive_bytes": source.stat().st_size, "members": members}
    atomic_json(manifest_path, manifest)
    return manifest, True


def account_state(config, name):
    return Path(config["state_dir"]) / (name + ".json")


def poll_account(config, account, client, now):
    name = account["name"]
    state_path = account_state(config, name)
    state = load_json(state_path, {"seen": {}, "verified_archives": {}})
    observations = client.list_exports(account["remote"])
    imported = 0
    deferred = 0
    for item in observations:
        path = item.get("Path", "")
        parts = PurePosixPath(path)
        match = EXPORT_NAME.fullmatch(parts.name)
        if not match:
            continue
        if not path or path.startswith("/") or ".." in parts.parts or "\\" in path:
            raise ArchiveError("Drive export path is unsafe")
        try:
            exported = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ArchiveError("Drive export filename has an invalid date") from exc
        if exported > now + dt.timedelta(days=1):
            raise ArchiveError("Drive export date is in the future; check system time and export metadata")
        size = item.get("Size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ArchiveError("Drive export has no valid size")
        fingerprint = {k: item.get(k) for k in ("ID", "Size", "ModTime", "Hashes")}
        prior = state["seen"].get(path)
        if prior is None or prior["fingerprint"] != fingerprint:
            state["seen"][path] = {"fingerprint": fingerprint, "stable_since": now.isoformat()}
            if path in state["verified_archives"]:
                raise ArchiveError("A previously archived Drive export changed; manual review required")
        observed = state["seen"][path]
        if (now - timestamp(observed["stable_since"])).total_seconds() < config["settle_hours"] * 3600:
            deferred += 1
            continue
        # Hash the path so equal basenames in different Drive folders cannot collide.
        key = parts.name[:-4] + "--" + hashlib.sha256(path.encode()).hexdigest()[:16]
        destination = Path(config["archive_dir"]) / name / (key + ".zip")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        client.copy(account["remote"], path, destination)
        if destination.is_symlink() or destination.stat().st_size != size:
            raise ArchiveError("Downloaded ZIP size mismatch; no success recorded")
        # On initial ingestion, independently match Drive's binary-file checksum.
        # On subsequent polls rclone performs its own comparison; deep verification
        # is available separately so a daily poll does not rehash the whole library.
        if path not in state["verified_archives"]:
            hashes = {k.lower(): v for k, v in (item.get("Hashes") or {}).items()}
            expected = hashes.get("md5")
            if not expected:
                raise ArchiveError("Drive export checksum is unavailable; no verification recorded")
            md5 = hashlib.md5()
            with open(destination, "rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    md5.update(block)
            if md5.hexdigest().lower() != expected.lower():
                raise ArchiveError("Downloaded ZIP does not match Drive checksum")
        _, changed = import_zip(destination, config["library_dir"], name, key, deep_verify=False)
        state["verified_archives"][path] = {"key": key, "verified_at": now.isoformat()}
        if changed:
            imported += 1
        # Reconcile after an interrupted state write as well as a new import.
        # Freshness follows export production time, never repeated polling time.
        old = timestamp(state["latest_verified_export"]) if state.get("latest_verified_export") else None
        if old is None or exported > old:
            state["latest_verified_export"] = exported.isoformat()
        atomic_json(state_path, state)
    atomic_json(state_path, state)
    return imported, deferred


def alerts(config, account, now):
    name = account["name"]
    state = load_json(account_state(config, name), {})
    expiry = dt.date.fromisoformat(account["schedule_expires_on"])
    remaining = (expiry - now.date()).days
    messages = []
    if remaining <= config["reminder_days"]:
        timing = ("expired %d day(s) ago" % -remaining) if remaining < 0 else ("expires in %d day(s)" % remaining)
        messages.append("%s: Google Takeout schedule %s (%s). Renew at https://takeout.google.com/ and update schedule_expires_on after confirming renewal." % (name, timing, expiry))
    latest = state.get("latest_verified_export")
    if latest:
        age = (now - timestamp(latest)).total_seconds() / 86400
        if age > config["stale_days"]:
            messages.append("%s: no newly verified export for %d days. Check Takeout, Drive space, authorization and import status." % (name, age))
    else:
        messages.append("%s: no verified archive yet. Complete the initial export/import and confirm all expected ZIP parts." % name)
    return messages
