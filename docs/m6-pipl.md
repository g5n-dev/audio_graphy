# AudioGraphy M6 PIPL §14.3 Compliance Guide

> PIPL (China's Personal Information Protection Law, 《个人信息保护法》) §14.3
> mandates encryption-at-rest, retention enforcement, PII redaction, and
> auditable data-subject access / erasure flows for systems processing
> personal information. This guide walks operators through AudioGraphy M6's
> compliance posture, configuration knobs, and operational playbook.

| Section | What you get |
|---|---|
| [§1 Overview](#1-overview) | scope + guarantees |
| [§2 Configuration](#2-configuration) | master key + retention_days |
| [§3 AES envelope flow](#3-aes-envelope-encryption-flow) | encrypt / decrypt / audit |
| [§4 PII categories](#4-pii-categories-covered) | 6 regex redaction rules |
| [§5 DSAR endpoints](#5-dsar-endpoints) | export / erase / audit + curl |
| [§6 Master key rotation](#6-master-key-rotation-m7) | placeholder + manual path |
| [§7 Compliance checklist](#7-deployment-compliance-checklist) | 12-point checklist |

---

## 1. Overview

AudioGraphy M6 ships four pillars of PIPL §14.3 compliance:

| Pillar | Module | What it does |
|---|---|---|
| **Encryption at rest** | `core/crypto.py` | AES envelope (master + per-file data key, Fernet-backed) for audio files |
| **PII redaction** | `core/pii.py` | Regex-based masking for 6 PII categories in transcripts and LLM outputs |
| **Retention enforcement** | `core/retention.py` | Daily 03:00 cron hard-deletes recordings past `recording_retention_days` |
| **DSAR endpoints** | `api/dsar.py` | Admin-only export / erase / audit endpoints with full audit trail |

**Out of scope for M6:**
- Chinese-name recognition (surname-dict NER is M7+).
- Soft-delete recycling bin (M6 is hard-delete only).
- MySQL TDE / column encryption (M6 covers application-layer only).
- Master-key auto-rotation (manual procedure documented; M7+ automation).

---

## 2. Configuration

### 2.1 Master key provisioning

The master AES key is a 32-byte urlsafe-base64 string stored on disk with
0600 permissions. Provision once per environment:

```bash
# Generate a new master key.
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > /run/secrets/audiography_master.key
chmod 0600 /run/secrets/audiography_master.key

# Point AudioGraphy at it (already the default; can be overridden in .env).
echo "MASTER_KEY_PATH=/run/secrets/audiography_master.key" >> .env
```

**Dev mode**: if the key file is missing AND `LOG_LEVEL=DEBUG`, the backend
auto-generates one and logs a loud warning. This is for local dev only;
production deployments must provision a key out-of-band.

### 2.2 Retention window

Edit `.env` to set the retention window in days:

```dotenv
# Recordings older than this many days are hard-deleted by the daily 03:00 cron.
RECORDING_RETENTION_DAYS=90
```

The cron runs at 03:00 Asia/Shanghai daily (see `main.py` lifespan wiring).

### 2.3 PII scrubber (stateless, no config)

`PIIScrubber` is stateless and always enabled. There are no tunable knobs
in M6; M7+ will add Chinese-name recognition as an opt-in module.

---

## 3. AES envelope encryption flow

AudioGraphy uses a two-tier envelope scheme — a long-lived **master key**
encrypts a fresh **data key** generated per audio file. The data key
encrypts the audio bytes; the master never directly encrypts audio.

```
┌─────────────────────────────────────────────────────────┐
│  Master Key (32 bytes, /run/secrets/audiography_master.key) │
└────────────────────┬────────────────────────────────────┘
                     │ wraps (Fernet)
                     ▼
              ┌─────────────┐
              │  Data Key   │  ← generated per file, ephemeral
              └──────┬──────┘
                     │ encrypts (Fernet)
                     ▼
              ┌──────────────┐
              │  Audio bytes │  ← ciphertext on disk
              └──────────────┘
```

**Encrypt flow** (on recording upload):

1. `IngestionService.register_recording(...)` calls `AudioCrypto.encrypt_file(plain, cipher)`.
2. `AudioCrypto` loads the master Fernet (lazy + cached).
3. Generates a fresh data key via `Fernet.generate_key()`.
4. Wraps the data key with the master → `encrypted_dk`.
5. Encrypts the audio bytes with the data key.
6. Writes a JSON header line + ciphertext to `audio_encrypted_path`.
7. Records metadata in `recordings.audio_encryption_meta` for DSAR.

**Decrypt flow** (on DSAR export, admin-only):

1. `POST /api/v1/dsar/export/{recording_id}` with `reason` body.
2. Endpoint verifies admin role + writes `dsar.export` audit_log row.
3. `AudioCrypto.decrypt_file(cipher, plain)` reverse: decrypts data key, then audio.
4. Returns a streaming ZIP with audio + transcript + tags + audit logs.
5. Audit row: `action=decrypt`, `target=recording:{id}`,
   `before={displayed: "redacted"}`, `after={displayed: "plain"}`.

---

## 4. PII categories covered

Six PII types are redacted by `core/pii.py`. Replacement format keeps
enough context for QA review without leaking the full PII value.

| Category | Pattern (simplified) | Replacement | Example |
|---|---|---|---|
| `phone` (mobile) | `1[3-9]\d{9}` | `138****1234` | `13812345678` → `138****5678` |
| `phone` (landline) | `0\d{2,3}-?\d{7,8}` | `010****5678` | `010-12345678` → `010****5678` |
| `id_card` | 18-digit CN ID | `11**********34` | last char may be `X` |
| `bank_card` | 16-19 digit number | `62***************8` | — |
| `email` | RFC-ish | `ab***@example.com` | — |
| `ipv4` | dotted quad | `10.0.**.**` | last two octets masked |

**Idempotent**: scrubbing an already-scrubbed text returns the same text
(no double-mask). Verified by `tests/core/test_pii.py::test_idempotent`.

**Layered application**:
- `segments.text_scrubbed` — written once at ASR completion.
- `query.answer` — re-scrubbed after LLM generation (defense-in-depth).
- `citations[].text` — third-pass scrub (defensive).

---

## 5. DSAR endpoints

All three endpoints require the `admin` role. Each writes an `audit_logs`
row before returning.

### 5.1 Export (access request)

```bash
# Submit an access request for recording 42.
curl -X POST http://localhost:8000/api/v1/dsar/export/42 \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{"reason": "质检复盘需要 - 工单 #1234"}'
```

Response (200 OK):

```json
{
  "recording_id": 42,
  "download_url": "/api/v1/dsar/files/req-abc123",
  "expires_at": "2026-07-21T15:00:00Z",
  "audit_log_id": 1024
}
```

Audit: `action=dsar.export`, `target=recording:42`,
`before={displayed: "redacted"}`, `after={displayed: "plain"}`.

### 5.2 Erase (right to be forgotten)

```bash
curl -X POST http://localhost:8000/api/v1/dsar/erase/42 \
  -H "Authorization: Bearer $ADMIN_JWT"
```

Response: `204 No Content`. Hard-deletes audio file + DB rows + GraphML
references. `audit_logs` rows themselves are NOT deleted (PIPL requires
the audit trail to persist).

### 5.3 Audit log query

```bash
# List audit_logs for tenant=chang_an, action=dsar.export, page 1.
curl "http://localhost:8000/api/v1/dsar/audit?action=dsar.export&page=1&page_size=50" \
  -H "Authorization: Bearer $ADMIN_JWT"
```

Response (200 OK):

```json
{
  "items": [
    {
      "id": 1024,
      "tenant_id": "chang_an",
      "user_id": 1,
      "action": "dsar.export",
      "target": "recording:42",
      "before_value": {"displayed": "redacted"},
      "after_value": {"displayed": "plain"},
      "occurred_at": "2026-07-21T14:32:11Z"
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 50
}
```

---

## 6. Master key rotation (M7+)

M6 ships a `rotate_master_key(old_path, new_path)` stub that raises
`NotImplementedError`. The manual procedure is documented here for ops:

1. Provision a new master key at `/run/secrets/audiography_master_v2.key`.
2. For each recording with `audio_encryption_meta`:
   a. Read the old encrypted data key from the JSON header.
   b. Decrypt the data key with the OLD master.
   c. Re-encrypt the data key with the NEW master.
   d. Rewrite the JSON header on disk.
   e. Update `recordings.audio_encryption_meta` to reflect the new key id.
3. Update `MASTER_KEY_PATH` env to point at the new key file.
4. Restart the backend.

This procedure preserves the data key (so audio bytes are not re-encrypted).
M7+ will ship an automated tool that runs as a one-shot admin command.

---

## 7. Deployment compliance checklist

A 12-point pre-production checklist for PIPL §14.3 sign-off:

- [ ] Master AES key generated and stored at `MASTER_KEY_PATH` (0600 perms).
- [ ] `LOG_LEVEL=INFO` (or higher) in production — DEBUG triggers dev-key auto-gen.
- [ ] `MASTER_KEY_PATH` backed up offline (loss = data loss; not recoverable).
- [ ] `RECORDING_RETENTION_DAYS` set per data-retention policy (e.g. 90 days).
- [ ] Retention cron visible in logs (`retention_daily` APScheduler job).
- [ ] `audit_logs` table writes verified for: `recording.uploaded`, `dsar.export`,
      `dsar.erase`, `decrypt`, `retention_delete`.
- [ ] `audit_logs.quick_wins` writes verified for: `prompt.activate`,
      `tags.recompute`, `recording.reindex`.
- [ ] PII scrubber verified end-to-end (upload transcript with phone number,
      GET recording → `[REDACTED-PHONE]` in response).
- [ ] DSAR export produces ZIP with audio + transcript + tags + audit_logs.
- [ ] DSAR erase hard-deletes audio + DB rows; audit_logs persist.
- [ ] Prometheus `/metrics` exposes `audiography_audit_log_written_total`
      for compliance dashboards.
- [ ] Backup / disaster-runbook includes master-key restoration steps.

---

**End of M6 PIPL Guide** — for architecture details see
[`docs/m6-architecture.md §3`](./m6-architecture.md#3-pipl-143-详细设计).
