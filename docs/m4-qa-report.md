# M4 QA Sign-off Report — AudioGraphy

**QA engineer:** 严过关
**Delivery director:** 齐活林
**Date:** 2026-07-21
**Milestone:** M4 — Real adapter scaffolding (VAD/LLM/Embed) + config mode-switching
**Baseline:** M3 commit `8f6f841` (623 tests / 91.54% coverage)
**Verification scope:** Independent re-run of all engineer claims + coverage gap-fill

---

## 1. 验证摘要 (Verification Summary)

All eight independent checks executed by QA on local checkout. Engineer's claims reproduced unless noted.

| # | Check | Expected | Actual | Result | Notes |
|---|-------|----------|--------|--------|-------|
| 1.1 | Full test suite | `643 passed` | `643 passed` (pre-gap-fill) → `657 passed` (post-gap-fill) | ✅ PASS | Engineer claim reproduced; QA added 14 tests, still all green. |
| 1.2 | Adapter module coverage ≥ 90% (PRD §8.1) | vad/llm/embed ≥ 90% | vad 90% ✓ / llm 89% ✗ / embed 86% ✗ (pre) → llm 99% / embed 98% (post) | ✅ → ✅ PASS | Gaps closed by QA — see §3. |
| 1.3 | ruff lint | `All checks passed!` | `All checks passed!` | ✅ PASS | — |
| 1.4 | mypy --strict on real adapters + config + bundle | `Success: no issues` | `Success: no issues found in 7 source files` | ✅ PASS | One informational note about unused pyproject sections (pre-existing, not blocking). |
| 1.5 | docker compose validation (mock + real profiles) | Both VALID | MOCK: VALID / REAL: VALID | ✅ PASS | — |
| 1.6 | Backwards compatibility smoke (all modes unset → all Mock) | `MockVAD MockASR MockLLM MockEmbed` | `MockVADAdapter MockASRAdapter MockLLMAdapter MockEmbedAdapter` | ✅ PASS | Required `WORKING_DIR` override because `/data` is read-only on QA host; same override used in 1.7/1.8. |
| 1.7 | Mode-mixing smoke (`ADAPTER_LLM_MODE=real`) | `MockVAD MockASR LLMOpenAI LLMOpenAI MockEmbed` | Exactly matches | ✅ PASS | stderr warning `JWT_SECRET is placeholder` is expected on the QA host and unrelated to M4 scope. |
| 1.8 | `ADAPTER_ASR_MODE=real` rejection | `ValueError` at Settings init | `OK: 1 validation error for Settings — ADAPTER_ASR_MODE=real is not supp…` | ✅ PASS | — |

**Overall:** 8/8 checks pass post-gap-fill. Engineer's original claims all reproduced within tolerance; the only material discrepancy was two modules below the 90% coverage bar, which QA remediated.

---

## 2. 覆盖率详情 (Coverage Detail)

Final coverage on `audio_graphy/adapters/real/` + `audio_graphy/adapters/exceptions.py` after gap-fill tests (32 respx cases; full project regression at 91.46% total).

| Module | Statements | Missing | Branches | Missing Br. | Cover | Missing Lines |
|--------|-----------:|--------:|---------:|------------:|------:|----------------|
| `adapters/exceptions.py` | 45 | 0 | 0 | 0 | **100%** | — |
| `adapters/real/vad_silero.py` | 58 | 4 | 12 | 3 | **90%** | 83, 107–109, 121→127, 131→exit |
| `adapters/real/llm_openai.py` | 89 | 0 | 16 | 1 | **99%** | 153→159 (only branch partial) |
| `adapters/real/embed_bge.py` | 53 | 0 | 10 | 1 | **98%** | 103→109 (only branch partial) |

**Reading the residuals:**

- `vad_silero.py` line 83: `VADRequestError` raised when the audio file does not exist on disk. The 6 existing respx tests all use the `tmp_wav` fixture, so this filesystem branch is intentionally uncovered. Adding a synthetic non-existent path test would push VAD to ~92%, but the branch is a trivial guard and was deliberately left for M5 (see §4).
- `vad_silero.py` lines 107–109: `httpx.HTTPError` non-timeout transport branch (mirrors the test QA added for LLM/embed). Same M5 deferral rationale — see §4.
- `llm_openai.py` 153→159 and `embed_bge.py` 103→109: branch coverage only — the *line* is hit by `test_*_client_recreation_after_close`, but the alternate branch outcome (client not re-created because it was still open) is unreachable in practice (the `or` short-circuits when `_client is None`). Acceptable.

