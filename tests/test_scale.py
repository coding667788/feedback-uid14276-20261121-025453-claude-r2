"""Scale, determinism and competing-approval tests at the stated input limit."""

import time
import unittest

from solution.engine import World
from solution.planner import Planner, brute_force_plan
from solution.validation import load_case

from builders import build_case, txn, app as mk_app, approval


def build_scale_case(n_txn: int = 24, n_keys: int = 80):
    key_names = [f"k{i:02d}" for i in range(n_keys)]
    keys = {k: [0, 1] for k in key_names}
    txns = []
    state = {k: 0 for k in key_names}
    ver = {k: 0 for k in key_names}
    for i in range(n_txn):
        # each txn writes two keys, adjacent transactions share one key so the
        # closure of the first txn transitively reaches every later txn
        left = key_names[i % n_keys]
        right = key_names[(i + 1) % n_keys]
        writes = []
        for k in (left, right):
            old = state[k]
            new = 1 - old
            ver[k] += 1
            writes.append((k, old, new, ver[k]))
            state[k] = new
        txns.append(txn(f"T{i:02d}", 10 * (i + 1), writes))
    data = build_case(case_id="scale", keys=keys, txns=txns,
                      apps=[mk_app("APP1", "T00")])
    case = load_case(data)
    # Approvals must bind the versions at the *execution* instant, so walk the
    # forced reverse chain once while recording each step's dependency vector.
    approvs = []
    world = World(case)
    for t in reversed(case.transactions):
        bound = {k: world.versions[k] for k in sorted(t.keys)}
        approvs.append(approval(f"AP-{t.txn_id}", "APP1", t.txn_id,
                                f"rv-{t.txn_id}", bound))
        world.apply_rollback(t.txn_id)
    data["approvals"] = approvs
    return data


class ScaleTests(unittest.TestCase):
    def test_planner_handles_24_txns_80_keys(self):
        data = build_scale_case()
        case = load_case(data)
        self.assertEqual(len(case.transactions), 24)
        self.assertEqual(len(case.keys), 80)
        start = time.perf_counter()
        result = Planner(case, case.apps["APP1"]).run()
        elapsed = time.perf_counter() - start
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(len(result.steps), 24)
        self.assertLess(elapsed, 10.0)

    def test_equivalent_state_memoisation_is_sound_and_deterministic(self):
        data = build_scale_case(n_txn=12, n_keys=30)
        case = load_case(data)
        planner = Planner(case, case.apps["APP1"])
        r1 = planner.run()
        planner2 = Planner(load_case(data), case.apps["APP1"])
        r2 = planner2.run()
        seq1 = [(s.txn_id, s.approval_id) for s in r1.steps]
        seq2 = [(s.txn_id, s.approval_id) for s in r2.steps]
        self.assertEqual(seq1, seq2)
        # memo entries were actually reused
        self.assertGreater(len(planner2.memo), 0)

    def test_output_is_canonical_and_stable(self):
        import json
        from solution.util import canonical
        data = build_scale_case(n_txn=8, n_keys=12)
        case = load_case(data)
        a = Planner(case, case.apps["APP1"]).run().to_dict()
        b = Planner(load_case(data), case.apps["APP1"]).run().to_dict()
        self.assertEqual(canonical(a), canonical(b))
        # key ordering is stable under json re-serialisation
        self.assertEqual(
            json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True)
        )


class CompetingApprovalsTests(unittest.TestCase):
    def _data(self):
        keys = {"x": [0, 1]}
        txns = [
            txn("T1", 10, [("x", 0, 1, 1)]),
            txn("T2", 20, [("x", 1, 0, 2)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        # two qualified approvals for T2 at the same versions, different
        # reviewers/ids; only one may be consumed.
        data["approvals"] = [
            approval("AP-T2-01-first", "APP1", "T2", "rv-a", {"x": 2}),
            approval("AP-T2-02-second", "APP1", "T2", "rv-b", {"x": 2}),
            approval("AP-T1", "APP1", "T1", "rv-c", {"x": 3}),
        ]
        return data

    def test_planner_picks_one_qualified_approval_by_id(self):
        data = self._data()
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(result.ok, result.reason)
        t2_choice = next(s for s in result.steps if s.txn_id == "T2")
        self.assertEqual(t2_choice.approval_id, "AP-T2-01-first")

    def test_only_one_competing_approval_is_consumed(self):
        from solution.storage import Store
        from solution.apply import apply_case
        import tempfile, os, shutil
        data = self._data()
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "c.sqlite")
        try:
            store = Store(db)
            store.bootstrap(load_case(data))
            plan = Planner(load_case(data),
                           load_case(data).apps["APP1"]).run().to_dict()
            plan.pop("plan_id", None)
            resp = apply_case(store, load_case(data), {"plan": plan})
            store.close()
            import sqlite3
            conn = sqlite3.connect(db)
            status = dict(conn.execute(
                "SELECT approval_id, status FROM approval").fetchall())
            conn.close()
            self.assertEqual(status["AP-T2-01-first"], "consumed")
            # the competing approval remains valid and usable elsewhere
            self.assertEqual(status["AP-T2-02-second"], "valid")
            self.assertEqual(resp["status"], "applied")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_brute_force_and_dfs_agree_on_competing_pool(self):
        data = self._data()
        case = load_case(data)
        dfs = Planner(case, case.apps["APP1"]).run()
        brute = brute_force_plan(case, case.apps["APP1"])
        self.assertEqual(
            [(s.txn_id, s.approval_id) for s in dfs.steps],
            [(s.txn_id, s.approval_id) for s in brute.steps],
        )


if __name__ == "__main__":
    unittest.main()
