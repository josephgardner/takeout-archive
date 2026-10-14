# Takeout Archive

Keep an additive Google Photos archive on a headless Synology NAS, with renewal and failure reminders delivered by DSM's existing email setup.

Google Takeout exports Photos to Google Drive. A NAS job uses rclone to copy new export ZIPs, verify them, and retain their contents and metadata. **Deleting a photo or video from Google never deletes its archived NAS copy.**

This initial release has synthetic tests. Live Google-account, DSM email and Hyper Backup restore validation are still required before relying on it. Normal operation needs no browser on the NAS; initial Google authorization and annual Takeout renewal use a desktop browser.

## Scope

- Synology DSM 7 and Python 3.8+, also usable on Linux/macOS with a scheduler.
- rclone's Google Drive backend, with a separate remote for each account.
- Google Photos-only Takeout exports, monthly or every two months for one year where offered by your account.
- Daily headless polling, 30-day renewal reminders, stale-export and authorization alerts.
- Source ZIPs, videos, photos, JSON sidecars and other archive members are retained.
- Identical content is stored once per account and extension; manifests retain original paths and album structure. Distinct files with identical names remain distinct.

There is no cloud deletion, NAS archive pruning, browser automation, or SMTP credential handling. This is an archive store, not a Synology Photos gallery or an EXIF-editing tool.

## Setup

1. Install [rclone from its official distribution](https://rclone.org/install/) and place the binary at a path your NAS user can execute. Python needs no third-party dependencies.
2. For each account, configure Google Takeout with **Google Photos as the only selected product**, recurring exports, ZIP format, and delivery to Google Drive. Where offered, choose monthly and **Only new and updated items** after the first export. Record the actual schedule expiration date. The first export establishes the baseline. [Google's export instructions](https://support.google.com/accounts/answer/3024190).
3. Create one named rclone **Google Drive** remote per account. Use read-only access where practical. Authenticate on a desktop using [rclone's headless setup procedure](https://rclone.org/remote_setup/). This does not use the restricted Google Photos API.
4. Copy `config.example.json` to a private `config.json`. Set separate absolute state/raw/library directories, your remote names, rclone binary path and actual Takeout expiry dates. The sample dates are placeholders. Keep rclone credentials and local config outside Git.
5. Check available Drive and NAS space. Exports consume Drive space and this tool leaves them there. Cloud-export cleanup is a separate manual decision. NAS storage retains ZIPs plus deduplicated extracted members.
6. Run the initial poll, then poll again after the configured stability period:

```sh
cd /volume1/docker/takeout-archive
/usr/bin/python3 -m takeout_archive --config /volume1/docker/takeout-archive/config.json poll
```

The first observation waits 24 hours by default; a metadata change resets that wait. A nonzero exit means attention is needed and includes initial-baseline reminders. Credentials are never included in reports.

## DSM email integration

Use **Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script**. Run under the user that owns the rclone configuration and archive directories; administrator/root access is unnecessary for this tool.

Schedule `poll` daily. Under Task Settings, enable **Send run details by email** and **Send run details only when the script terminates abnormally**, using your existing alert recipient. [Synology documents this mechanism](https://kb.synology.com/index.php/en-ro/DSM/help/DSM/AdminCenter/system_taskscheduler?version=7).

Use the command above as the entire script. Preserve its exit status: do not append `exit 0`, pipe it through an unchecked command, or swallow failures. DSM handles mail using its existing notification configuration.

Verify delivery by temporarily running this command from a scheduled task with those email settings:

```sh
cd /volume1/docker/takeout-archive
/usr/bin/python3 -m takeout_archive --config /volume1/docker/takeout-archive/config.json test-alert
```

`test-alert` deliberately exits 1 and says **TEST ONLY**. It does not contact Google or modify archive data. After confirming receipt, restore the task's command to `poll`.

Reminders begin 30 days before each account's configured expiry and repeat on each daily run until resolved. They include the account label, date and renewal link. Renew the schedule in Google Takeout, then update `schedule_expires_on`; changing that local date alone does not renew Google. Verify a new export arrives. The example config alerts after 45 days for monthly exports; set `stale_days` to 75 for two-month exports. The clock uses the production date of the latest verified archive, so rereading old ZIPs cannot make an obsolete backup appear fresh.

The program cannot observe SMTP delivery from Task Scheduler. DSM owns mail retries/logging. Keep Task Scheduler output enabled, and periodically check the job and notification delivery; a NAS that is offline cannot email about its own downtime.

## Storage and verification

```text
raw/<account>/<export-part-and-path-id>.zip
library/<account>/objects/<sha256>.<extension>
library/<account>/manifests/<export-part-and-path-id>.json
state/<account>.json
```

Manifests map each original archive member path to its immutable object, size and SHA-256. Raw ZIPs also retain the original export structure, so they can be opened or imported by other tools. JSON sidecars are preserved byte-for-byte; this tool does not rewrite original media. Objects are read-only to reduce accidental edits. The storage format is documented in [docs/FORMAT.md](docs/FORMAT.md).

On first ingestion, the downloaded ZIP must match Drive's MD5 and its members must pass ZIP CRC checks. A manifest is published only after all members in that ZIP are ingested. Existing objects are never overwritten. Interrupted imports can resume; temporary incomplete files are discarded, while installed objects and raw inputs remain.

Google does not supply this tool with the library inventory or the expected number of export parts. Metadata stability is a delivery heuristic, **not proof that every export part or every Google photo was included**. The tool reports verified individual ZIPs, never a proven complete Google library. Compare expected part counts with Google Takeout for the initial baseline and after questionable exports; an unseen missing final part cannot be detected automatically yet. This limitation is a priority for a live pilot.

Normal polling checks existing object presence/size, rather than rehashing the entire library daily. Schedule a monthly deep verification separately:

```sh
cd /volume1/docker/takeout-archive
/usr/bin/python3 -m takeout_archive --config /volume1/docker/takeout-archive/config.json verify
```

Protect the raw/library directories with Hyper Backup or your offsite backup tool, then recover representative ZIPs and media into an isolated directory. A passing checksum is not an offsite restore test. Back up state/config too, with appropriate protections for credentials.

## Other commands

```sh
python3 -m takeout_archive --config config.json status
python3 -m takeout_archive --config config.json check
python3 -m takeout_archive --config config.json import /path/to/export.zip --account person-one
python3 -m unittest discover -s tests -v
```

`status` reads local metadata and does not contact Google. `check` produces renewal/freshness reminders without downloads. Local `import` preserves the supplied ZIP; it does not assert Google export freshness.

## Why another small tool?

Existing projects informed the scope: [othermore/google-photos-backup](https://github.com/othermore/google-photos-backup), [gphotos-sync](https://github.com/gilesknap/gphotos-sync), and [gtakeout_backup](https://github.com/yottabit42/gtakeout_backup). This implementation is original and intentionally focuses on retained inputs, additive storage and DSM's existing mail route. rclone handles cloud access; Google handles export generation. Contributions and adjacent NAS setups are welcome.

MIT licensed.
