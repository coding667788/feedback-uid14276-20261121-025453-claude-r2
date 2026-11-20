"""SQLite persistence for the compensation ledger.

All mutations run inside a single ``BEGIN IMMEDIATE`` transaction so that
concurrent Python clients serialize on the write lock; any failure rolls the
whole transaction back.  Reads run in autocommit/SHARED mode.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .errors import LedgerError, PersistenceFault
from .model import App, Approval, Case, KeySpec, Txn
from .util import canonical, receipt_id

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS key_spec (
    key           TEXT PRIMARY KEY,
    domain        TEXT NOT NULL,
    initial_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS txn (
    txn_id       TEXT PRIMARY KEY,
    committed_at INTEGER NOT NULL UNIQUE,
    writes       TEXT NOT NULL,
    weight       INTEGER NOT NULL,
    category     TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('active','rolled_back')),
    rollback_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS key_version (
    key            TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS application (
    app_id          TEXT PRIMARY KEY,
    target_txn_id   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    window_start    INTEGER NOT NULL,
    window_end      INTEGER NOT NULL,
    requested_at    INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('open','consumed'))
);
CREATE TABLE IF NOT EXISTS approval (
    approval_id       TEXT PRIMARY KEY,
    app_id            TEXT NOT NULL,
    txn_id            TEXT NOT NULL,
    reviewer          TEXT NOT NULL,
    bound_versions    TEXT NOT NULL,
    decided_at        INTEGER NOT NULL,
    policy_generation INTEGER NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('valid','revoked','consumed'))
);
CREATE TABLE IF NOT EXISTS command_receipt (
    command        TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    receipt_id     TEXT NOT NULL,
    response       TEXT NOT NULL,
    PRIMARY KEY (command, idempotency_key)
);
CREATE TABLE IF NOT EXISTS apply_receipt (
    receipt_id      TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL,
    app_id          TEXT NOT NULL,
    steps           TEXT NOT NULL,
    new_versions    TEXT NOT NULL,
    final_state     TEXT NOT NULL,
    committed_at_lt INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS step_receipt (
    receipt_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    txn_id          TEXT NOT NULL,
    approval_id     TEXT NOT NULL,
    dep_versions    TEXT NOT NULL,
    reviewer        TEXT NOT NULL,
    PRIMARY KEY (receipt_id, seq)
);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    def _ensure_schema(self) -> None:
        # executescript() implicitly commits any pending transaction, so the
        # DDL runs in autocommit and only the meta insert is transactional.
        self.conn.executescript(_SCHEMA)
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        elif int(row["value"]) != SCHEMA_VERSION:
            raise LedgerError("E_SCHEMA", "incompatible schema version", at="/db")

    def is_bootstrapped(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM meta WHERE key='case_id'"
        ).fetchone() is not None

    # ------------------------------------------------------------------ #
    def bootstrap(self, case: Case) -> dict[str, Any]:
        if self.is_bootstrapped():
            raise LedgerError("E_DB_STATE", "database already bootstrapped", at="/db")
        versions = self._current_versions_from_case(case)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._put_meta("case_id", case.case_id)
            self._put_meta("logical_time", str(case.logical_time))
            self._put_meta("policy_generation", str(case.policy_generation))
            self._put_meta("constraints", canonical([
                self._constraint_dict(c) for c in case.constraints
            ]))
            for spec in case.keys.values():
                self.conn.execute(
                    "INSERT INTO key_spec(key, domain, initial_value) VALUES(?,?,?)",
                    (spec.key, canonical(list(spec.domain)), canonical(spec.initial_value)),
                )
                self.conn.execute(
                    "INSERT INTO key_version(key, current_version) VALUES(?,?)",
                    (spec.key, versions[spec.key]),
                )
            for t in case.transactions:
                self.conn.execute(
                    "INSERT INTO txn(txn_id, committed_at, writes, weight, category,"
                    " status, rollback_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        t.txn_id, t.committed_at,
                        canonical([w.__dict__ for w in t.writes]),
                        t.weight, t.category,
                        "rolled_back" if t.rolled_back else "active",
                        t.rollback_at,
                    ),
                )
            for app in case.apps.values():
                self.conn.execute(
                    "INSERT INTO application(app_id, target_txn_id, idempotency_key,"
                    " window_start, window_end, requested_at, status)"
                    " VALUES(?,?,?,?,?,?, 'open')",
                    (app.app_id, app.target_txn_id, app.idempotency_key,
                     app.window_start, app.window_end, app.requested_at),
                )
            for ap in case.approvals.values():
                self.conn.execute(
                    "INSERT INTO approval(approval_id, app_id, txn_id, reviewer,"
                    " bound_versions, decided_at, policy_generation, status)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        ap.approval_id, ap.app_id, ap.txn_id, ap.reviewer,
                        canonical(ap.bound_versions), ap.decided_at,
                        ap.policy_generation, "revoked" if ap.revoked else "valid",
                    ),
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.snapshot()

    @staticmethod
    def _constraint_dict(c: Any) -> dict[str, Any]:
        return {
            "constraint_id": c.constraint_id,
            "active_key": c.active_key,
            "active_values": list(c.active_values),
            "requires": [
                {"key": r.key, "allowed": list(r.allowed)} for r in c.requires
            ],
        }

    def _put_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?)", (key, value)
        )

    def _get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    @staticmethod
    def _current_versions_from_case(case: Case) -> dict[str, int]:
        versions = {k: 0 for k in case.keys}
        events: list[tuple[int, int, str]] = []
        for idx, t in enumerate(case.transactions):
            events.append((t.committed_at, idx, "commit"))
            if t.rolled_back:
                events.append((t.rollback_at, idx, "rollback"))
        events.sort(key=lambda e: (e[0], 0 if e[2] == "commit" else 1))
        for _w, idx, _kind in events:
            for wr in case.transactions[idx].writes:
                versions[wr.key] += 1
        return versions

    # ------------------------------------------------------------------ #
    # snapshots
    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        if not self.is_bootstrapped():
            raise LedgerError("E_DB_STATE", "database is not bootstrapped", at="/db")
        keys: dict[str, KeySpec] = {}
        for row in self.conn.execute("SELECT * FROM key_spec"):
            keys[row["key"]] = KeySpec(
                key=row["key"],
                domain=tuple(json.loads(row["domain"])),
                initial_value=json.loads(row["initial_value"]),
            )
        versions = {
            row["key"]: row["current_version"]
            for row in self.conn.execute("SELECT * FROM key_version")
        }
        txns: list[Txn] = []
        from .model import Write
        for row in self.conn.execute("SELECT * FROM txn ORDER BY committed_at"):
            writes = tuple(Write(**w) for w in json.loads(row["writes"]))
            txns.append(Txn(
                txn_id=row["txn_id"], committed_at=row["committed_at"],
                writes=writes, weight=row["weight"], category=row["category"],
                rolled_back=row["status"] == "rolled_back",
                rollback_at=row["rollback_at"],
            ))
        apps = {
            row["app_id"]: App(
                app_id=row["app_id"], target_txn_id=row["target_txn_id"],
                idempotency_key=row["idempotency_key"],
                window_start=row["window_start"], window_end=row["window_end"],
                requested_at=row["requested_at"],
            )
            for row in self.conn.execute("SELECT * FROM application")
        }
        approvals = {}
        for row in self.conn.execute("SELECT * FROM approval"):
            # both 'revoked' and 'consumed' rows are unusable to the engine
            approvals[row["approval_id"]] = Approval(
                approval_id=row["approval_id"], app_id=row["app_id"],
                txn_id=row["txn_id"], reviewer=row["reviewer"],
                bound_versions=json.loads(row["bound_versions"]),
                decided_at=row["decided_at"],
                policy_generation=row["policy_generation"],
                revoked=row["status"] in ("revoked", "consumed"),
            )
        constraints_raw = self._get_meta("constraints")
        from .model import Constraint, Requirement
        constraints = tuple(
            Constraint(
                constraint_id=c["constraint_id"], active_key=c["active_key"],
                active_values=tuple(c["active_values"]),
                requires=tuple(
                    Requirement(r["key"], tuple(r["allowed"]))
                    for r in c["requires"]
                ),
            )
            for c in json.loads(constraints_raw or "[]")
        )
        case = Case(
            case_id=self._get_meta("case_id") or "",
            logical_time=int(self._get_meta("logical_time") or "0"),
            policy_generation=int(self._get_meta("policy_generation") or "0"),
            keys=keys, constraints=constraints,
            transactions=tuple(txns), apps=apps, approvals=approvals,
        )
        return {"case": case, "versions": versions}

    # ------------------------------------------------------------------ #
    # idempotent commands
    # ------------------------------------------------------------------ #
    def lookup_receipt(self, command: str, idem_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT receipt_id, response FROM command_receipt "
            "WHERE command=? AND idempotency_key=?",
            (command, idem_key),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["response"])
        return {**payload, "receipt_id": row["receipt_id"]}

    def save_receipt(self, command: str, idem_key: str, response: dict[str, Any]) -> str:
        rid = receipt_id(command, {"idem": idem_key, "response": response})
        self.conn.execute(
            "INSERT INTO command_receipt(command, idempotency_key, receipt_id, response)"
            " VALUES(?,?,?,?)",
            (command, idem_key, rid, canonical(response)),
        )
        return rid

    def submit_application(self, app: App) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.lookup_receipt("submit-application", app.idempotency_key)
            if existing is not None:
                self.conn.execute("ROLLBACK")
                return existing
            if self.conn.execute(
                "SELECT 1 FROM application WHERE app_id=?", (app.app_id,)
            ).fetchone():
                raise LedgerError("E_DUPLICATE",
                                  f"application '{app.app_id}' already exists",
                                  at="/application/app_id")
            row = self.conn.execute(
                "SELECT status FROM txn WHERE txn_id=?", (app.target_txn_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("E_REF",
                                  f"unknown target transaction '{app.target_txn_id}'",
                                  at="/application/target_txn_id")
            if row["status"] != "active":
                raise LedgerError("E_TXN_UNDONE",
                                  "target transaction is already undone",
                                  at="/application/target_txn_id")
            self.conn.execute(
                "INSERT INTO application(app_id, target_txn_id, idempotency_key,"
                " window_start, window_end, requested_at, status)"
                " VALUES(?,?,?,?,?,?, 'open')",
                (app.app_id, app.target_txn_id, app.idempotency_key,
                 app.window_start, app.window_end, app.requested_at),
            )
            response = {
                "status": "accepted", "app_id": app.app_id,
                "target_txn_id": app.target_txn_id,
                "window": [app.window_start, app.window_end],
            }
            rid = self.save_receipt("submit-application", app.idempotency_key, response)
            response = {**response, "receipt_id": rid}
            self.conn.execute("COMMIT")
            return response
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def register_approval(self, ap: Approval) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            idem = ap.approval_id
            existing = self.lookup_receipt("register-approval", idem)
            if existing is not None:
                self.conn.execute("ROLLBACK")
                return existing
            if self.conn.execute(
                "SELECT 1 FROM approval WHERE approval_id=?", (ap.approval_id,)
            ).fetchone():
                raise LedgerError("E_DUPLICATE",
                                  f"approval '{ap.approval_id}' already exists",
                                  at="/approval/approval_id")
            app_row = self.conn.execute(
                "SELECT * FROM application WHERE app_id=?", (ap.app_id,)
            ).fetchone()
            if app_row is None:
                raise LedgerError("E_REF", f"unknown application '{ap.app_id}'",
                                  at="/approval/app_id")
            if not self.conn.execute(
                "SELECT 1 FROM txn WHERE txn_id=?", (ap.txn_id,)
            ).fetchone():
                raise LedgerError("E_REF", f"unknown transaction '{ap.txn_id}'",
                                  at="/approval/txn_id")
            self.conn.execute(
                "INSERT INTO approval(approval_id, app_id, txn_id, reviewer,"
                " bound_versions, decided_at, policy_generation, status)"
                " VALUES(?,?,?,?,?,?,?, 'valid')",
                (ap.approval_id, ap.app_id, ap.txn_id, ap.reviewer,
                 canonical(ap.bound_versions), ap.decided_at,
                 ap.policy_generation),
            )
            response = {
                "status": "registered", "approval_id": ap.approval_id,
                "app_id": ap.app_id, "txn_id": ap.txn_id,
                "reviewer": ap.reviewer,
            }
            rid = self.save_receipt("register-approval", idem, response)
            # receipt_id is derived deterministically from the stable body;
            # surface it without feeding it back into the stored receipt body.
            response = {**response, "receipt_id": rid}
            self.conn.execute("COMMIT")
            return response
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def revoke_approval(self, approval_id: str) -> dict[str, Any]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing = self.lookup_receipt("revoke-approval", approval_id)
            if existing is not None:
                self.conn.execute("ROLLBACK")
                return existing
            row = self.conn.execute(
                "SELECT status FROM approval WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("E_NOT_FOUND",
                                  f"approval '{approval_id}' does not exist",
                                  at="/approval_id")
            if row["status"] == "consumed":
                raise LedgerError("E_APPROVAL_CONSUMED",
                                  "a consumed approval cannot be revoked",
                                  at="/approval_id")
            self.conn.execute(
                "UPDATE approval SET status='revoked' WHERE approval_id=?",
                (approval_id,),
            )
            response = {"status": "revoked", "approval_id": approval_id}
            rid = self.save_receipt("revoke-approval", approval_id, response)
            response = {**response, "receipt_id": rid}
            self.conn.execute("COMMIT")
            return response
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ #
    # atomic apply
    # ------------------------------------------------------------------ #
    def begin_write(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def rollback_write(self) -> None:
        self.conn.execute("ROLLBACK")

    def commit_write(self) -> None:
        self.conn.execute("COMMIT")

    def fault_point(self, fault: dict[str, Any], phase: str) -> None:
        """Test-only persistence fault injection; raises before COMMIT."""
        if fault.get("phase") == phase:
            raise PersistenceFault(
                fault.get("message", f"injected fault at {phase}"),
                at=f"/fault/{phase}",
            )

    def apply_plan(self, plan_id: str, app_id: str,
                   steps: list[dict[str, Any]],
                   final_state: dict[str, Any],
                   new_versions: dict[str, int],
                   consumed_approvals: list[str],
                   rolled_back: list[tuple[str, int]],
                   step_receipts: list[dict[str, Any]],
                   committed_at_lt: int,
                   fault: dict[str, Any]) -> dict[str, Any]:
        """Persist a fully verified plan.  Caller holds BEGIN IMMEDIATE."""
        # The apply receipt id IS the plan fingerprint.  Replaying the same
        # plan therefore always yields the identical receipt, by construction.
        rid = plan_id
        replayed = self.lookup_receipt("apply", plan_id)
        if replayed is not None:
            return replayed
        if self.conn.execute(
            "SELECT 1 FROM apply_receipt WHERE receipt_id=?", (rid,)
        ).fetchone() is not None:
            return self._existing_apply(rid)

        self.fault_point(fault, "before_step_writes")
        for txn_id, rb_at in rolled_back:
            self.conn.execute(
                "UPDATE txn SET status='rolled_back', rollback_at=? WHERE txn_id=?",
                (rb_at, txn_id),
            )
        for k, v in new_versions.items():
            self.conn.execute(
                "UPDATE key_version SET current_version=? WHERE key=?", (v, k)
            )
        for ap_id in consumed_approvals:
            self.conn.execute(
                "UPDATE approval SET status='consumed' WHERE approval_id=?",
                (ap_id,),
            )
        self.conn.execute(
            "UPDATE application SET status='consumed' WHERE app_id=?", (app_id,)
        )
        for sr in step_receipts:
            self.conn.execute(
                "INSERT INTO step_receipt(receipt_id, seq, txn_id, approval_id,"
                " dep_versions, reviewer) VALUES(?,?,?,?,?,?)",
                (rid, sr["seq"], sr["txn_id"], sr["approval_id"],
                 canonical(sr["dep_versions"]), sr["reviewer"]),
            )
        self.conn.execute(
            "INSERT INTO apply_receipt(receipt_id, plan_id, app_id, steps,"
            " new_versions, final_state, committed_at_lt) VALUES(?,?,?,?,?,?,?)",
            (rid, plan_id, app_id, canonical(steps), canonical(new_versions),
             canonical(final_state), committed_at_lt),
        )
        response = {
            "status": "applied",
            "receipt_id": rid,
            "plan_id": plan_id,
            "app_id": app_id,
            "steps": steps,
            "new_versions": new_versions,
            "final_state": final_state,
        }
        # Store under the plan fingerprint itself (no second digest), so the
        # idempotency key and the returned receipt id are identical for ever.
        self.conn.execute(
            "INSERT INTO command_receipt(command, idempotency_key, receipt_id,"
            " response) VALUES('apply', ?, ?, ?)",
            (plan_id, rid, canonical(response)),
        )
        self.fault_point(fault, "before_commit")
        return response

    def _existing_apply(self, rid: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT response FROM command_receipt WHERE receipt_id=?", (rid,)
        ).fetchone()
        payload = json.loads(row["response"])
        return {**payload, "receipt_id": row["receipt_id"]}
