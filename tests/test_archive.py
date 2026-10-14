import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from takeout_archive.cli import main
from takeout_archive.core import (ArchiveError, Rclone, UTC, account_state, alerts,
                                 config_from_file, import_zip, load_json, lock,
                                 poll_account, store_object)


class FakeDrive:
    def __init__(self, files):
        self.files = files
        self.deleted = []

    def list_exports(self, remote):
        return [{"Path": name, "Size": path.stat().st_size, "ID": name,
                 "ModTime": "2026-01-01T00:00:00Z",
                 "Hashes": {"MD5": hashlib.md5(path.read_bytes()).hexdigest()}}
                for name, path in self.files.items()]

    def copy(self, remote, source_path, destination):
        source = self.files[source_path]
        if destination.exists():
            if destination.read_bytes() != source.read_bytes():
                raise ArchiveError("Immutable destination differs")
        else:
            shutil.copyfile(source, destination)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {
            "state_dir": str(self.root / "state"),
            "archive_dir": str(self.root / "raw"),
            "library_dir": str(self.root / "library"),
            "settle_hours": 24, "stale_days": 75, "reminder_days": 30,
            "accounts": [{"name": "example", "remote": "example-drive:Takeout",
                          "schedule_expires_on": "2027-01-01"}],
        }
        self.account = self.config["accounts"][0]
        self.now = dt.datetime(2026, 1, 2, tzinfo=UTC)

    def tearDown(self):
        self.temporary.cleanup()

    def make_zip(self, name="input.zip", members=None):
        target = self.root / name
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
            for path, data in (members or [("Takeout/Google Photos/Photos from 2025/image.jpg", b"synthetic photo")]):
                archive.writestr(path, data)
        return target

    def imported(self, source, key="sample"):
        return import_zip(source, self.config["library_dir"], "example", key)

    def test_deleting_from_drive_does_not_delete_nas_zip_or_media(self):
        source = self.make_zip()
        drive = FakeDrive({"takeout-20260101T000000Z-001.zip": source})
        self.assertEqual(poll_account(self.config, self.account, drive, self.now), (0, 1))
        later = self.now + dt.timedelta(hours=25)
        self.assertEqual(poll_account(self.config, self.account, drive, later), (1, 0))
        existing = sorted(p for p in (self.root / "raw").rglob("*.zip"))
        objects = sorted(p for p in (self.root / "library").rglob("*.jpg"))
        drive.files = {}
        self.assertEqual(poll_account(self.config, self.account, drive, later + dt.timedelta(days=1)), (0, 0))
        self.assertTrue(all(p.exists() for p in existing + objects))
        self.assertEqual(drive.deleted, [])

    def test_identical_media_is_deduplicated_and_metadata_retained(self):
        source = self.make_zip(members=[("Takeout/Google Photos/Album/a.jpg", b"same"),
                                       ("Takeout/Google Photos/Year/a.jpg", b"same"),
                                       ("Takeout/Google Photos/Album/a.jpg.json", b'{"title":"a.jpg"}')])
        manifest, changed = self.imported(source)
        self.assertTrue(changed)
        self.assertEqual(len(manifest["members"]), 3)
        self.assertEqual(len(list((self.root / "library/example/objects").glob("*.jpg"))), 1)
        before = source.read_bytes()
        _, changed = self.imported(source)
        self.assertFalse(changed)
        self.assertEqual(source.read_bytes(), before)

    def test_same_filename_different_bytes_survives_both_imports(self):
        first = self.make_zip("first.zip", [("Takeout/Google Photos/a.jpg", b"first")])
        second = self.make_zip("second.zip", [("Takeout/Google Photos/a.jpg", b"second")])
        a, _ = self.imported(first, "first")
        b, _ = self.imported(second, "second")
        self.assertNotEqual(a["members"][0]["object"], b["members"][0]["object"])
        self.assertEqual(len(list((self.root / "library/example/objects").glob("*.jpg"))), 2)

    def test_changed_zip_cannot_replace_previous_import(self):
        first = self.make_zip(members=[("Takeout/Google Photos/a.jpg", b"first")])
        original, _ = self.imported(first)
        self.make_zip(members=[("Takeout/Google Photos/a.jpg", b"other")])
        with self.assertRaises(ArchiveError):
            self.imported(first)
        self.assertEqual(load_json(self.root / "library/example/manifests/sample.json"), original)
        target = self.root / "library/example/objects" / original["members"][0]["object"]
        self.assertEqual(target.read_bytes(), b"first")

    def test_corrupt_zip_never_publishes_success_manifest(self):
        source = self.make_zip(members=[("Takeout/a.jpg", b"UNIQUE-SYNTHETIC-PAYLOAD")])
        data = source.read_bytes().replace(b"UNIQUE-SYNTHETIC-PAYLOAD", b"UNIQUE-SYNTHETIC-FAILURE")
        source.write_bytes(data)
        with self.assertRaises(ArchiveError):
            self.imported(source)
        self.assertTrue(source.exists())
        self.assertFalse((self.root / "library/example/manifests/sample.json").exists())

    def test_unsafe_archive_paths_are_rejected_before_any_objects(self):
        for bad in ("../escape.jpg", "/absolute.jpg", "Takeout/../../escape.jpg", "C:/escape.jpg", "a\\b.jpg"):
            with self.subTest(path=bad):
                source = self.make_zip(members=[("Takeout/good.jpg", b"safe"), (bad, b"bad")])
                with self.assertRaises(ArchiveError):
                    self.imported(source)
                self.assertFalse((self.root / "library/example/objects").exists())

    def test_symlink_member_is_rejected(self):
        source = self.root / "link.zip"
        info = zipfile.ZipInfo("Takeout/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(info, "../../elsewhere")
        with self.assertRaises(ArchiveError):
            self.imported(source)

    def test_failed_object_install_never_removes_existing_data(self):
        directory = self.root / "objects"
        target, _, _ = store_object(io.BytesIO(b"existing"), directory, ".jpg")
        with patch("takeout_archive.core.os.link", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                store_object(io.BytesIO(b"new"), directory, ".jpg")
        self.assertEqual((directory / target).read_bytes(), b"existing")
        self.assertEqual(list(directory.glob(".pending-*")), [])

    def test_changed_existing_object_is_not_silently_overwritten(self):
        directory = self.root / "objects"
        target, _, _ = store_object(io.BytesIO(b"original"), directory, ".jpg")
        destination = directory / target
        destination.chmod(0o600)
        destination.write_bytes(b"tampered")
        with self.assertRaises(ArchiveError):
            store_object(io.BytesIO(b"original"), directory, ".jpg")
        self.assertEqual(destination.read_bytes(), b"tampered")

    def test_repeated_poll_does_not_refresh_old_export_date(self):
        drive = FakeDrive({"takeout-20260101T000000Z-001.zip": self.make_zip()})
        poll_account(self.config, self.account, drive, self.now)
        later = self.now + dt.timedelta(hours=25)
        poll_account(self.config, self.account, drive, later)
        old = load_json(account_state(self.config, "example"))["latest_verified_export"]
        poll_account(self.config, self.account, drive, later + dt.timedelta(days=10))
        self.assertEqual(load_json(account_state(self.config, "example"))["latest_verified_export"], old)
        self.assertTrue(any("no newly verified" in m for m in alerts(self.config, self.account, later + dt.timedelta(days=90))))

    def test_metadata_change_restarts_stability_wait(self):
        source = self.make_zip()
        drive = FakeDrive({"takeout-20260101T000000Z-001.zip": source})
        poll_account(self.config, self.account, drive, self.now)
        self.make_zip(members=[("Takeout/a.jpg", b"different content")])
        self.assertEqual(poll_account(self.config, self.account, drive, self.now + dt.timedelta(hours=25)), (0, 1))

    def test_manifest_recovers_freshness_after_interrupted_state_write(self):
        drive = FakeDrive({"takeout-20260101T000000Z-001.zip": self.make_zip()})
        poll_account(self.config, self.account, drive, self.now)
        later = self.now + dt.timedelta(hours=25)
        poll_account(self.config, self.account, drive, later)
        state_path = account_state(self.config, "example")
        state = load_json(state_path)
        state.pop("latest_verified_export")
        state["verified_archives"] = {}
        state_path.write_text(json.dumps(state))
        poll_account(self.config, self.account, drive, later)
        self.assertEqual(load_json(state_path)["latest_verified_export"], "2026-01-01T00:00:00+00:00")

    def test_future_export_date_cannot_suppress_staleness(self):
        drive = FakeDrive({"takeout-20990101T000000Z-001.zip": self.make_zip()})
        with self.assertRaises(ArchiveError):
            poll_account(self.config, self.account, drive, self.now)
        self.assertFalse((self.root / "raw").exists())

    def test_wrong_drive_checksum_has_no_success_record(self):
        class WrongHash(FakeDrive):
            def list_exports(self, remote):
                rows = super().list_exports(remote)
                rows[0]["Hashes"]["MD5"] = "0" * 32
                return rows
        drive = WrongHash({"takeout-20260101T000000Z-001.zip": self.make_zip()})
        poll_account(self.config, self.account, drive, self.now)
        with self.assertRaises(ArchiveError):
            poll_account(self.config, self.account, drive, self.now + dt.timedelta(hours=25))
        state = load_json(account_state(self.config, "example"))
        self.assertFalse(state["verified_archives"])
        self.assertNotIn("latest_verified_export", state)

    def test_renewal_and_initial_baseline_alerts(self):
        early = dt.datetime(2026, 12, 1, tzinfo=UTC)
        self.assertFalse(any("expires in" in m for m in alerts(self.config, self.account, early)))
        due = dt.datetime(2026, 12, 2, tzinfo=UTC)
        self.assertTrue(any("expires in 30" in m for m in alerts(self.config, self.account, due)))
        expired = dt.datetime(2027, 1, 2, tzinfo=UTC)
        self.assertTrue(any("expired 1" in m for m in alerts(self.config, self.account, expired)))
        self.assertTrue(any("no verified archive" in m for m in alerts(self.config, self.account, due)))

    def test_overlapping_jobs_are_rejected(self):
        with lock(self.root / "lock"):
            with self.assertRaises(ArchiveError):
                with lock(self.root / "lock"):
                    pass

    def test_rclone_never_uses_sync_move_or_prints_auth_secrets(self):
        commands = []
        def run(arguments, **kwargs):
            commands.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        with patch("takeout_archive.core.subprocess.run", side_effect=run):
            Rclone().copy("drive:Takeout", "a.zip", self.root / "a.zip")
        self.assertIn("copyto", commands[0])
        self.assertIn("--immutable", commands[0])
        self.assertNotIn("move", commands[0])
        self.assertNotIn("sync", commands[0])
        bad = subprocess.CompletedProcess([], 1, "", "invalid_grant secret-token-value")
        with patch("takeout_archive.core.subprocess.run", return_value=bad):
            with self.assertRaises(ArchiveError) as caught:
                Rclone().list_exports("drive:Takeout")
        self.assertIn("reauthorize", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

    def test_cli_test_alert_exits_nonzero_without_contacting_google(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(self.config))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(Rclone, "list_exports") as listing:
            self.assertEqual(main(["--config", str(config_path), "test-alert"]), 1)
        listing.assert_not_called()
        self.assertIn("TEST ONLY", output.getvalue())
        self.assertFalse((self.root / "raw").exists())

    def test_config_rejects_account_traversal_and_overlapping_roots(self):
        path = self.root / "config.json"
        self.account["name"] = "../elsewhere"
        path.write_text(json.dumps(self.config))
        with self.assertRaises(ArchiveError):
            config_from_file(path)
        self.account["name"] = "example"
        self.config["archive_dir"] = self.config["library_dir"]
        path.write_text(json.dumps(self.config))
        with self.assertRaises(ArchiveError):
            config_from_file(path)


if __name__ == "__main__":
    unittest.main()
