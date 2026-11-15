"""Errores de dominio con código estable. Se serializan como {"error": code, "message": ...}."""

from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(DomainError):
    status_code = 404
    code = "NOT_FOUND"


class Conflict(DomainError):
    status_code = 409
    code = "CONFLICT"


class InsufficientFunds(DomainError):
    status_code = 402
    code = "INSUFFICIENT_FUNDS"


class FeatureDisabled(DomainError):
    status_code = 403
    code = "FEATURE_DISABLED"


class ChargerFault(DomainError):
    status_code = 503
    code = "CHARGER_FAULT"
