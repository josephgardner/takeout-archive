# Archive format v1

Each account has an independent namespace. Account labels are lowercase letters, numbers, underscores or hyphens; they are not email addresses.

## Raw inputs

For Drive inputs, the retained ZIP name combines its original export-part name with 16 hexadecimal characters from the SHA-256 of its Drive-relative path. This distinguishes equal filenames in different folders. Existing files are immutable during download (`rclone copyto --immutable`). Source exports are never deleted.

## Objects

Members stream into a temporary file while computing SHA-256. After CRC/size validation, the object is installed with a create-only hardlink; the temporary name is removed. A preexisting object must match its content hash. No existing object is unlinked to make room for another.

Object filenames are full SHA-256 plus a sanitized original extension. Members without a suitable extension use `.bin`. Duplicate payloads with the same extension share one object. Account namespaces are independent. No album path is used as a filesystem extraction target, so colliding original filenames remain separate objects.

## Manifests

One JSON manifest per verified ZIP contains:

```json
{
  "schema_version": 1,
  "archive_sha256": "<full SHA-256 of retained ZIP>",
  "archive_bytes": 12345,
  "members": [
    {
      "path": "Google Photos/Example Album/image.jpg",
      "object": "<full SHA-256>.jpg",
      "sha256": "<full SHA-256>",
      "bytes": 1234
    }
  ]
}
```

Leading `Takeout/` is removed from the recorded path. Otherwise names are retained as metadata, not extracted directory paths. All regular members, including JSON sidecars and HTML, are retained. Symbolic links, encrypted members, absolute paths and traversal paths are rejected. Manifests are atomically replaced and synced only after an entire ZIP passes ingestion. No complete-export or complete-library marker is recorded.

## State

Private per-account JSON stores Drive-relative filenames/fingerprints, first stable observations, verified ZIP references and the latest observed verified export production date. Export filenames and identifiers can be private. State belongs in an access-controlled directory and outside Git.

Loss of state can cause downloads to be observed again; preserved raw inputs and manifests allow reconciliation without replacing archived content. Failed state writes do not erase original inputs. Do not manually edit manifests or objects. A deep `verify` checks their hashes but cannot restore a missing object without the original ZIP or offsite copy.
