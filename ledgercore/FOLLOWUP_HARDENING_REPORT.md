# LedgerCore – Follow-up Security Hardening Report

**Scope:** follow-up fixes for PostgreSQL runtime privileges and concurrent refresh requests.

## Implemented

### 1. Separate PostgreSQL roles

- `ledger` remains the bootstrap administrator used only by the PostgreSQL bootstrap/role-setup service. It is not used in the API or migration service connection strings.
- `ledger_migrator` is created as `NOSUPERUSER`, `NOCREATEDB`, `NOCREATEROLE`, `NOINHERIT`, `NOREPLICATION`, and `NOBYPASSRLS`. It owns the LedgerCore schema objects and is the only role used by the Alembic migration service.
- `ledger_app` is a separate `LOGIN` role with the same restricted role attributes. Alembic grants it only `SELECT`, `INSERT`, `UPDATE`, and `DELETE` on application tables, and `USAGE`/`SELECT` on sequences. It has no `CREATE` on schema `public`, no `TRUNCATE`, no `TRIGGER`, no `REFERENCES`, and no privileges on `alembic_version`.
- Default privileges are set for future objects created by `ledger_migrator`, so later migrations do not accidentally leave the API role without runtime DML access.
- Compose now has a one-shot `role-bootstrap` service, a separate one-shot `migrator` service, and an API service that connects only as `ledger_app`. The API image no longer runs Alembic at startup.
- The role bootstrap runs on every Compose start so an existing named volume can be upgraded without deleting it. It transfers existing LedgerCore objects to `ledger_migrator` and leaves the database itself owned by the bootstrap administrator. Keep the existing `POSTGRES_PASSWORD`; add `LEDGER_MIGRATOR_PASSWORD` and `LEDGER_APP_PASSWORD` to `.env`. Do not run `docker compose down -v`.

### 2. Refresh-token concurrency and logout

- The SPA now uses one in-tab `refreshPromise`, so parallel `401` responses reuse a single refresh attempt.
- Across tabs, it uses the browser Web Locks API. A tab that waited for another tab checks `/auth/me` before attempting a refresh, so it reuses the newly refreshed cookies instead of replaying the previous refresh token. A bounded `localStorage` lease is the compatibility fallback where Web Locks are unavailable.
- Browser test `test_parallel_expired_requests_share_one_refresh_across_tabs` opens two tabs, expires the shared access cookie, fires parallel protected requests, and asserts both requests recover with exactly one `/auth/refresh` request.
- `revoke_token` now atomically locks/updates the presented token row, retrieves its `family_id` even if the token was already consumed, then revokes every token in the family in the same database transaction. If logout overlaps a refresh, the row lock serializes them; a successor inserted by the refresh is included in the family-wide revocation.
- Added `test_logout_of_consumed_refresh_token_revokes_its_entire_family`.

## Local verification performed

| Check | Result |
|---|---|
| Python compile/AST checks (`app`, tests, migrations, E2E) | PASS |
| `node --check app/static/app.js` | PASS |
| `sh -n docker/init/010-create-db-roles.sh` | PASS |
| YAML parsing (`docker-compose.yml`, GitHub Actions workflow) | PASS |
| `alembic upgrade head` on SQLite | PASS |
| `alembic check` on SQLite | PASS – no schema drift detected |
| `tests/test_auth.py`, `tests/test_migrations.py`, `tests/test_postgres_privileges.py` | 18 passed, 3 skipped |
| `tests/test_ledger.py`, `tests/test_audit.py` | 29 passed, 2 skipped |

The focused runs used `--no-cov`; they do not establish a project-wide coverage percentage.

## Not verified in this environment

- PostgreSQL is not installed/available here. The privilege assertions (`DROP TRIGGER`, `TRUNCATE`) and PostgreSQL concurrency tests are included but skipped on SQLite. Run the PostgreSQL CI job to verify them against PostgreSQL 16.
- Docker is not installed here, so Compose startup, role bootstrap against an existing volume, and the image smoke test were not run.
- Playwright browser tests were added/updated but were not executed in this run. The two-tab refresh test must pass in GitHub Actions before calling cross-tab behavior browser-verified.
- The full test suite could not be collected because `hypothesis` is missing from the current Python environment. A second run excluding property tests did not complete within the execution limit. No full-suite pass or total coverage figure is claimed.
- GitHub Actions has not been run from this environment. The deployment is not certified until PostgreSQL, Playwright, dependency audit, lint/type-check, and Docker jobs are green.

## Files changed in this follow-up

- `docker-compose.yml`, `docker/init/010-create-db-roles.sh`, `Dockerfile`, `.env.example`
- `alembic/versions/0003_restricted_app_role.py`
- `app/services/auth.py`, `app/static/app.js`
- `tests/test_auth.py`, `tests/conftest.py`, `tests/test_postgres_privileges.py`
- `e2e/test_frontend.py`, `.github/workflows/ci.yml`, `README.md`
