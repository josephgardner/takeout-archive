import argparse
import json
import os
from pathlib import Path
import sys

from .core import (ArchiveError, Rclone, account_state, alerts, config_from_file,
                   digest_file, import_zip, load_json, lock, poll_account, utcnow)


def parser():
    result = argparse.ArgumentParser(description="Headless, additive Google Takeout archives")
    result.add_argument("--config", required=True, help="JSON configuration path")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("poll", help="Copy stable Drive exports and verify/import them")
    sub.add_parser("check", help="Print renewal/freshness reminders for DSM Task Scheduler")
    sub.add_parser("status", help="Print local status without contacting Google")
    sub.add_parser("verify", help="Deep-check retained ZIPs and extracted objects")
    sub.add_parser("test-alert", help="Print a harmless delivery test; exits 1 for DSM email")
    local = sub.add_parser("import", help="Import a local ZIP without changing/deleting the source")
    local.add_argument("zip", type=Path)
    local.add_argument("--account", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        config = config_from_file(args.config)
        state_dir = Path(config["state_dir"])
        with lock(state_dir / "job.lock"):
            now = utcnow()
            if args.command == "test-alert":
                print("TEST ONLY: Takeout Archive notification delivery test.")
                print("No Google files, NAS archives or schedule dates were changed.")
                print("Exit 1 intentionally requests an email from DSM Task Scheduler.")
                return 1
            if args.command == "import":
                if args.account not in {a["name"] for a in config["accounts"]}:
                    raise ArchiveError("Unknown account label")
                if args.zip.is_symlink() or not args.zip.is_file():
                    raise ArchiveError("Choose an existing regular ZIP file")
                key = "local-" + digest_file(args.zip)
                manifest, changed = import_zip(args.zip, config["library_dir"], args.account, key)
                print("%s: %d ZIP members verified; %s. Source preserved." %
                      (args.account, len(manifest["members"]), "imported" if changed else "already archived"))
                print("Local import does not establish Google export freshness or whole-library completeness.")
                return 0
            if args.command == "status":
                output = []
                for account in config["accounts"]:
                    state = load_json(account_state(config, account["name"]), {})
                    output.append({"account": account["name"],
                                   "schedule_expires_on": account["schedule_expires_on"],
                                   "verified_archives": len(state.get("verified_archives", {})),
                                   "latest_verified_archive_export": state.get("latest_verified_export"),
                                   "whole_export_completeness": "not established by this tool"})
                print(json.dumps(output, indent=2))
                return 0
            if args.command == "verify":
                checked = 0
                for account in config["accounts"]:
                    name = account["name"]
                    directory = Path(config["library_dir"]) / name / "manifests"
                    for manifest_path in sorted(directory.glob("*.json")):
                        key = manifest_path.stem
                        if key.startswith("local-"):
                            # Locally imported source paths may move; verify objects independently.
                            manifest = load_json(manifest_path)
                            for entry in manifest["members"]:
                                target = Path(config["library_dir"]) / name / "objects" / entry["object"]
                                if target.is_symlink() or not target.is_file() or digest_file(target) != entry["sha256"]:
                                    raise ArchiveError("Archived object is missing or changed")
                        else:
                            source = Path(config["archive_dir"]) / name / (key + ".zip")
                            if source.is_symlink() or not source.is_file():
                                raise ArchiveError("Retained source ZIP is missing")
                            import_zip(source, config["library_dir"], name, key, deep_verify=True)
                        checked += 1
                if not checked:
                    raise ArchiveError("No archive manifests to verify yet")
                print("%d archive manifests and their objects passed deep verification." % checked)
                print("This does not establish Google library completeness or offsite recoverability.")
                return 0
            reminders = []
            failures = False
            client = Rclone(config.get("rclone_binary", "rclone"))
            for account in config["accounts"]:
                if args.command == "poll":
                    try:
                        imported, deferred = poll_account(config, account, client, now)
                        print("%s: %d newly verified archive(s), %d waiting for metadata stability." %
                              (account["name"], imported, deferred))
                    except (ArchiveError, OSError, ValueError, KeyError, TypeError) as exc:
                        failures = True
                        reason = str(exc) if isinstance(exc, ArchiveError) else "Local data or I/O failure; manual review required"
                        reminders.append(account["name"] + ": " + reason)
                try:
                    reminders.extend(alerts(config, account, now))
                except (ArchiveError, OSError, ValueError, KeyError, TypeError):
                    failures = True
                    reminders.append(account["name"] + ": cannot evaluate renewal/freshness state")
            if reminders:
                print("\nACTION REQUIRED — Takeout Archive")
                for message in reminders:
                    print("- " + message)
                print("Existing NAS copies remain preserved. Renewing dates locally does not renew Google Takeout.")
                return 1
            if args.command == "check":
                print("No renewal or archive-freshness reminders are due.")
            else:
                print("All observed eligible archives processed. Confirm export part counts separately.")
            return 1 if failures else 0
    except (ArchiveError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if isinstance(exc, ArchiveError) else "Configuration or local I/O error"
        print("ACTION REQUIRED — Takeout Archive: " + reason, file=sys.stderr)
        return 1
