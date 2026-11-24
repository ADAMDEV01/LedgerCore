# Multi-tenant workspace implementation

## Account model

- An organization is a workspace boundary.
- Every user belongs to exactly one organization.
- The first registrant is created as that workspace's CFO.
- CFOs can provision VIEWER, ACCOUNTANT, and CFO users inside their own workspace.
- Email addresses remain globally unique; organization membership is not many-to-many.

## Workspace creation

`POST /api/v1/auth/register` accepts company name, full name, email, and a password of 12–128
characters. The endpoint validates the request origin, creates the organization, initializes
company-local journal/invoice/audit sequences and the standard chart of accounts, creates the
first CFO, and issues the normal HttpOnly access/refresh cookies. These operations run in one
database transaction. A failed step rolls the workspace creation back.

The browser login page exposes a matching workspace registration form. No email verification
or invitation-email workflow is included in this release.

## Tenant isolation

The following records are organization-owned:

- Accounts and accounting periods
- Document sequences
- Journal entries, seals, and ledger lines
- Invoices and invoice payments
- Audit log rows
- Idempotency records
- Users

The request session sets `app.current_organization_id` with PostgreSQL `set_config(..., true)`,
so the value is transaction-local and is reset when the transaction ends. PostgreSQL RLS
policies use that value for both row visibility and write checks. ORM loader criteria and
flush-time organization stamping provide additional protection for application queries and
new ORM objects. User lookup for authentication remains global by the globally unique email or
signed user id; user-management endpoints explicitly scope list and update operations to the
current organization.

Database triggers validate organization ownership for ledger-line account/entry references,
journal seals, invoice journal entries, invoice-payment invoice/journal references, and journal
reversals. This prevents a valid row id from another workspace being used as a foreign reference.

## Existing installations

Alembic revision `0004` creates the default organization (id 1), backfills existing users and
financial data into it, and then makes the organization key mandatory. Existing users, login
credentials, posted journal entries, invoice history, audit history, and document sequence
high-water marks are preserved. Document numbers, accounting periods, audit sequences, and
idempotency keys become organization-local.

Run `alembic upgrade head` as `ledger_migrator` before deploying the application code. The
migration does not run automatically against a production database from this feature branch.
Do not reset or recreate the existing production database volume.

## Verification

The test suite includes workspace signup, default CFO provisioning, company-local chart of
accounts, same account-code reuse across companies, separate user lists, and independent invoice
numbering. Existing accounting, audit-chain, idempotency, refresh-token concurrency, migration,
and PostgreSQL privilege tests remain part of CI.
