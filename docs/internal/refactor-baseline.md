# Test baseline

Reference point for the open-source hardening work. Re-run the same command
after each step and compare against these numbers.

```bash
cd backend && ./.venv/bin/pytest tests/ --no-cov -q
```

## 2026-07-31 — after `fix(security): remove the mock-mode password bypass`

```
1 failed, 2809 passed, 4 skipped, 189 errors in 290.08s
```

**Everything that does not touch MySQL passes.** All 190 non-passing results
come from the same source and none of them is an assertion failure:

| Count | Kind | Cause |
|---|---|---|
| 189 | error at fixture setup | `tests/{models,storage,core}/conftest.py` connect to MySQL on `127.0.0.1:3307` with no availability probe. Against a stale local `audiography_test` schema, `Base.metadata.drop_all` raises MySQL error 1091 (`Can't DROP ...; check that it exists`). |
| 1 | "failure" that is really a warning | `PytestUnraisableExceptionWarning` for aiomysql connections the same fixtures leave unclosed. `filterwarnings = ["error"]` promotes it to a failure. |

Both are properties of the local MySQL fixtures, not of the code under test. CI
runs against a fresh `mysql:8.0` service container and does not hit them.

To reproduce a clean local run, recreate the test database first:

```sql
DROP DATABASE IF EXISTS audiography_test;
CREATE DATABASE audiography_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Two follow-ups are tracked against this:

- give the three conftest engine fixtures the availability probe that
  `tests/storage/test_llm_cache_store_mysql.py` already uses, so a missing or
  stale MySQL skips instead of erroring
- close the aiomysql engines in fixture teardown so the run is warning-clean

## Offline subset

These suites use SQLite and run without any external service:

```bash
cd backend && ./.venv/bin/pytest \
  tests/api tests/services tests/auth tests/config tests/regression \
  tests/analytics tests/adapters tests/eval tests/prompts tests/tags \
  --no-cov -q
```
