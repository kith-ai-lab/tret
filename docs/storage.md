# Storage

Where tret keeps the bytes of documents, and how to move them off a volume.

## What is stored where

Everything tret keeps as a file goes through one interface (`tret/storage`, a `BlobStore`):

| What | Key |
|---|---|
| Uploads, fetched pages, files imported from SharePoint/OneDrive/Drive | `<sha256>-<filename>` |
| Packaged page subsets (approval-time packaging) | `packages/<finding_id>/<name>` |
| Batch records kept as files | `batches/<batch_id>/<name>` |

Keys are POSIX-relative: no leading slash, no `..`. The `documents` table records the key
in `storage_key` and, for readers that need a real file, the local path in `storage_path`.

Not in the store, always on local disk under `TRET_STORAGE_DIR`: installed domain packs
(`packs/`), the s3 cache (`cache/`), and the short-lived upload spool (`.incoming-*.part`).

## The two backends

**`local`** (default). Bytes live at `TRET_STORAGE_DIR/<key>`. This is the layout tret has
always used; nothing changes on disk, and no extra package is needed.

**`s3`**. Bytes live in an S3-compatible bucket (Fly Tigris, AWS S3, MinIO). A file survives
a restart, a redeploy and the loss of the machine, and a download made for one batch is
reused by the next. Poppler, pypdf and tesseract need a real file, so objects are staged on
demand into `TRET_STORAGE_DIR/cache/<key>`. Cache files are named by key and every document
key is content-addressed, so evicting one is always safe; it is downloaded again on the next
read. Install the client with `pip install 'tret[s3]'` (boto3).

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `TRET_STORAGE_BACKEND` | `local` | `local` or `s3` |
| `TRET_STORAGE_DIR` | `./storage` | Local root; also the spool and the s3 cache |
| `TRET_STORAGE_S3_BUCKET` | | Bucket; falls back to `BUCKET_NAME` |
| `TRET_STORAGE_S3_ENDPOINT_URL` | | Endpoint; falls back to `AWS_ENDPOINT_URL_S3` |
| `TRET_STORAGE_S3_REGION` | | Region; falls back to `AWS_REGION` (Tigris uses `auto`) |
| `TRET_STORAGE_CACHE_MAX_MB` | `512` | Soft cap on the s3 cache; `0` = unbounded. Keep it well below the volume size |

Credentials are the standard `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. The fallbacks
above are exactly what `fly storage create` sets, so on Fly only the backend switch is needed.
With `s3` and no bucket, tret refuses to boot with a message saying so. The boot log prints
one `storage:` line with the active backend (never a secret).

## Moving a Fly deployment to Tigris

1. `fly storage create` and attach it to the app. This sets `BUCKET_NAME`,
   `AWS_ENDPOINT_URL_S3`, `AWS_REGION` and the access keys as secrets.
2. Make sure the image has boto3 (`tret[s3]`), then `fly secrets set TRET_STORAGE_BACKEND=s3`.
   Also set the cache cap below the volume size: the cache lives on the volume, so on a 1 GB
   volume use `fly secrets set TRET_STORAGE_CACHE_MAX_MB=256` (256 to 512 is right; the
   default 512 is the most you want there). A cap near or above the volume size lets the
   cache fill the disk it was meant to relieve.
   Until the next step, rows that are not in the bucket yet are still served from the volume.
3. On a machine with the volume mounted: `tret storage migrate --to s3` (add `--dry-run` first
   to see the plan).

Later, once you trust the bucket, you can shrink or drop the volume. `--delete-local` removes
each volume copy as soon as its upload is verified; without it nothing is deleted.

### `tret storage migrate`

For every Document row it uploads the file under its key, checks the stored size, records
`storage_key`, and prints a summary. The first `--verify-sample` uploads (default 20) and every
file under 8 MB are also re-downloaded straight from the bucket and hashed. An object already
present at the right size is skipped, so the command is resumable and a re-run after success
changes nothing. It exits non-zero if anything failed, and lists each failure. `storage_path`
is left alone, so pointing the backend back at `local` still works. Files on disk with no
Document row (orphans) are not moved.

### `tret storage status`

Prints the backend, bucket and endpoint (never credentials), object count and bytes, the cache
size, and how many Document rows have a `storage_key`. On s3 it also reports
`cache_packages_bytes`, `cache_pinned_bytes`, `cache_floor_bytes` (the non-evictable floor),
`cache_evictable_bytes` and `cache_max_mb`.

## Operating notes

- The migration uploads without filling the cache, so it does not grow the volume.
- Cache eviction never touches a file that is pinned (or sits under a pinned directory, as a
  running packaging job's `cache/packages/<finding_id>/` does) or lives under a dot-prefixed
  in-progress directory or file. `.tmp-*` downloads older than an hour are crash debris and are
  evicted. Everything else, including `cache/packages/` after a run finishes, is ordinary
  cache: the bucket holds every output and `local_path` downloads it again on demand.
- The pinned and in-progress files are a floor under the cap. If the floor exceeds
  `TRET_STORAGE_CACHE_MAX_MB`, tret logs one warning (at most every 10 minutes) with both
  numbers; raise the cap or run fewer packaging jobs at once.
- Imports create objects with create-if-absent (`If-None-Match: *`). If an endpoint does not
  support that header, tret logs one warning and falls back to check-then-write.

- Cache sizing: allow room for the largest document you package, since packaging stages the
  source PDF and writes its outputs there. The cap is soft: files staged in the last minute
  are never evicted, and neither is the file a call is returning.
- Packaged outputs are evidence. They are written to the cache first, uploaded when the
  packager returns, and removed only if that attempt cannot register them.
- Encrypt the bucket and scope its keys to tret; documents are stored in the clear, as on a
  volume (see [hardening.md](hardening.md) section 8).
