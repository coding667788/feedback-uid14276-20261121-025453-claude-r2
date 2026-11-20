"""Structured error codes with locatable field paths."""

from __future__ import annotations

from typing import Any


class LedgerError(Exception):
    """A domain/validation failure carrying a stable code and a field path.

    ``at`` is a JSON-pointer style path (e.g. ``/transactions/2/writes/0/key``)
    so callers can locate the offending input field.
    """

    def __init__(
        self,
        code: str,
        message: str,
        at: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.at = at
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.at is not None:
            payload["at"] = self.at
        if self.details:
            payload["details"] = self.details
        return payload


class PersistenceFault(LedgerError):
    """Raised by fault injection to simulate a storage failure before commit."""

    def __init__(self, message: str = "injected persistence fault", at: str = "/fault") -> None:
        super().__init__("E_PERSISTENCE", message, at=at)
