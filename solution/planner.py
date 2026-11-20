"""Compensation planning.

At a fixed logical time the planner searches for the executable complete
rollback prefix of a target transaction.  Objective order:

1. minimum cost of *extra* (outside-closure) compensated transactions,
   weighted by their input ``weight``;
2. minimum total number of compensation steps;
3. lexicographic tie-break on the sequence of ``(txn_id, approval_id)``.

The search is DFS with branch-and-bound and memoisation.  ``brute_force_plan``
is an independent exhaustive enumerator used by the test suite to cross-check
the search on small cases.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace

from .engine import World, approvals_for
from .errors import LedgerError
from .model import App, Approval, Case, Txn
from .validation import dep_keys

# Search safety bound for the given input scale (<= 24 effective transactions).
MAX_NODES = 200_000


@dataclass(frozen=True)
class PlanStep:
    seq: int
    txn_id: str
    approval_id: str
    reviewer: str
    in_closure: bool
    weight: int
    dep_versions: dict[str, int]          # versions pinned before the step


@dataclass
class PlanResult:
    ok: bool
    app_id: str
    target_txn_id: str
    logical_time: int
    policy_generation: int
    closure: tuple[str, ...] = ()
    steps: tuple[PlanStep, ...] = ()
    outside_cost: int = 0
    tiebreak: tuple[tuple[str, str], ...] = ()
    reason: dict | None = None

    def to_dict(self) -> dict:
        if not self.ok:
            return {
                "status": "unsatisfiable",
                "app_id": self.app_id,
                "target_txn_id": self.target_txn_id,
                "logical_time": self.logical_time,
                "policy_generation": self.policy_generation,
                "closure": list(self.closure),
                "reason": self.reason,
            }
        return {
            "status": "ok",
            "app_id": self.app_id,
            "target_txn_id": self.target_txn_id,
            "logical_time": self.logical_time,
            "policy_generation": self.policy_generation,
            "closure": list(self.closure),
            "outside_cost": self.outside_cost,
            "step_count": len(self.steps),
            "tiebreak": [list(pair) for pair in self.tiebreak],
            "steps": [
                {
                    "seq": s.seq,
                    "txn_id": s.txn_id,
                    "approval_id": s.approval_id,
                    "reviewer": s.reviewer,
                    "in_closure": s.in_closure,
                    "weight": s.weight,
                    "dep_versions": s.dep_versions,
                }
                for s in self.steps
            ],
        }


@dataclass
class _NodeBudget:
    nodes: int = 0
    failures: list[tuple] = field(default_factory=list)


class Planner:
    def __init__(self, case: Case, app: App, *, allow_undone_target: bool = False,
                 version_override: dict[str, int] | None = None):
        self.case = case
        self.app = app
        self.now = case.logical_time
        self.world = World(case, version_override=version_override)
        if app.target_txn_id not in self.world.active and not allow_undone_target:
            raise LedgerError("E_TXN_STATE",
                              f"target transaction '{app.target_txn_id}' is not active",
                              at="/target_txn_id")
        if app.target_txn_id in self.world.active:
            self.closure: frozenset[str] = self.world.closure(app.target_txn_id)
        else:
            self.closure = frozenset()
        self._by_id = {t.txn_id: t for t in case.transactions}
        # Equivalent states (same active set + same per-key last reviewer)
        # share one memo entry; the stored objective is relative to the state,
        # so it is independent of how the state was reached.
        self.memo: dict[tuple, tuple | None] = {}
        self.budget = _NodeBudget()

    # ------------------------------------------------------------------ #
    def _eligible_approvals(self, txn: Txn) -> list[Approval]:
        out: list[Approval] = []
        for ap, ok, _why in approvals_for(self.world, self.app, txn, self.now):
            if ok:
                out.append(ap)
        out.sort(key=lambda a: a.approval_id)
        return out

    def _memo_key(self, last_reviewer: dict[str, str],
                  closure_remaining: frozenset[str]) -> tuple:
        return (frozenset(self.world.active),
                tuple(sorted(last_reviewer.items())),
                tuple(sorted(closure_remaining)))

    def _solve(self, depth: int,
               last_reviewer: dict[str, str],
               closure_remaining: frozenset[str]) -> tuple[PlanStep, ...] | None:
        self.budget.nodes += 1
        if self.budget.nodes > MAX_NODES:
            raise LedgerError("E_PLAN_TOO_LARGE", "search exceeded node budget", at="/plan")

        # Terminal only when EVERY closure transaction is undone.  Checking the
        # target alone would stop early while later closure writers stay active,
        # publishing an incomplete rollback prefix.
        if not closure_remaining:
            return ()

        key = self._memo_key(last_reviewer, closure_remaining)
        if key in self.memo:
            return self.memo[key]

        candidates = sorted(self.world.rollbackable(), key=str)
        best: tuple[PlanStep, ...] | None = None
        best_key: tuple | None = None

        for tid in candidates:
            txn = self._by_id[tid]
            in_closure = tid in self.closure
            extra_cost = 0 if in_closure else txn.weight

            elig = self._eligible_approvals(txn)
            if not elig:
                self._record_failure(depth, txn, "E_NO_APPROVAL")
                continue

            workable = [
                ap for ap in elig
                if all(last_reviewer.get(k) != ap.reviewer for k in txn.keys)
            ]
            if not workable:
                self.budget.failures.append(
                    (depth, tid, "E_ADJACENCY",
                     [a.approval_id for a in elig]))
                continue

            snap_versions = dict(self.world.versions)
            dep_snap = {k: snap_versions[k]
                        for k in sorted(dep_keys(txn, self.case.constraints))}

            for ap in workable:
                touched = sorted(txn.keys)
                prev_rev = {k: last_reviewer.get(k) for k in touched}
                try:
                    self.world.apply_rollback(tid)
                except LedgerError as exc:
                    if exc.code == "E_INVARIANT":
                        self.budget.failures.append(
                            (depth, tid, "E_INVARIANT", exc.details))
                        self._undo(tid, snap_versions)
                        continue
                    raise
                for k in touched:
                    last_reviewer[k] = ap.reviewer
                next_remaining = (closure_remaining - {tid}
                                  if tid in closure_remaining
                                  else closure_remaining)

                tail = self._solve(depth + 1, last_reviewer, next_remaining)

                # undo the simulated step, restoring exact state
                self.world.active.add(tid)
                self.world.versions = dict(snap_versions)
                self.world.refresh()
                for k in touched:
                    if prev_rev[k] is None:
                        last_reviewer.pop(k, None)
                    else:
                        last_reviewer[k] = prev_rev[k]  # type: ignore[assignment]

                if tail is None:
                    continue

                head = PlanStep(
                    seq=0,  # renumbered once the final sequence is known
                    txn_id=tid,
                    approval_id=ap.approval_id,
                    reviewer=ap.reviewer,
                    in_closure=in_closure,
                    weight=txn.weight,
                    dep_versions=dep_snap,
                )
                cand = (head, *tail)
                tail_cost = sum(s.weight for s in tail if not s.in_closure)
                cand_key = (extra_cost + tail_cost, len(cand),
                            tuple((s.txn_id, s.approval_id) for s in cand))
                if best_key is None or cand_key < best_key:
                    best, best_key = cand, cand_key

        self.memo[key] = best
        return best

    def _undo(self, tid: str, snap_versions: dict[str, int]) -> None:
        self.world.active.add(tid)
        self.world.versions = dict(snap_versions)
        self.world.refresh()

    def _record_failure(self, depth: int, txn: Txn, code: str) -> None:
        considered = [
            ap.approval_id
            for ap in self.case.approvals.values()
            if ap.txn_id == txn.txn_id and ap.app_id == self.app.app_id
        ]
        self.budget.failures.append((depth, txn.txn_id, code, sorted(considered)))

    # ------------------------------------------------------------------ #
    def run(self) -> PlanResult:
        steps = self._solve(0, {}, frozenset(self.closure))
        base = dict(
            app_id=self.app.app_id,
            target_txn_id=self.app.target_txn_id,
            logical_time=self.case.logical_time,
            policy_generation=self.case.policy_generation,
            closure=tuple(sorted(self.closure)),
        )
        if steps is None:
            return PlanResult(ok=False, reason=self._explain(), **base)  # type: ignore[arg-type]

        renumbered = tuple(
            replace(s, seq=i + 1) for i, s in enumerate(steps)
        )
        outside = sum(s.weight for s in renumbered if not s.in_closure)
        return PlanResult(
            ok=True,
            steps=renumbered,
            outside_cost=outside,
            tiebreak=tuple((s.txn_id, s.approval_id) for s in renumbered),
            **base,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ #
    def _explain(self) -> dict:
        """Explain the earliest missing approval / blocking prefix transaction."""
        if not self.budget.failures:
            return {
                "code": "E_NO_APPROVAL",
                "message": "no rollback sequence exists",
                "at": "/plan",
                "details": {},
            }
        depth, tid, code, extra = min(
            self.budget.failures, key=lambda f: (f[0], f[1], f[2])
        )
        detail: dict = {"depth": depth, "txn_id": tid, "reason": code}
        if code == "E_NO_APPROVAL":
            detail["approval_ids_considered"] = extra
            msg = f"no qualified approval for required transaction '{tid}'"
        elif code == "E_ADJACENCY":
            detail["blocked_approval_ids"] = extra
            msg = (f"all approvals for '{tid}' reuse the reviewer of an adjacent "
                   f"compensation step on one of its keys")
        elif code == "E_INVARIANT":
            detail["violation"] = extra
            msg = f"rolling back '{tid}' here violates a service invariant"
        else:  # pragma: no cover - defensive
            msg = f"transaction '{tid}' cannot be compensated"

        # surface a blocking later-writer chain when the prefix is stuck
        blockers = self._blockers(tid)
        if blockers:
            detail["blocker_txn_ids"] = blockers
        # when the earliest failure is the unrollable target itself, show its
        # blockers explicitly even if another code was recorded
        if not detail.get("blocker_txn_ids"):
            target_blockers = self._blockers(self.app.target_txn_id)
            if target_blockers:
                detail["target_blocker_txn_ids"] = target_blockers
        return {"code": code, "message": msg, "at": "/plan", "details": detail}

    def _blockers(self, tid: str) -> list[str]:
        # Diagnostics run against the initial world (the search always restores
        # state on the way back up), so blockers reflect the request as issued.
        initial = World(self.case)
        return initial.blockers(tid)


# --------------------------------------------------------------------------- #
# independent exhaustive enumerator (cross-check, small cases only)
# --------------------------------------------------------------------------- #
def brute_force_plan(case: Case, app: App, *, limit: int = 8) -> PlanResult:
    """Enumerate every valid rollback order.  Only for cases with at most
    ``limit`` active transactions; used by tests to cross-check the DFS."""
    world = World(case)
    closure = world.closure(app.target_txn_id)
    active = sorted(world.active)
    if len(active) > limit:
        raise LedgerError("E_PLAN_TOO_LARGE",
                          f"brute force supports at most {limit} active transactions",
                          at="/plan")
    by_id = {t.txn_id: t for t in case.transactions}
    last_reviewer: dict[str, str] = {}
    best: tuple | None = None
    best_key: tuple | None = None

    for perm in itertools.permutations(active):
        w = World(case)
        lr: dict[str, str] = {}
        steps: list[PlanStep] = []
        outside = 0
        valid = True
        for i, tid in enumerate(perm):
            # permutation must only roll back prefix-satisfiable transactions
            if tid not in w.rollbackable():
                valid = False
                break
            txn = by_id[tid]
            elig = [ap for ap, ok, _ in approvals_for(w, app, txn, case.logical_time) if ok]
            elig = [ap for ap in elig if all(lr.get(k) != ap.reviewer for k in txn.keys)]
            if not elig:
                valid = False
                break
            ap = min(elig, key=lambda a: a.approval_id)
            dep_snap = {k: w.versions[k]
                        for k in sorted(dep_keys(txn, case.constraints))}
            try:
                w.apply_rollback(tid)
            except LedgerError as exc:
                if exc.code == "E_INVARIANT":
                    valid = False
                    break
                raise
            for k in txn.keys:
                lr[k] = ap.reviewer
            in_closure = tid in closure
            if not in_closure:
                outside += txn.weight
            steps.append(PlanStep(i + 1, tid, ap.approval_id, ap.reviewer,
                                  in_closure, txn.weight, dep_snap))
            if not (closure & w.active):
                break
        if not valid or (closure & w.active):
            continue
        key = (outside, len(steps),
               tuple((s.txn_id, s.approval_id) for s in steps))
        if best_key is None or key < best_key:
            best_key, best = key, tuple(steps)

    base = dict(
        app_id=app.app_id,
        target_txn_id=app.target_txn_id,
        logical_time=case.logical_time,
        policy_generation=case.policy_generation,
        closure=tuple(sorted(closure)),
    )
    if best is None:
        return PlanResult(ok=False, reason={"code": "E_NO_APPROVAL",
                                            "message": "no rollback sequence exists",
                                            "at": "/plan", "details": {}},
                          **base)  # type: ignore[arg-type]
    return PlanResult(
        ok=True, steps=best, outside_cost=best_key[0],  # type: ignore[index]
        tiebreak=tuple((s.txn_id, s.approval_id) for s in best),
        **base,  # type: ignore[arg-type]
    )
