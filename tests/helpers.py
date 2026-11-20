"""Shared helpers for the unittest suite: in-memory case builders and a tiny
file-based CLI runner.  All data is fictional and generated locally.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any

from solution.validation import load_case


def write_case(tmp: str, name: str, data: dict[str, Any]) -> str:
    path = os.path.join(tmp, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    return path


def load(data: dict[str, Any]):
    return load_case(json.loads(json.dumps(data, ensure_ascii=False)))


def cli(args: list[str]) -> tuple[int, dict[str, Any]]:
    proc = subprocess.run(
        [sys.executable, "-m", "solution", *args],
        capture_output=True, text=True, encoding="utf-8",
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else {}
    return proc.returncode, out


def fresh_db(tmp: str, name: str = "ledger.sqlite") -> str:
    path = os.path.join(tmp, name)
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    return path


def make_tmp() -> str:
    return tempfile.mkdtemp(prefix="ledger_test_")
