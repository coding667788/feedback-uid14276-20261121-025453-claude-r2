"""Command line entry point.

Usage:
    python -m solution <command> CASE.json [--db DB.sqlite]

Commands:
    validate            structural/semantic validation of a case (no db)
    bootstrap           create/load the persistent ledger from a case
    plan                plan at the case's fixed logical time
    apply               atomically commit a planned sequence
    submit-application  idempotent compensation request
    register-approval   idempotent reviewer decision
    revoke-approval     invalidate an unused approval
    inspect             read-only ledger snapshot
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any

from .apply import apply_case, plan_fingerprint
from .engine import World
from .errors import LedgerError
from .model import App, Approval
from .planner import Planner
from .storage import Store
from .validation import dep_keys, load_case

COMMANDS = {
    "validate", "bootstrap", "plan", "apply",
    "submit-application", "register-approval", "revoke-approval", "inspect",
}
DB_COMMANDS = COMMANDS - {"validate"}


def _read_case(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise LedgerError("E_FILE", f"case file not found: {path}", at="/file")
    except json.JSONDecodeError as exc:
        raise LedgerError(
            "E_JSON", f"invalid JSON: {exc.msg}",
            at=f"/line/{exc.lineno}/col/{exc.colno}",
        ) from exc


def _emit(obj: dict[str, Any], code: int = 0) -> int:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2))
    sys.stdout.write("\n")
    return code


def _parse_application(raw: dict[str, Any], at: str = "/application") -> App:
    def need(k: str) -> Any:
        if k not in raw:
            raise LedgerError("E_MISSING_FIELD", f"missing '{k}'", at=f"{at}/{k}")
        return raw[k]
    return App(
        app_id=str(need("app_id")), target_txn_id=str(need("target_txn_id")),
        idempotency_key=str(need("idempotency_key")),
        window_start=int(need("window_start")), window_end=int(need("window_end")),
        requested_at=int(need("requested_at")),
    )


def _parse_approval(raw: dict[str, Any], at: str = "/approval") -> Approval:
    def need(k: str) -> Any:
        if k not in raw:
            raise LedgerError("E_MISSING_FIELD", f"missing '{k}'", at=f"{at}/{k}")
        return raw[k]
    bv = need("bound_versions")
    if not isinstance(bv, dict):
        raise LedgerError("E_TYPE", "bound_versions must be an object", at=f"{at}/bound_versions")
    return Approval(
        approval_id=str(need("approval_id")), app_id=str(need("app_id")),
        txn_id=str(need("txn_id")), reviewer=str(need("reviewer")),
        bound_versions={str(k): int(v) for k, v in bv.items()},
        decided_at=int(need("decided_at")),
        policy_generation=int(need("policy_generation")),
    )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m solution")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("case_file")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    try:
        raw = _read_case(args.case_file)
        case = load_case(raw)

        if args.command == "validate":
            return _emit({
                "status": "valid",
                "case_id": case.case_id,
                "transaction_count": len(case.transactions),
                "key_count": len(case.keys),
                "constraint_count": len(case.constraints),
                "current_versions": World(case).versions,
            })

        if not args.db:
            raise LedgerError("E_USAGE",
                             f"command '{args.command}' requires --db DB.sqlite",
                             at="/--db")
        store = Store(args.db)
        try:
            if args.command == "bootstrap":
                snap = store.bootstrap(case)
                return _emit({
                    "status": "bootstrapped",
                    "case_id": case.case_id,
                    "current_versions": snap["versions"],
                })

            if args.command == "inspect":
                snap = store.snapshot()
                live: Case = snap["case"]
                world = World(live)
                return _emit({
                    "status": "ok",
                    "case_id": live.case_id,
                    "logical_time": live.logical_time,
                    "policy_generation": live.policy_generation,
                    "active_transactions": sorted(world.active),
                    "rolled_back_transactions": [
                        t.txn_id for t in live.transactions if t.rolled_back
                    ],
                    "current_versions": snap["versions"],
                    "effective_state": world.state,
                    "applications": [
                        {"app_id": a.app_id,
                         "target_txn_id": a.target_txn_id,
                         "window": [a.window_start, a.window_end]}
                        for a in live.apps.values()
                    ],
                    "approvals": [
                        {"approval_id": a.approval_id, "app_id": a.app_id,
                         "txn_id": a.txn_id, "reviewer": a.reviewer,
                         "revoked": a.revoked}
                        for a in live.approvals.values()
                    ],
                })

            # Every remaining command works against the live ledger state.
            snap = store.snapshot()
            live = snap["case"]

            if args.command == "plan":
                req = raw.get("plan_request") or {}
                app_id = req.get("app_id")
                if not app_id or app_id not in live.apps:
                    raise LedgerError("E_REF",
                                      "plan_request.app_id must name an application",
                                      at="/plan_request/app_id")
                result = Planner(live, live.apps[app_id],
                                 version_override=snap["versions"]).run()
                payload = result.to_dict()
                if result.ok:
                    payload["plan_id"] = plan_fingerprint(payload)
                    payload["dependency_keys"] = sorted({
                        k for s in result.steps
                        for k in dep_keys(
                            next(t for t in live.transactions if t.txn_id == s.txn_id),
                            live.constraints,
                        )
                    })
                return _emit(payload)

            if args.command == "apply":
                payload = raw.get("apply")
                if not isinstance(payload, dict) or not isinstance(payload.get("plan"), dict):
                    raise LedgerError("E_MISSING_FIELD",
                                      "apply.plan object is required", at="/apply/plan")
                # Inline case overrides (e.g. fault injection) travel with the
                # request but never mutate stored rows.
                live.fault = case.fault
                live.logical_time = case.logical_time
                return _emit(apply_case(store, live, payload))

            if args.command == "submit-application":
                app = _parse_application(raw["application"])
                return _emit(store.submit_application(app))

            if args.command == "register-approval":
                ap = _parse_approval(raw["approval"])
                # dependency-key scope validation against stored constraints
                txn = next((t for t in live.transactions if t.txn_id == ap.txn_id), None)
                if txn is None:
                    raise LedgerError("E_REF", f"unknown transaction '{ap.txn_id}'",
                                      at="/approval/txn_id")
                dep = dep_keys(txn, live.constraints)
                if set(ap.bound_versions) != dep:
                    raise LedgerError("E_APPROVAL_SCOPE",
                                      "bound_versions keys must equal dependency keys",
                                      at="/approval/bound_versions",
                                      details={"expected": sorted(dep)})
                return _emit(store.register_approval(ap))

            if args.command == "revoke-approval":
                approval_id = raw.get("approval_id")
                if not isinstance(approval_id, str):
                    raise LedgerError("E_MISSING_FIELD",
                                      "approval_id is required", at="/approval_id")
                return _emit(store.revoke_approval(approval_id))

            raise LedgerError("E_USAGE", f"unknown command {args.command}", at="/command")
        finally:
            store.close()

    except LedgerError as exc:
        return _emit({"status": "error", **exc.to_payload()}, code=2)
    except sqlite3.OperationalError as exc:
        le = LedgerError("E_SQLITE", str(exc), at="/db")
        return _emit({"status": "error", **le.to_payload()}, code=2)
    except sqlite3.IntegrityError as exc:
        le = LedgerError("E_DB_CONFLICT", str(exc), at="/db")
        return _emit({"status": "error", **le.to_payload()}, code=2)
