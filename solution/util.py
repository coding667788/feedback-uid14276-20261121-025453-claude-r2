"""Deterministic canonical-JSON and stable receipt helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(obj: Any) -> str:
    """Deterministic JSON encoding: sorted keys, no whitespace, no ASCII escape."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: Any, prefix: str = "", length: int = 16) -> str:
    """Stable hex id derived from the canonical form of ``obj``."""
    h = hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()
    return f"{prefix}{h[:length]}" if prefix else h[:length]


def receipt_id(kind: str, body: dict[str, Any]) -> str:
    """Stable idempotency receipt for a command body."""
    return digest({"kind": kind, "body": body}, prefix=f"{kind}_")
