"""Pure ledger semantics: state reconstruction, invariants, closure, eligibility.

The engine never touches SQLite; both the planner and the apply path reuse the
same code so planning and committing cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LedgerError
from .model import App, Approval, Case, Constraint, Txn
from .validation import dep_keys


@dataclass(frozen=True)
class Violation:
    constraint_id: str
    key: str
    value: object


# --------------------------------------------------------------------------- #
# invariant evaluation
# --------------------------------------------------------------------------- #
def evaluate_constraints(state: dict[str, object],
                         constraints: tuple[Constraint, ...]) -> list[Violation]:
    """Return all violated constraint groups against ``state`` (never publishes)."""
    bad: list[Violation] = []
    for c in constraints:
        if state[c.active_key] not in c.active_values:
            continue
        for r in c.requires:
            if state[r.key] not in r.allowed:
                bad.append(Violation(c.constraint_id, r.key, state[r.key]))
    return bad


def assert_invariants(state: dict[str, object], constraints: tuple[Constraint, ...],
                      stage: str) -> None:
    bad = evaluate_constraints(state, constraints)
    if bad:
        v = bad[0]
        raise LedgerError(
            "E_INVARIANT",
            f"constraint '{v.constraint_id}' violated at {stage}: key '{v.key}'={v.value!r}",
            at="/state",
            details={"constraint_id": v.constraint_id, "key": v.key, "stage": stage},
        )


# --------------------------------------------------------------------------- #
# world state
# --------------------------------------------------------------------------- #
class World:
    """Mutable simulation over the set of still-effective (active) transactions.

    Values are reconstructed from initial values plus the newest still-active
    write per key.  Per-key versions count every write event (commits and
    compensation writes); rolling a transaction back restores the previous
    effective transaction's value while the version keeps increasing.
    """

    def __init__(self, case: Case, version_override: dict[str, int] | None = None):
        self.case = case
        self.active: set[str] = {t.txn_id for t in case.transactions if not t.rolled_back}
        self._by_id = {t.txn_id: t for t in case.transactions}
        replayed = self._replay_versions()
        # When the database supplies authoritative current versions (e.g. a
        # concurrent client bumped a key), they win over the pure history
        # replay; otherwise concurrent writes would be invisible to planning.
        if version_override is not None:
            if set(version_override) != set(replayed):
                raise LedgerError(
                    "E_DB_STATE",
                    "stored version table does not cover the same keys as history",
                    at="/db/key_version",
                )
            self.versions = dict(version_override)
        else:
            self.versions = replayed
        self.state = self.effective_state()

    # -- history ----------------------------------------------------------- #
    def _replay_versions(self) -> dict[str, int]:
        versions = {k: 0 for k in self.case.keys}
        events: list[tuple[int, int, str]] = []
        for idx, t in enumerate(self.case.transactions):
            events.append((t.committed_at, idx, "commit"))
            if t.rolled_back:
                events.append((t.rollback_at, idx, "rollback"))
        events.sort(key=lambda e: (e[0], 0 if e[2] == "commit" else 1))
        for _when, idx, kind in events:
            for w in self.case.transactions[idx].writes:
                versions[w.key] += 1
        return versions

    # -- values ------------------------------------------------------------ #
    def effective_state(self) -> dict[str, object]:
        state = {k: spec.initial_value for k, spec in self.case.keys.items()}
        for t in sorted(
            (self._by_id[tid] for tid in self.active), key=lambda t: t.committed_at
        ):
            for w in t.writes:
                state[w.key] = w.new_value
        return state

    def refresh(self) -> None:
        self.state = self.effective_state()

    # -- rollback mechanics ------------------------------------------------ #
    def rollbackable(self) -> set[str]:
        """Transactions whose rollback prefix is currently satisfied: no still
        *later* effective transaction writes any of their keys."""
        out: set[str] = set()
        active_txns = [self._by_id[tid] for tid in self.active]
        for t in active_txns:
            blocked = any(
                u is not t and u.committed_at > t.committed_at and (u.keys & t.keys)
                for u in active_txns
            )
            if not blocked:
                out.add(t.txn_id)
        return out

    def blockers(self, txn_id: str) -> list[str]:
        """Still-active later writers of any key written by ``txn_id``."""
        txn = self._by_id[txn_id]
        return sorted(
            u.txn_id for u in self.case.transactions
            if u.txn_id in self.active
            and u.committed_at > txn.committed_at
            and (u.keys & txn.keys)
        )

    def apply_rollback(self, txn_id: str) -> None:
        if txn_id not in self.active:
            raise LedgerError("E_TXN_STATE", f"transaction '{txn_id}' is not active",
                              at=f"/plan/steps/{txn_id}")
        if txn_id not in self.rollbackable():
            raise LedgerError("E_PREFIX",
                              f"transaction '{txn_id}' has a later writer still active",
                              at=f"/plan/steps/{txn_id}")
        t = self._by_id[txn_id]
        # Snapshot first: an invariant failure must leave the world untouched
        # (no partially published intermediate state).
        snap_active = set(self.active)
        snap_versions = dict(self.versions)
        snap_state = dict(self.state)
        for w in t.writes:
            self.versions[w.key] += 1
        self.active.discard(txn_id)
        self.refresh()
        try:
            assert_invariants(self.state, self.case.constraints,
                              f"after rollback of {txn_id}")
        except LedgerError:
            self.active = snap_active
            self.versions = snap_versions
            self.state = snap_state
            raise

    # -- closure ----------------------------------------------------------- #
    def closure(self, target_txn_id: str) -> frozenset[str]:
        """Mandatory full-rollback prefix of ``target_txn_id``.

        Includes the target and every still-effective transaction that is a
        later *writer* of any key reachable through the write chain (transitive).
        Invariant read-only keys do not extend the prefix: a transaction that
        only touches such a key is an optional outside-closure compensation.
        """
        if target_txn_id not in self.active:
            raise LedgerError("E_TXN_STATE",
                              f"target transaction '{target_txn_id}' is not active",
                              at="/target_txn_id")
        seen: set[str] = set()
        frontier = [target_txn_id]
        while frontier:
            tid = frontier.pop()
            if tid in seen:
                continue
            seen.add(tid)
            t = self._by_id[tid]
            for u in self.case.transactions:
                if u.txn_id in self.active and u.committed_at > t.committed_at \
                        and (u.keys & t.keys):
                    frontier.append(u.txn_id)
        return frozenset(seen)

    # -- snapshots for planning / apply ------------------------------------ #
    def dep_versions(self, txn: Txn) -> dict[str, int]:
        dep = dep_keys(txn, self.case.constraints)
        return {k: self.versions[k] for k in sorted(dep)}


# --------------------------------------------------------------------------- #
# approval eligibility
# --------------------------------------------------------------------------- #
def approval_eligible(approval: Approval, app: App, txn: Txn,
                      world: World, *, now: int) -> tuple[bool, str | None]:
    """Check every static + current-state condition of an approval.

    Per-key reviewer adjacency and single-consumption are handled by the
    planner / apply layer because they depend on the running sequence.
    """
    if approval.revoked:
        return False, "approval revoked"
    if approval.app_id != app.app_id:
        return False, "approval belongs to another application"
    if approval.txn_id != txn.txn_id:
        return False, "approval signed for another transaction"
    if approval.policy_generation != world.case.policy_generation:
        return False, "stale policy generation"
    if not (app.window_start <= now <= app.window_end):
        return False, "outside application time window"
    if not (app.window_start <= approval.decided_at <= app.window_end):
        return False, "decision issued outside application time window"
    dep = dep_keys(txn, world.case.constraints)
    if set(approval.bound_versions) != set(dep):
        return False, "bound key set does not match transaction dependency keys"
    for k, v in approval.bound_versions.items():
        if world.versions[k] != v:
            return False, f"bound version of '{k}' is stale"
    return True, None


def approvals_for(world: World, app: App, txn: Txn,
                  now: int) -> list[tuple[Approval, bool, str]]:
    """All approvals that reference this transaction under ``app`` with verdict."""
    out: list[tuple[Approval, bool, str]] = []
    for ap in sorted(world.case.approvals.values(), key=lambda a: a.approval_id):
        if ap.txn_id != txn.txn_id or ap.app_id != app.app_id:
            continue
        ok, why = approval_eligible(ap, app, txn, world, now=now)
        out.append((ap, ok, why or ""))
    return out
