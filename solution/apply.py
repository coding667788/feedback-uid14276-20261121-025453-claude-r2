"""Apply path: verify a planned sequence against the live database and commit it
atomically.  Every check reuses the same engine code as the planner.
"""

from __future__ import annotations

from typing import Any

from .engine import World, approval_eligible, assert_invariants
from .errors import LedgerError
from .model import App, Approval, Case
from .planner import Planner
from .storage import Store
from .util import digest
from .validation import dep_keys


def plan_fingerprint(plan: dict[str, Any]) -> str:
    # One stable digest namespace shared by plan_id and the apply receipt, so
    # replaying the identical plan always returns the identical receipt id.
    return digest(
        {
            "app_id": plan["app_id"],
            "tiebreak": plan["tiebreak"],
            "steps": [
                {"txn_id": s["txn_id"], "approval_id": s["approval_id"],
                 "dep_versions": s["dep_versions"]}
                for s in plan["steps"]
            ],
            "policy_generation": plan["policy_generation"],
            "logical_time": plan["logical_time"],
        },
        prefix="apply_",
    )


def _require(cond: bool, code: str, message: str, at: str,
             details: dict[str, Any] | None = None) -> None:
    if not cond:
        raise LedgerError(code, message, at=at, details=details or {})


def apply_case(store: Store, case: Case, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the apply request and commit the whole sequence or nothing.

    The write lock is acquired first; the authoritative snapshot, idempotency
    lookup, fresh re-plan and all version checks then happen under that lock so
    a concurrent client cannot interleave a conflicting write.
    """
    at = "/apply"
    plan = payload.get("plan")
    _require(isinstance(plan, dict), "E_TYPE", "apply.plan must be an object", f"{at}/plan")
    app_id = plan.get("app_id")
    _require(isinstance(app_id, str), "E_TYPE", "plan.app_id must be a string",
             f"{at}/plan/app_id")
    _require(isinstance(plan.get("steps"), list) and plan["steps"],
             "E_MISSING_FIELD", "plan.steps must be a non-empty array",
             f"{at}/plan/steps")
    _require(plan.get("policy_generation") == case.policy_generation,
             "E_POLICY_GENERATION",
             "plan was produced under a different policy generation",
             f"{at}/plan/policy_generation")

    declared_id = plan.get("plan_id")
    fp = plan_fingerprint(plan)
    if declared_id is not None:
        _require(declared_id == fp, "E_PLAN_TAMPERED",
                 "plan_id does not match the plan fingerprint",
                 f"{at}/plan/plan_id")

    store.begin_write()
    try:
        # Authoritative snapshot under the write lock.
        snap = store.snapshot()
        live: Case = snap["case"]
        live.fault = case.fault
        live.logical_time = case.logical_time
        _require(app_id in live.apps, "E_REF", "plan.app_id unknown",
                 f"{at}/plan/app_id")
        app: App = live.apps[app_id]

        # Idempotent replay: same plan fingerprint always returns the same
        # receipt, even after the target transaction has already been undone.
        replayed = store.lookup_receipt("apply", fp)
        if replayed is not None:
            store.rollback_write()
            return replayed

        _require(
            app.window_start <= live.logical_time <= app.window_end,
            "E_WINDOW_CLOSED",
            "application time window is not valid at apply time",
            f"{at}/logical_time",
        )

        # Re-run the planner at the current fixed logical time and require the
        # same sequence: any intervening write/approval change surfaces here.
        fresh = Planner(live, app, version_override=snap["versions"]).run()
        _require(fresh.ok, "E_PLAN_STALE",
                 "no executable plan exists for the target transaction any more",
                 f"{at}/plan", details=fresh.reason or {})
        fresh_ids = [(s.txn_id, s.approval_id) for s in fresh.steps]
        given_ids = [(s["txn_id"], s["approval_id"]) for s in plan["steps"]]
        _require(fresh_ids == given_ids, "E_PLAN_STALE",
                 "stored plan no longer matches the optimally executable sequence",
                 f"{at}/plan/steps",
                 details={"stored": given_ids, "current": fresh_ids})
        # Same (txn, approval) sequence is not enough: every step's pinned
        # dependency versions must also match the freshly simulated ones, so a
        # concurrent bump of an invariant read-only key cannot slip through.
        fresh_pins = {
            (s.txn_id, s.approval_id): dict(s.dep_versions) for s in fresh.steps
        }
        for step in plan["steps"]:
            key = (step["txn_id"], step["approval_id"])
            _require(
                dict(step["dep_versions"]) == fresh_pins[key],
                "E_WRITE_CONFLICT",
                f"pinned dependency versions for '{step['txn_id']}' drifted",
                f"{at}/plan/steps/dep_versions",
                details={"stored": step["dep_versions"],
                         "current": fresh_pins[key]},
            )

        world = World(live, version_override=snap["versions"])
        by_id = {t.txn_id: t for t in live.transactions}
        last_reviewer: dict[str, str] = {}
        step_receipts: list[dict[str, Any]] = []
        steps_out: list[dict[str, Any]] = []
        rolled_back: list[tuple[str, int]] = []
        consumed: list[str] = []

        for i, step in enumerate(plan["steps"], start=1):
            sp = f"{at}/plan/steps/{i - 1}"
            tid = step["txn_id"]
            ap_id = step["approval_id"]
            txn = by_id.get(tid)
            _require(txn is not None, "E_REF", f"unknown transaction '{tid}'", f"{sp}/txn_id")
            _require(tid in world.active, "E_TXN_STATE",
                     f"transaction '{tid}' is not active", f"{sp}/txn_id")
            _require(tid in world.rollbackable(), "E_PREFIX",
                     f"later writer of a key of '{tid}' is still active", f"{sp}/txn_id",
                     details={"blocker_txn_ids": world.blockers(tid)})

            ap: Approval | None = live.approvals.get(ap_id)
            _require(ap is not None, "E_REF",
                     f"unknown approval '{ap_id}'", f"{sp}/approval_id")
            _require(ap.txn_id == tid and ap.app_id == app_id,
                     "E_APPROVAL_SCOPE",
                     "approval does not bind this transaction and application",
                     f"{sp}/approval_id")
            ok, why = approval_eligible(ap, app, txn, world, now=live.logical_time)
            _require(ok, "E_APPROVAL_INVALID",
                     f"approval '{ap_id}' is not usable: {why}",
                     f"{sp}/approval_id", details={"reason": why or ""})
            _require(
                all(last_reviewer.get(k) != ap.reviewer for k in txn.keys),
                "E_ADJACENCY",
                f"reviewer '{ap.reviewer}' already signed the adjacent step on a key",
                f"{sp}/approval_id",
            )

            # Every read/write dependency key (including invariant read-only
            # keys) must still carry the version the plan pinned.
            dep = dep_keys(txn, live.constraints)
            pinned = step["dep_versions"]
            _require(set(pinned) == set(dep), "E_PLAN_STALE",
                     "plan dependency key set drifted", f"{sp}/dep_versions",
                     details={"plan": sorted(pinned), "required": sorted(dep)})
            for k in dep:
                _require(
                    pinned.get(k) == world.versions[k],
                    "E_WRITE_CONFLICT",
                    f"key '{k}' changed since planning (have {world.versions[k]},"
                    f" plan pinned {pinned.get(k)})",
                    f"{sp}/dep_versions/{k}",
                    details={"current": world.versions[k], "pinned": pinned.get(k)},
                )

            world.apply_rollback(tid)  # also asserts invariants at the new state
            for k in txn.keys:
                last_reviewer[k] = ap.reviewer

            rolled_back.append((tid, live.logical_time + i))
            consumed.append(ap_id)
            step_receipts.append({
                "seq": i, "txn_id": tid, "approval_id": ap_id,
                "dep_versions": dict(pinned), "reviewer": ap.reviewer,
            })
            steps_out.append({
                "seq": i, "txn_id": tid, "approval_id": ap_id,
                "reviewer": ap.reviewer, "in_closure": step["in_closure"],
                "weight": txn.weight, "dep_versions": dict(pinned),
            })

        # Final checks: closure complete, target undone, all invariants hold.
        _require(app.target_txn_id not in world.active, "E_PREFIX",
                 "target transaction was not fully rolled back", f"{at}/plan")
        assert_invariants(world.state, live.constraints, "final state")

        response = store.apply_plan(
            plan_id=fp,
            app_id=app_id,
            steps=steps_out,
            final_state=world.state,
            new_versions=dict(world.versions),
            consumed_approvals=consumed,
            rolled_back=rolled_back,
            step_receipts=step_receipts,
            committed_at_lt=live.logical_time + len(plan["steps"]) + 1,
            fault=live.fault,
        )
        store.commit_write()
        return response
    except Exception:
        store.rollback_write()
        raise
