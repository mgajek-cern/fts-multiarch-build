# S3/MinIO integration

The testbed runs two MinIO instances (`minio1`, `minio2`) as S3-compatible
object stores. Two distinct transfer paths exercise them — a direct FTS path
and a Rucio conveyor path — each with different auth and TLS requirements.

## Architecture

MinIO runs plain HTTP on port 9000. A lightweight nginx TLS proxy
(`minio1-tls`, `minio2-tls`) sits in front of it and presents the HTTPS
endpoint that Rucio's pre-signed URL path requires. The direct FTS path
bypasses the proxy entirely.

```
minio1-tls:9000  ──TLS──▶  nginx  ──HTTP──▶  minio1:9000
minio2-tls:9000  ──TLS──▶  nginx  ──HTTP──▶  minio2:9000
```

MinIO certs are signed by the testbed CA with SANs covering both `minioN`
and `minioN-tls`, so FTS can verify the nginx cert when it fetches
pre-signed URLs.

---

## Path A — direct FTS (`test-fts-with-s3.py`)

gfal2/davix handles S3 signing internally using credentials from
`gfal2_http_plugin.conf`. MinIO stays on plain HTTP; no TLS proxy is
involved.

```
┌─────────────┐   s3://minio1:9000/...   ┌─────────┐   SigV2 signed   ┌──────────┐
│ fts         │ ──────────────────────▶  │   fts   │ ───────────────▶ │  minio1  │
│ (container) │   gfal2 signs request    │  :8446  │   HTTP :9000     │  HTTP    │
└─────────────┘                          └─────────┘                  └──────────┘
```

**Credential flow:** `[S3] ACCESS_KEY / SECRET_KEY` in `gfal2_http_plugin.conf`
→ davix signs the request before sending → MinIO validates signature.

**S3 credential registration in FTS** uses `S3:minio1:9000` (port required —
gfal2 matches on `hostname:port`):

```python
fts_curl("-X", "POST", f"{FTS}/config/cloud_storage",
    "-d", json.dumps({"storage_name": "S3:minio1:9000"}))
```

---

## Path B — Rucio conveyor (`test-rucio-transfers.py`)

Rucio generates a pre-signed HTTPS URL using credentials from
`rse-accounts.cfg`, then submits that URL to classic FTS. FTS fetches the
object using the pre-signed URL — no separate S3 credentials needed at
the FTS layer.

```
┌────────┐  SigV4 sign  ┌──────────────┐  https://minio1-tls:9000/...?sig=...
│ rucio  │ ───────────▶ │ rse-accounts │ ────────────────────────────┐
└────────┘              └──────────────┘                             │
                                                                     ▼
┌────────┐  submit job  ┌─────────┐  fetch pre-signed URL   ┌───────────────┐
│ rucio  │ ───────────▶ │   fts   │ ─────────────────────▶  │ minio1-tls    │
│        │              │  :8446  │                         │ nginx :9000   │
└────────┘              └─────────┘                         └───────┬───────┘
                                                                    │ proxy_pass
                                                                    ▼
                                                            ┌──────────────┐
                                                            │ minio1       │
                                                            │ HTTP :9000   │
                                                            └──────────────┘
```

**Credential flow:** `rse-accounts.cfg` (keyed by RSE UUID) → Rucio signs URL
with SigV4 → URL contains signature in query string → FTS fetches without
needing separate S3 credentials.

**RSE protocol hostname** is `minio1-tls` (the nginx proxy), not `minio1`:

```bash
rucio-admin rse add-protocol MINIO1 \
    --scheme https --hostname minio1-tls --port 9000 \
    --prefix /fts-test ...
```

---

## Cross-protocol transfers (S3 ↔ XRootD)

The Rucio `BASE_SCHEME_MAP` is patched to allow `root://` ↔ `https://`
cross-protocol routing. Without this patch the conveyor raises
`MISMATCH_SCHEME` and the request goes STUCK.

```python
# shared/patches/rucio/constants.py
BASE_SCHEME_MAP = {
    ...
    "root":  ["root", "https"],   # enables XRD → S3
    "https": ["https", ..., "root"],  # enables S3 → XRD
}
```

This enables:

| Test | Source | Destination | FTS instance |
|------|--------|-------------|--------------|
| `test_s3_minio1_to_minio2` | MINIO1 (https) | MINIO2 (https) | fts (GSI) |
| `test_s3_minio1_to_xrd2` | MINIO1 (https) | XRD2 (root) | fts (GSI) |
| `test_s3_xrd1_to_minio1` | XRD1 (root) | MINIO1 (https) | fts (GSI) |
| `test_s3_minio1_to_minio2_oidc` | MINIO1 (https) | MINIO2 (https) | fts (GSI) |

Note the OIDC variant (`_oidc`) only changes how the Rucio client
authenticates to the Rucio server — the transfer still uses SigV4 signed
URLs submitted to the classic GSI FTS instance.

---

## Why XRD3/XRD4 + S3 cannot be combined

XRD3/XRD4 use SciTokens via `fts-oidc` (bearer token auth).
MINIO RSEs use SigV4 via classic `fts` (GSI cert auth).
A single FTS job cannot authenticate using both mechanisms simultaneously.

```
XRD3 ──────▶ fts-oidc  (accepts bearer tokens, rejects GSI)
                  ✗
MINIO1 ────▶ fts        (accepts GSI proxy, rejects unmanaged tokens)
```

Multihop would be required to bridge the two auth domains — not worth the
added complexity for a testbed demonstration.

---

## Seeding files into MinIO

**From inside the MinIO container** (used by `test-rucio-transfers.py`):

```python
svc_exec("minio1", ["bash", "-c",
    "mc alias set local http://localhost:9000 minioadmin minioadmin --quiet; "
    "mc pipe local/fts-test/path/to/file"
])
```

**From inside the FTS container** (used by `test-fts-with-s3.py`):

```python
_run(["bash", "-c",
    "mc alias set local https://minio1:9000 minioadmin minioadmin --insecure --quiet; "
    "mc mb --insecure --ignore-existing local/fts-test; "
    "printf 'content' | mc pipe --insecure local/fts-test/file"
])
```