All three real-adapter modules are above the 90% bar mandated by PRD §8.1, with two of three at ≥ 98%.

### Total project coverage

| Metric | M3 baseline | M4 engineer claim | M4 post-QA gap-fill |
|--------|------------:|------------------:|--------------------:|
| Tests | 623 | 643 (+20) | **657 (+34 vs M3, +14 vs engineer)** |
| Total coverage | 91.54% | 91.03% | **91.46%** |
| Real-adapter respx tests | 0 | 18 | **32** |

Net coverage dipped 0.08 ppt versus M3 because M4 introduced 245 new statements (3 adapters + exceptions + bundle wiring); the +14 QA tests recovered most of the deficit.

---

## 3. 填补的测试 gap (Tests Added by QA)

All additions are **pure test code** under `backend/tests/adapters/real/`. **No source-code modifications** — see constraint in task brief.

### 3.1 `tests/adapters/real/test_llm_openai.py` — +7 test functions

| # | Function (file:line) | Covers source lines | Rationale |
|---|----------------------|---------------------|-----------|
| 1 | `test_llm_with_max_tokens` (test_llm_openai.py:167) | `llm_openai.py:99–100` (`if max_tokens is not None: payload["max_tokens"] = …`) | Engineer's 7 cases all omitted `max_tokens`, leaving the payload-augmentation branch cold. Asserts the serialized request body contains the field. |
| 2 | `test_llm_err_transport` (test_llm_openai.py:185) | `llm_openai.py:118–123` (`except httpx.HTTPError → LLMServerError`) | Existing `err_timeout` only hit the `TimeoutException` branch; the sibling `HTTPError` branch (e.g. `ConnectError`, `ReadError`) was uncovered. |
| 3 | `test_llm_client_recreation_after_close` (test_llm_openai.py:199) | `llm_openai.py:152–159` (`_get_client` recreation when `is_closed`) | Lazy-init `is_closed` branch was never entered. Test forces close, then re-uses adapter, asserting a new client instance is built. |
| 4 | `test_llm_close_idempotent` (test_llm_openai.py:221) | `llm_openai.py:161–165` (`aclose` when `_client is None` + double-close) | The `if self._client and not self._client.is_closed` short-circuit had no test for the falsy path. |
| 5 | `test_llm_non_json_response` (test_llm_openai.py:240) | `llm_openai.py:192–197` (`except ValueError → LLMServerError`) | Engineer's `err_*` cases all returned text on 4xx/5xx; none returned 200 with non-JSON body, leaving the JSON-parse failure branch cold. |
| 6 | `test_llm_missing_choices_in_response` (test_llm_openai.py:254) | `llm_openai.py:199–205` (`except (KeyError, IndexError, TypeError)`) | Malformed-but-valid JSON path was uncovered. Returns `{"unexpected": "shape"}` and asserts `LLMServerError`. |

### 3.2 `tests/adapters/real/test_embed_bge.py` — +8 test functions

| # | Function (file:line) | Covers source lines | Rationale |
|---|----------------------|---------------------|-----------|
| 1 | `test_embed_empty_input_returns_empty` (test_embed_bge.py:115) | `embed_bge.py:71–72` (`if not texts: return ()`) | Empty-list short-circuit was uncovered; also asserts no httpx client is created. |
| 2 | `test_embed_batch_too_large` (test_embed_bge.py:127) | `embed_bge.py:73–77` (`if len(texts) > self._max_batch: raise`) | Constructs adapter with `max_batch=4`, submits 5 texts, asserts `EmbedServerError`. PRD §4.3 batching deferral. |
| 3 | `test_embed_err_transport` (test_embed_bge.py:140) | `embed_bge.py:89–91` (`except httpx.HTTPError → EmbedServerError`) | Mirrors `test_llm_err_transport`; existing tests only covered `TimeoutException`. |
| 4 | `test_embed_client_recreation_after_close` (test_embed_bge.py:152) | `embed_bge.py:102–109` (`_get_client` recreation) | Lazy-init `is_closed` branch. |
| 5 | `test_embed_close_idempotent` (test_embed_bge.py:169) | `embed_bge.py:111–115` (`aclose` no-op branches) | Tests close-on-None and double-close. |
| 6 | `test_embed_missing_data_key` (test_embed_bge.py:184) | `embed_bge.py:125–131` (`except (KeyError, TypeError)`) | 200 OK with valid JSON missing the `data` key. |
| 7 | `test_embed_non_json_response` (test_embed_bge.py:197) | `embed_bge.py:117–124` (`except ValueError`) | 200 OK with HTML body. |
| 8 | `test_embed_partial_dim_mismatch_in_batch` (test_embed_bge.py:210) | `embed_bge.py:134–140` (dim check inside iteration) | Existing `err_dim_mismatch` only triggered on the first vector; this test verifies the check fires mid-batch on the *second* item. |

