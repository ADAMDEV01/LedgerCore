from typing import Optional


class DomainError(Exception):
    """Business-rule failure that maps to an HTTP response. Services never import FastAPI."""

    status_code = 400

    def __init__(self, detail: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers


class AuthenticationError(DomainError):
    status_code = 401


class PermissionDenied(DomainError):
    status_code = 403


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class LedgerValidationError(DomainError):
    status_code = 422


class TooManyRequests(DomainError):
    status_code = 429


class SystemAccountMissing(DomainError):
    status_code = 500


class AppendOnlyViolation(RuntimeError):
    """Raised when code tries to UPDATE/DELETE an append-only ledger row through the ORM."""
