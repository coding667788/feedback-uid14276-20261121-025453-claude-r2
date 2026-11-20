"""Domain model dataclasses (all immutable views over the input case)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Write:
    key: str
    old_value: Any
    new_value: Any
    new_version: int


@dataclass(frozen=True)
class Txn:
    txn_id: str
    committed_at: int          # fixed logical time; unique, ascending in history
    writes: tuple[Write, ...]
    weight: int = 1
    rolled_back: bool = False  # historical state at case bootstrap
    rollback_at: int = 0       # logical time of historical rollback, 0 if none
    category: str = "default"

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(w.key for w in self.writes)


@dataclass(frozen=True)
class App:
    """A compensation request referencing one original transaction and a window."""

    app_id: str
    target_txn_id: str
    idempotency_key: str
    window_start: int
    window_end: int
    requested_at: int


@dataclass(frozen=True)
class Approval:
    """A reviewer decision.  Valid only inside the application window and when
    every signed key version matches the simulated current version at that step.
    """

    approval_id: str
    app_id: str
    txn_id: str
    reviewer: str
    bound_versions: dict[str, int]   # key -> version the reviewer signed
    decided_at: int
    policy_generation: int
    revoked: bool = False


@dataclass(frozen=True)
class Requirement:
    """One peer key allowed-set requirement inside a constraint group."""

    key: str
    allowed: tuple[Any, ...]


@dataclass(frozen=True)
class Constraint:
    """Service invariant group.

    When ``active_key`` has a value inside ``active_values`` every peer
    requirement must simultaneously hold.
    """

    constraint_id: str
    active_key: str
    active_values: tuple[Any, ...]
    requires: tuple[Requirement, ...]

    @property
    def keys(self) -> frozenset[str]:
        return frozenset([self.active_key, *(r.key for r in self.requires)])


@dataclass(frozen=True)
class KeySpec:
    key: str
    domain: tuple[Any, ...]
    initial_value: Any


@dataclass
class Case:
    case_id: str
    logical_time: int
    policy_generation: int
    keys: dict[str, KeySpec]
    constraints: tuple[Constraint, ...]
    transactions: tuple[Txn, ...]
    apps: dict[str, App]
    approvals: dict[str, Approval]
    fault: dict[str, Any] = field(default_factory=dict)

    def txn(self, txn_id: str) -> Txn:
        for t in self.transactions:
            if t.txn_id == txn_id:
                return t
        raise KeyError(txn_id)