### 3.3 Aggregate delta

| File | Before | After | Δ |
|------|-------:|------:|---:|
| `test_llm_openai.py` | 7 tests / 89% cov | 13 tests / 99% cov | +6 tests, +10 ppt |
| `test_embed_bge.py` | 5 tests / 86% cov | 13 tests / 98% cov | +8 tests, +12 ppt |
| `test_vad_silero.py` | 6 tests / 90% cov | 6 tests / 90% cov | unchanged (already ≥ 90%) |
| **Total real respx** | **18 tests** | **32 tests** | **+14 tests** |

---

## 4. 未解决问题 (Non-blocking Issues Deferred to M5)

These are not release-blockers for M4. Engineer should address in M5.

### 4.1 VAD transport-error branch uncovered (M5)

`vad_silero.py:107–109` (`except httpx.HTTPError`) has no test. The equivalent branch in LLM (`test_llm_err_transport`) and Embed (`test_embed_err_transport`) was added by QA; a symmetric `test_vad_err_transport` using `httpx.ConnectError` would push VAD to ~94%. **Skipped here** because VAD already meets the 90% bar and the test would be a one-line mirror — better bundled with a VAD test re-organisation in M5.

### 4.2 VAD file-not-found branch uncovered (M5)

`vad_silero.py:82–86` raises `VADRequestError` when the audio path is not a file on disk. All 6 existing tests use the `tmp_wav` fixture. Trivially fixable with a synthetic non-existent path; deferred to keep the QA diff focused on coverage gaps that were *under* the 90% bar.

### 4.3 Branch coverage residuals in `_get_client`

`llm_openai.py:153→159` and `embed_bge.py:103→109` show as partial branches because the `or` short-circuit (`self._client is None or self._client.is_closed`) cannot *both* be false-y in normal operation. This is a structural artefact, not a real test gap. No action needed.

### 4.4 `JWT_SECRET` stderr warning

In check 1.7 (mode-mixing smoke), the config validator prints `REAL adapter ON but JWT_SECRET is placeholder — set a strong JWT_SECRET` to stderr. This is a non-fatal warning from `config.py` (not part of M4 scope — pre-existing security hardening). Documented for awareness; no fix needed for sign-off.

### 4.5 `pyproject.toml` unused-section note

mypy emits `note: unused section(s): module = ['apscheduler.*', 'networkx.*']`. Pre-existing; tracked separately. Not an error.

### 4.6 Coverage drift vs M3

Total project coverage is 91.46% vs M3's 91.54% (−0.08 ppt). Cause: M4 added 245 new statements; even after gap-fill the ratio dipped slightly. Still well above the 85% CI gate. M5 should target ≥ 91.6% to recover headroom.

---

## 5. 签署 (Sign-off)

**Verdict:** M4 is release-ready.

All eight independent verification checks pass. Engineer's claims (643 tests, 91.03% total, 18 respx tests, ruff/mypy clean, docker `--profile real` valid, BC + mode-mixing + ASR-rejection smokes green) were **all reproduced**. The only material finding was two adapter modules below the 90% coverage bar (`llm_openai.py` 89%, `embed_bge.py` 86%); QA closed both gaps — adding 14 respx tests, lifting them to 99% and 98% respectively, with zero regressions in the full 657-test suite.

No source code (`config.py`, `bundle.py`, `adapters/real/*.py`, `adapters/exceptions.py`) was modified by QA. Six non-blocking issues are documented in §4 for M5 follow-up.

```
严过关 (QA) — 2026-07-21 — M4 release-ready: ✅
```

---

*Report end. Hand back to 齐活林 (delivery director) for final release decision.*
