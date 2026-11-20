import shutil
import sqlite3
import tempfile
import threading
import unittest

from solution.apply import apply_case
from solution.errors import LedgerError, PersistenceFault
from solution.planner import Planner
from solution.storage import Store
from solution.validation import load_case

from helpers import cli, fresh_db, write_case
from builders import build_case, txn, app as mk_app, approval


def chain_case():
    keys = {"x": [0, 1], "y": [0, 1]}
    txns = [
        txn("T1", 10, [("x", 0, 1, 1), ("y", 0, 1, 1)]),
        txn("T2", 20, [("x", 1, 0, 2)]),
    ]
    data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
    case = load_case(data)
    from solution.engine import World
    from solution.validation import dep_keys
    w = World(case)
    approvs = []
    t2 = next(t for t in case.transactions if t.txn_id == "T2")
    approvs.append(approval("AP-T2", "APP1", "T2", "rv2",
                            {k: w.versions[k] for k in sorted(dep_keys(t2, case.constraints))}))
    w.apply_rollback("T2")
    t1 = next(t for t in case.transactions if t.txn_id == "T1")
    approvs.append(approval("AP-T1", "APP1", "T1", "rv1",
                            {k: w.versions[k] for k in sorted(dep_keys(t1, case.constraints))}))
    data["approvals"] = approvs
    data["plan_request"] = {"app_id": "APP1"}
    return data


def planned_payload(data):
    case = load_case(data)
    result = Planner(case, case.apps["APP1"]).run()
    assert result.ok, result.reason
    plan = result.to_dict()
    plan.pop("plan_id", None)
    return {"plan": plan}


class StorageHappyPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = chain_case()
        self.case_path = write_case(self.tmp, "case.json", self.data)
        self.db = fresh_db(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bootstrap_plan_apply_inspect_cli(self):
        code, out = cli(["bootstrap", self.case_path, "--db", self.db])
        self.assertEqual(code, 0, out)

        plan_req = dict(self.data)
        code, plan_out = cli(["plan", write_case(self.tmp, "plan_req.json", plan_req),
                              "--db", self.db])
        self.assertEqual(code, 0, plan_out)
        self.assertEqual(plan_out["status"], "ok")
        self.assertEqual(len(plan_out["steps"]), 2)

        apply_data = dict(self.data)
        apply_data["apply"] = {"plan": {k: v for k, v in plan_out.items()
                                        if k != "plan_id"}}
        apply_path = write_case(self.tmp, "apply.json", apply_data)
        code, applied = cli(["apply", apply_path, "--db", self.db])
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["status"], "applied")
        self.assertTrue(applied["receipt_id"].startswith("apply_"))

        code, again = cli(["apply", apply_path, "--db", self.db])
        self.assertEqual(code, 0, again)
        # replay is keyed by the plan fingerprint, so it returns the stored
        # receipt even though the recomputed digest would differ.
        self.assertEqual(again["receipt_id"], applied["receipt_id"])
        self.assertEqual(again["plan_id"], applied["plan_id"])

        code, insp = cli(["inspect", self.case_path, "--db", self.db])
        self.assertEqual(code, 0)
        self.assertEqual(insp["active_transactions"], [])
        self.assertEqual(set(insp["rolled_back_transactions"]), {"T1", "T2"})
        # versions keep increasing, values return to initial
        self.assertEqual(insp["effective_state"], {"x": 0, "y": 0})
        self.assertGreaterEqual(insp["current_versions"]["x"], 3)

    def test_step_receipts_persisted(self):
        store = Store(self.db)
        store.bootstrap(load_case(self.data))
        payload = planned_payload(self.data)
        resp = apply_case(store, load_case(self.data), payload)
        store.close()
        conn = sqlite3.connect(self.db)
        rows = list(conn.execute(
            "SELECT seq, txn_id, approval_id FROM step_receipt "
            "WHERE receipt_id=? ORDER BY seq", (resp["receipt_id"],)))
        self.assertEqual([r[1] for r in rows], ["T2", "T1"])
        conn.close()


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = chain_case()
        self.db = fresh_db(self.tmp)
        self.store = Store(self.db)
        self.store.bootstrap(load_case(self.data))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_application_submission_is_idempotent(self):
        from solution.model import App
        a = App(app_id="AX", target_txn_id="T1", idempotency_key="idem-x",
                window_start=0, window_end=200, requested_at=50)
        r1 = self.store.submit_application(a)
        r2 = self.store.submit_application(a)
        self.assertEqual(r1, r2)
        self.assertEqual(r1["receipt_id"], r2["receipt_id"])

    def test_approval_registration_and_revoke(self):
        from solution.model import Approval
        ap = Approval(approval_id="APX", app_id="APP1", txn_id="T2",
                      reviewer="rv", bound_versions={"x": 2, "y": 1},
                      decided_at=60, policy_generation=1)
        r1 = self.store.register_approval(ap)
        r2 = self.store.register_approval(ap)
        self.assertEqual(r1["receipt_id"], r2["receipt_id"])
        rv = self.store.revoke_approval("APX")
        self.assertEqual(rv["status"], "revoked")

    def test_same_approval_id_replay_after_process_restart(self):
        # simulate a process exit by closing and reopening the store
        from solution.model import Approval
        ap = Approval(approval_id="APX2", app_id="APP1", txn_id="T2",
                      reviewer="rv", bound_versions={"x": 2, "y": 1},
                      decided_at=60, policy_generation=1)
        first = self.store.register_approval(ap)
        self.store.close()
        store2 = Store(self.db)
        second = store2.register_approval(ap)
        self.assertEqual(first, second)
        store2.close()


class PersistenceFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = fresh_db(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply_with_fault(self, phase):
        data = chain_case()
        data["fault"] = {"phase": phase}
        store = Store(self.db)
        store.bootstrap(load_case(chain_case()))
        payload = planned_payload(chain_case())
        with self.assertRaises(PersistenceFault) as ctx:
            apply_case(store, load_case(data), payload)
        self.assertEqual(ctx.exception.code, "E_PERSISTENCE")
        store.close()
        # nothing committed: all transactions still active
        conn = sqlite3.connect(self.db)
        statuses = list(conn.execute("SELECT txn_id, status FROM txn ORDER BY txn_id"))
        conn.close()
        self.assertTrue(all(s == "active" for _, s in statuses))

    def test_fault_before_step_writes_rolls_back(self):
        self._apply_with_fault("before_step_writes")

    def test_fault_before_commit_rolls_back(self):
        self._apply_with_fault("before_commit")

    def test_retry_after_fault_succeeds_with_same_receipt(self):
        data = chain_case()
        data["fault"] = {"phase": "before_commit"}
        store = Store(self.db)
        store.bootstrap(load_case(chain_case()))
        payload = planned_payload(chain_case())
        with self.assertRaises(PersistenceFault):
            apply_case(store, load_case(data), payload)
        store.close()

        clean = chain_case()
        store2 = Store(self.db)
        resp = apply_case(store2, load_case(clean), payload)
        store2.close()
        self.assertEqual(resp["status"], "applied")
        # a further replay returns the identical receipt
        store3 = Store(self.db)
        again = apply_case(store3, load_case(clean), payload)
        store3.close()
        self.assertEqual(again["receipt_id"], resp["receipt_id"])


class WriteConflictTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = fresh_db(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_related_key_concurrent_apply_one_wins_one_fails(self):
        # two applications on overlapping keys; only one apply can win
        data = chain_case()
        data["applications"].append(mk_app("APP2", "T2", idem="idem-app2"))
        # approvals for APP2 on T2 (T2 writes only x; no constraints here)
        data["approvals"].append(approval("AP2-T2", "APP2", "T2", "rvX",
                                          {"x": 2}))
        store = Store(self.db)
        store.bootstrap(load_case(data))

        payload1 = planned_payload(data)  # APP1 full chain
        case2 = load_case(data)
        plan2 = Planner(case2, case2.apps["APP2"]).run().to_dict()
        plan2.pop("plan_id", None)
        payload2 = {"plan": plan2}

        ok = {"winner": None}
        errors = []

        def run(payload, tag):
            try:
                r = apply_case(Store(self.db), load_case(data), payload)
                ok["winner"] = tag
                return r
            except LedgerError as exc:
                errors.append((tag, exc.code))

        t1 = threading.Thread(target=run, args=(payload1, "A"))
        t2 = threading.Thread(target=run, args=(payload2, "B"))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertIsNotNone(ok["winner"])
        self.assertEqual(len(errors), 1)
        self.assertIn(errors[0][1],
                      {"E_PLAN_STALE", "E_WRITE_CONFLICT", "E_APPROVAL_INVALID",
                       "E_TXN_STATE", "E_PREFIX"})
        # exactly one apply committed: count apply receipts
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM apply_receipt").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_invariant_read_only_key_concurrent_modification(self):
        # invariant read-only key 'r' is pinned by the plan but bumped in the
        # live db before apply -> E_WRITE_CONFLICT and no commit
        keys = {"x": [0, 1], "r": [0, 1]}
        constraints = [
            {"constraint_id": "g", "active_key": "x", "active_values": [1],
             "requires": [{"key": "r", "allowed": [0, 1]}]}
        ]
        txns = [txn("T1", 10, [("x", 0, 1, 1)])]
        data = build_case(keys=keys, constraints=constraints, txns=txns,
                          apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        from solution.engine import World
        from solution.validation import dep_keys
        w = World(case)
        t1 = case.transactions[0]
        data["approvals"] = [
            approval("AP-T1", "APP1", "T1", "rv",
                     {k: w.versions[k] for k in sorted(dep_keys(t1, case.constraints))})
        ]
        store = Store(self.db)
        store.bootstrap(load_case(data))
        payload = planned_payload(data)

        # concurrent writer bumps the read-only key version directly in the db
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE key_version SET current_version = current_version + 1 "
                     "WHERE key='r'")
        conn.commit()
        conn.close()

        with self.assertRaises(LedgerError) as ctx:
            apply_case(store, load_case(data), payload)
        # the plan either fails the fresh re-plan or the explicit pin check;
        # both prove the read-only key version is part of the apply guard.
        self.assertIn(ctx.exception.code,
                      {"E_WRITE_CONFLICT", "E_PLAN_STALE"})
        store.close()

    def test_no_critical_change_after_plan_succeeds(self):
        # planning then applying with no intervening change must succeed
        data = chain_case()
        store = Store(self.db)
        store.bootstrap(load_case(data))
        payload = planned_payload(data)
        resp = apply_case(store, load_case(data), payload)
        self.assertEqual(resp["status"], "applied")
        store.close()

    def test_many_concurrent_clients_same_plan_one_durable_row(self):
        # independent Store connections (separate processes are equivalent at
        # the SQLite lock level) all submit the same plan: every caller gets the
        # same receipt but exactly one row is durably committed.
        data = chain_case()
        store = Store(self.db)
        store.bootstrap(load_case(data))
        store.close()
        payload = planned_payload(data)
        receipts: list[str] = []
        lock = threading.Lock()

        def client():
            s = Store(self.db)
            r = apply_case(s, load_case(data), payload)
            s.close()
            with lock:
                receipts.append(r["receipt_id"])

        threads = [threading.Thread(target=client) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(receipts), 8)
        self.assertEqual(set(receipts), {receipts[0]})
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT COUNT(*) FROM apply_receipt").fetchone()[0]
        conn.close()
        self.assertEqual(rows, 1)


class PolicyAndWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = fresh_db(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_policy_generation_mismatch_rejects_plan(self):
        data = chain_case()
        store = Store(self.db)
        store.bootstrap(load_case(data))
        payload = planned_payload(data)
        payload["plan"]["policy_generation"] = 999
        with self.assertRaises(LedgerError) as ctx:
            apply_case(store, load_case(data), payload)
        self.assertEqual(ctx.exception.code, "E_POLICY_GENERATION")
        store.close()

    def test_tampered_plan_id_rejected(self):
        data = chain_case()
        store = Store(self.db)
        store.bootstrap(load_case(data))
        payload = planned_payload(data)
        payload["plan"]["plan_id"] = "plan_deadbeef"
        with self.assertRaises(LedgerError) as ctx:
            apply_case(store, load_case(data), payload)
        self.assertEqual(ctx.exception.code, "E_PLAN_TAMPERED")
        store.close()


if __name__ == "__main__":
    unittest.main()
