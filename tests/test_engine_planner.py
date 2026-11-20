import json
import unittest

from solution.engine import World, approval_eligible, assert_invariants
from solution.errors import LedgerError
from solution.model import Approval
from solution.planner import Planner, brute_force_plan
from solution.validation import load_case, dep_keys

from builders import build_case, txn, app as mk_app, approval, random_case


# --------------------------------------------------------------------------- #
# 1. cross-key diamond dependency + per-key version monotonicity
# --------------------------------------------------------------------------- #
def diamond_case():
    keys = {"a": ["a0", "a1"], "b": ["b0", "b1"],
            "c": ["c0", "c1"], "d": ["d0", "d1"]}
    txns = [
        txn("T1", 10, [("a", "a0", "a1", 1), ("b", "b0", "b1", 1)]),
        txn("T2", 20, [("a", "a1", "a0", 2), ("c", "c0", "c1", 1)]),
        txn("T3", 30, [("b", "b1", "b0", 2), ("d", "d0", "d1", 1)]),
        txn("T4", 40, [("c", "c1", "c0", 2), ("d", "d1", "d0", 2)]),
    ]
    data = build_case(keys=keys, txns=txns,
                      apps=[mk_app("APP1", "T1")])
    case = load_case(data)
    world = World(case)
    cur = dict(world.versions)
    # Emit approvals for BOTH legal middle orders.  After T4, both T2 and T3
    # are rollbackable; the planner must still pick one deterministic sequence
    # by the (txn_id, approval_id) tie-break.
    approvals: list[dict] = []
    reviewers = {"T4": "r4", "T3": "r3", "T2": "r2", "T1": "r1"}

    def emit(tid: str):
        t = next(x for x in case.transactions if x.txn_id == tid)
        bound = {k: world.versions[k]
                 for k in sorted(dep_keys(t, case.constraints))}
        approvals.append(approval(f"AP-{tid}", "APP1", tid,
                                  reviewers[tid], bound))

    emit("T4")
    # branch A: T4 -> T3 -> T2 -> T1
    world.apply_rollback("T4")
    emit("T3")
    world.apply_rollback("T3")
    emit("T2")
    world.apply_rollback("T2")
    emit("T1")
    # rebuild and branch B approvals: T4 -> T2 -> T3 -> T1
    wb = World(case)
    wb.apply_rollback("T4")
    t = next(x for x in case.transactions if x.txn_id == "T2")
    bound = {k: wb.versions[k] for k in sorted(dep_keys(t, case.constraints))}
    approvals.append(approval("AP-T2-b", "APP1", "T2", "r2b", bound))
    wb.apply_rollback("T2")
    t = next(x for x in case.transactions if x.txn_id == "T3")
    bound = {k: wb.versions[k] for k in sorted(dep_keys(t, case.constraints))}
    approvals.append(approval("AP-T3-b", "APP1", "T3", "r3b", bound))
    wb.apply_rollback("T3")
    t = next(x for x in case.transactions if x.txn_id == "T1")
    bound = {k: wb.versions[k] for k in sorted(dep_keys(t, case.constraints))}
    approvals.append(approval("AP-T1-b", "APP1", "T1", "r1b", bound))
    data["approvals"] = approvals
    return data, cur


class DiamondTests(unittest.TestCase):
    def test_closure_is_the_full_diamond(self):
        data, _ = diamond_case()
        case = load_case(data)
        world = World(case)
        self.assertEqual(world.closure("T1"), {"T1", "T2", "T3", "T4"})

    def test_plan_executes_full_prefix_with_deterministic_tie_break(self):
        data, _ = diamond_case()
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(result.ok, result.reason)
        # T4 must be first; after it both T2 and T3 are legal, and the
        # (txn_id, approval_id) tie-break deterministically chooses T2/AP-T2.
        ids = [s.txn_id for s in result.steps]
        self.assertEqual(ids[0], "T4")
        self.assertEqual(ids[-1], "T1")
        self.assertEqual(sorted(ids), ["T1", "T2", "T3", "T4"])
        self.assertEqual(result.outside_cost, 0)
        # a second, independent run returns the identical sequence
        again = Planner(load_case(data), case.apps["APP1"]).run()
        self.assertEqual(
            [(s.txn_id, s.approval_id) for s in result.steps],
            [(s.txn_id, s.approval_id) for s in again.steps],
        )

    def test_brute_force_agrees_on_diamond(self):
        data, _ = diamond_case()
        case = load_case(data)
        dfs = Planner(case, case.apps["APP1"]).run()
        brute = brute_force_plan(case, case.apps["APP1"], limit=5)
        self.assertTrue(brute.ok)
        self.assertEqual(
            [(s.txn_id, s.approval_id) for s in dfs.steps],
            [(s.txn_id, s.approval_id) for s in brute.steps],
        )

    def test_per_key_versions_keep_increasing_through_rollback(self):
        data, cur = diamond_case()
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        world = World(case)
        seen = {k: [cur[k]] for k in cur}
        for step in result.steps:
            world.apply_rollback(step.txn_id)
            for k in dep_keys(next(t for t in case.transactions if t.txn_id == step.txn_id),
                              case.constraints):
                seen[k].append(world.versions[k])
        for k, seq in seen.items():
            self.assertEqual(seq, sorted(seq), f"{k} not monotone")
            self.assertEqual(len(seq), len(set(seq)), f"{k} version repeated")

    def test_effective_source_returns_to_previous_undone_txn(self):
        # T2 writes a; rolling back T2 (after T1 also undone) restores initial,
        # and a later re-plan sees T1 inactive.
        data, _ = diamond_case()
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        world = World(case)
        for s in result.steps:
            world.apply_rollback(s.txn_id)
        self.assertEqual(world.state,
                         {k: case.keys[k].initial_value for k in case.keys})
        # versions did NOT reset
        self.assertTrue(all(v > 0 for v in world.versions.values()))


# --------------------------------------------------------------------------- #
# 2. same value after compensation but the old approval is stale
# --------------------------------------------------------------------------- #
class SameValueStaleApprovalTests(unittest.TestCase):
    def _data(self):
        keys = {"x": [0, 1]}
        txns = [
            txn("T1", 10, [("x", 0, 1, 1)]),
            txn("T2", 20, [("x", 1, 1, 2)]),  # same value, version still grows
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        world = World(case)
        return data, case, world

    def test_same_value_write_still_bumps_version(self):
        _, case, world = self._data()
        self.assertEqual(world.state["x"], 1)
        self.assertEqual(world.versions["x"], 2)

    def test_old_approval_is_not_eligible_after_versions_move(self):
        data, case, world = self._data()
        t2 = next(t for t in case.transactions if t.txn_id == "T2")
        ap_old = Approval(approval_id="AP-old", app_id="APP1", txn_id="T2",
                          reviewer="rv", bound_versions={"x": 1},
                          decided_at=60, policy_generation=1)
        ok, why = approval_eligible(ap_old, case.apps["APP1"], t2, world, now=100)
        self.assertFalse(ok)
        self.assertIn("stale", why)
        ap_new = Approval(approval_id="AP-new", app_id="APP1", txn_id="T2",
                          reviewer="rv", bound_versions={"x": 2},
                          decided_at=60, policy_generation=1)
        ok, _ = approval_eligible(ap_new, case.apps["APP1"], t2, world, now=100)
        self.assertTrue(ok)

    def test_planner_requires_fresh_approval(self):
        data, case, world = self._data()
        t1 = next(t for t in case.transactions if t.txn_id == "T1")
        # stale approval for T2 (version 1 instead of current 2) blocks the plan
        data["approvals"] = [
            approval("AP-T2-stale", "APP1", "T2", "rv", {"x": 1}),
        ]
        case2 = load_case(data)
        result = Planner(case2, case2.apps["APP1"]).run()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason["code"], "E_NO_APPROVAL")
        self.assertIn("T2", result.reason["details"]["txn_id"])


# --------------------------------------------------------------------------- #
# 3. two legal orders, different approval suffixes; tie-break + brute force
# --------------------------------------------------------------------------- #
class LegalOrderTiebreakTests(unittest.TestCase):
    def _data(self):
        # T1 writes (a, b) and is the common ancestor. T2 later writes b, T3
        # later writes a (disjoint side branches), and T4 writes neither but is
        # linked into the closure by touching both branch side keys b and c.
        # After T4 is undone, T2 and T3 touch disjoint keys and are therefore
        # independently rollbackable -> two legal middle orders; all four
        # transactions still belong to T1's transitive write-chain closure.
        keys = {"a": [0, 1], "b": [0, 1], "c": [0, 1]}
        txns = [
            txn("T1", 10, [("a", 0, 1, 1), ("b", 0, 1, 1)]),
            txn("T2", 20, [("b", 1, 0, 2)]),
            txn("T3", 30, [("a", 1, 0, 2), ("c", 0, 1, 1)]),
            txn("T4", 40, [("b", 0, 1, 3), ("c", 1, 0, 2)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        self.assertEqual(World(case).closure("T1"),
                         frozenset({"T1", "T2", "T3", "T4"}))
        approvals = []

        def emit(tid, suffix, w):
            t = next(x for x in case.transactions if x.txn_id == tid)
            bound = {k: w.versions[k] for k in sorted(dep_keys(t, case.constraints))}
            approvals.append(approval(f"AP-{tid}-{suffix}", "APP1", tid,
                                      f"rv-{tid}-{suffix}", bound))

        # order A: T4, T2, T3, T1
        wa = World(case)
        emit("T4", "p", wa); wa.apply_rollback("T4")
        emit("T2", "p", wa); wa.apply_rollback("T2")
        emit("T3", "p", wa); wa.apply_rollback("T3")
        emit("T1", "p", wa)
        # order B: T4, T3, T2, T1
        wb = World(case)
        wb.apply_rollback("T4")
        emit("T3", "q", wb); wb.apply_rollback("T3")
        emit("T2", "q", wb); wb.apply_rollback("T2")
        emit("T1", "q", wb)
        data["approvals"] = approvals
        return data

    def test_planner_matches_brute_force_and_picks_lex_min(self):
        data = self._data()
        case = load_case(data)
        app = case.apps["APP1"]
        dfs = Planner(case, app).run()
        brute = brute_force_plan(case, app, limit=5)
        self.assertTrue(dfs.ok and brute.ok, dfs.reason)
        dfs_seq = [(s.txn_id, s.approval_id) for s in dfs.steps]
        brute_seq = [(s.txn_id, s.approval_id) for s in brute.steps]
        self.assertEqual(dfs_seq, brute_seq)
        # the whole closure is undone and T4 is forced first
        self.assertEqual(sorted(t for t, _ in dfs_seq),
                         ["T1", "T2", "T3", "T4"])
        self.assertEqual(dfs_seq[0][0], "T4")
        self.assertEqual(dfs_seq[-1][0], "T1")
        self.assertEqual(len(dfs.steps), 4)
        self.assertEqual(dfs.outside_cost, 0)

    def test_two_distinct_legal_orders_both_exist(self):
        data = self._data()
        case = load_case(data)
        orders_seen = set()
        for order in (["T4", "T2", "T3", "T1"], ["T4", "T3", "T2", "T1"]):
            w = World(case)
            for tid in order:
                self.assertIn(tid, w.rollbackable())
                w.apply_rollback(tid)
            self.assertEqual(w.active, set())
            orders_seen.add(tuple(order))
        self.assertEqual(len(orders_seen), 2)

    def test_adjacency_blocks_same_reviewer_on_shared_key(self):
        keys = {"x": [0, 1]}
        txns = [
            txn("T1", 10, [("x", 0, 1, 1)]),
            txn("T2", 20, [("x", 1, 0, 2)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        world = World(case)
        # both steps approved by the SAME reviewer -> adjacent conflict on x
        data["approvals"] = [
            approval("AP-T2", "APP1", "T2", "same", {"x": 2}),
            approval("AP-T1", "APP1", "T1", "same", {"x": 3}),
        ]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason["code"], "E_ADJACENCY")


# --------------------------------------------------------------------------- #
# 4. intermediate state would violate an invariant; no intermediate publication
# --------------------------------------------------------------------------- #
class InvariantIntermediateTests(unittest.TestCase):
    def test_world_rejects_invalid_intermediate(self):
        # Every committed checkpoint is legal. T1 yields (x=1,y=1); T2a flips
        # x to 0 -> (0,1); T2b flips y to 0 -> (0,0). All txns are prefix-
        # ordered, so rollback must run T2b then T2a. After T2b is undone the
        # state returns to (0,1); undoing T2a then restores x=1 while y is 1
        # (legal). To observe a genuinely invalid rollback intermediate, build
        # the symmetric split where the y-writer is undone FIRST while the
        # x-writer stays undone: history T1(x=1,y=1), X(x->0), Y(y->0);
        # rolling Y back alone restores y=1 (state (0,1), legal); rolling X
        # back at that point restores x=1 with y=1 (also legal). Instead the
        # invalid intermediate is reached by rolling X back while Y is still
        # active (y=0): state becomes (1,0) -> invariant violation.
        keys = {"x": [0, 1], "y": [0, 1]}
        constraints = [
            {"constraint_id": "g", "active_key": "x", "active_values": [1],
             "requires": [{"key": "y", "allowed": [1]}]}
        ]
        txns = [
            txn("T1", 10, [("x", 0, 1, 1), ("y", 0, 1, 1)]),
            txn("X", 20, [("x", 1, 0, 2)]),       # (0,1) legal
            txn("Y", 30, [("y", 1, 0, 2)]),       # (0,0) legal
        ]
        data = build_case(keys=keys, constraints=constraints, txns=txns,
                          apps=[mk_app("APP1", "X")])
        case = load_case(data)
        # X is prefix-blocked while Y (later writer of y only) is active only
        # if they share a key; they do not, so X is rollbackable directly and
        # doing so produces the invalid intermediate (x=1,y=0).
        world = World(case)
        self.assertIn("X", world.rollbackable())
        active_before = set(world.active)
        versions_before = dict(world.versions)
        with self.assertRaises(LedgerError) as ctx:
            world.apply_rollback("X")
        self.assertEqual(ctx.exception.code, "E_INVARIANT")
        # atomic: no partially published state
        self.assertEqual(world.active, active_before)
        self.assertEqual(world.versions, versions_before)


# --------------------------------------------------------------------------- #
# 5. solvable only by compensating an outside-closure transaction
# --------------------------------------------------------------------------- #
class OutsideClosureTests(unittest.TestCase):
    def _data(self):
        # Invariant g: y=1 => w in [1]. All historical checkpoints are legal:
        #   T1: (x0,y0,z0,w1) -> (x1,y1,z1,w1)
        #   T2: y 1->0, x 1->0 (g inactive afterwards)
        #   O : w 1->0 while y=0 (g inactive, legal)
        # Undoing the {T1,T2} closure alone: T2 first (restores y=1,x=1) while
        # O still holds w=0 -> already invalid at that point, so O must be
        # undone first. O writes no closure key -> it is outside the closure.
        keys = {"x": [0, 1], "y": [0, 1], "z": [0, 1], "w": [0, 1]}
        initial = {"x": 0, "y": 0, "z": 0, "w": 1}
        constraints = [
            {"constraint_id": "g", "active_key": "y", "active_values": [1],
             "requires": [{"key": "w", "allowed": [1]}]}
        ]
        txns = [
            txn("T1", 10, [("x", 0, 1, 1), ("y", 0, 1, 1), ("z", 0, 1, 1)]),
            txn("T2", 20, [("x", 1, 0, 2), ("y", 1, 0, 2)]),
            txn("O", 30, [("w", 1, 0, 1)], weight=7),
        ]
        data = build_case(keys=keys, initial=initial, constraints=constraints,
                          txns=txns, apps=[mk_app("APP1", "T1")])
        case = load_case(data)
        world = World(case)
        self.assertEqual(world.closure("T1"), frozenset({"T1", "T2"}))
        approvals = []
        # O deps {w, y} (group g pins y read-only); current w=1,y=0
        approvals.append(approval("AP-O", "APP1", "O", "rvO",
                                  {"w": 1, "y": 2}))
        # T2 deps {x,y,w}; reachable with w=1 (no O) and w=2 (after O)
        approvals.append(approval("AP-T2-pre", "APP1", "T2", "rv2pre",
                                  {"x": 2, "y": 2, "w": 1}))
        approvals.append(approval("AP-T2", "APP1", "T2", "rv2",
                                  {"x": 2, "y": 2, "w": 2}))
        # T1 deps {x,y,z,w}; after T2 x->3,y->3; w is 1 (no O) or 2 (after O)
        approvals.append(approval("AP-T1-pre", "APP1", "T1", "rv1pre",
                                  {"x": 3, "y": 3, "z": 1, "w": 1}))
        approvals.append(approval("AP-T1", "APP1", "T1", "rv1",
                                  {"x": 3, "y": 3, "z": 1, "w": 2}))
        data["approvals"] = approvals
        return data, world

    def test_closure_only_order_is_unsatisfiable(self):
        data, world = self._data()
        # Without O's approval the closure alone fails: restoring y=1 while O
        # still holds w=0 violates g at the T2 rollback checkpoint.
        no_o = json.loads(json.dumps(data))
        no_o["approvals"] = [a for a in no_o["approvals"] if a["txn_id"] != "O"]
        case_no_o = load_case(no_o)
        self.assertFalse(Planner(case_no_o, case_no_o.apps["APP1"]).run().ok)

        # With O available the planner inserts the outside transaction first.
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(result.ok, result.reason)
        ids = [s.txn_id for s in result.steps]
        self.assertEqual(ids[0], "O")
        self.assertEqual(sorted(ids), ["O", "T1", "T2"])
        outside = [s for s in result.steps if not s.in_closure]
        self.assertEqual([s.txn_id for s in outside], ["O"])
        self.assertEqual(result.outside_cost, 7)

        w = World(case)
        for s in result.steps:
            w.apply_rollback(s.txn_id)
        assert_invariants(w.state, case.constraints, "final")
        self.assertEqual(w.state, {"x": 0, "y": 0, "z": 0, "w": 1})

    def test_objective_matches_brute_force(self):
        data, _ = self._data()
        case = load_case(data)
        brute = brute_force_plan(case, case.apps["APP1"], limit=4)
        dfs = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(brute.ok and dfs.ok)
        self.assertEqual(
            [(s.txn_id, s.approval_id) for s in dfs.steps],
            [(s.txn_id, s.approval_id) for s in brute.steps],
        )
        self.assertEqual(dfs.outside_cost, 7)
        self.assertEqual(len(dfs.steps), len(brute.steps))

    def test_without_outside_approval_no_plan_exists(self):
        data, _ = self._data()
        data["approvals"] = [a for a in data["approvals"] if a["txn_id"] != "O"]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertFalse(result.ok)


# --------------------------------------------------------------------------- #
# 6. exhaustive small-sample cross-check (planner vs brute force)
# --------------------------------------------------------------------------- #
class ExhaustiveCrossCheckTests(unittest.TestCase):
    def test_many_random_cases_match(self):
        mismatches = 0
        for seed in range(60):
            data = random_case(seed, n_txn=5, n_keys=4)
            case = load_case(data)
            app = case.apps["APP1"]
            dfs = Planner(case, app).run()
            brute = brute_force_plan(case, app, limit=6)
            dfs_seq = None if not dfs.ok else [
                (s.txn_id, s.approval_id) for s in dfs.steps]
            brute_seq = None if not brute.ok else [
                (s.txn_id, s.approval_id) for s in brute.steps]
            if dfs_seq != brute_seq:
                mismatches += 1
        self.assertEqual(mismatches, 0)

    def test_deterministic_across_runs(self):
        data = random_case(7)
        case = load_case(data)
        r1 = Planner(case, case.apps["APP1"]).run()
        r2 = Planner(load_case(random_case(7)), case.apps["APP1"]).run()
        self.assertEqual(
            [(s.txn_id, s.approval_id) for s in r1.steps],
            [(s.txn_id, s.approval_id) for s in r2.steps],
        )


# --------------------------------------------------------------------------- #
# 7. unsatisfiable explanation points at earliest missing approval / blocker
# --------------------------------------------------------------------------- #
class ExplanationTests(unittest.TestCase):
    def test_explains_missing_approval_and_prefix_blocker(self):
        keys = {"x": [0, 1], "z": [0, 1]}
        txns = [
            txn("T1", 10, [("x", 0, 1, 1)]),
            txn("T2", 20, [("x", 1, 0, 2)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        # only approve T1; T2 has no approval and blocks T1
        case = load_case(data)
        data["approvals"] = [approval("AP-T1", "APP1", "T1", "rv", {"x": 3})]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertFalse(result.ok)
        reason = result.reason
        self.assertEqual(reason["code"], "E_NO_APPROVAL")
        self.assertEqual(reason["details"]["txn_id"], "T2")
        # the target T1 cannot be undone while its later writer T2 is active
        self.assertIn("T2", reason["details"]["target_blocker_txn_ids"])

    def test_window_expiry_invalidates_approval(self):
        keys = {"x": [0, 1]}
        txns = [txn("T1", 10, [("x", 0, 1, 1)])]
        data = build_case(
            keys=keys, txns=txns,
            apps=[mk_app("APP1", "T1", window=(0, 50))],
            logical_time=100,  # window already closed
        )
        data["approvals"] = [approval("AP-T1", "APP1", "T1", "rv", {"x": 1})]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason["code"], "E_NO_APPROVAL")


class ReviewerAdjacencyRuleTests(unittest.TestCase):
    def test_same_reviewer_allowed_on_disjoint_key_txns(self):
        # T1 and T2 share no key; the same reviewer may sign both. T2 is not in
        # T1's closure anyway, so the plan undoes T1 alone without conflict.
        keys = {"a": [0, 1], "b": [0, 1]}
        txns = [txn("T1", 10, [("a", 0, 1, 1)]),
                txn("T2", 20, [("b", 0, 1, 1)])]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        data["approvals"] = [
            approval("AP-T1", "APP1", "T1", "same", {"a": 1}),
            approval("AP-T2", "APP1", "T2", "same", {"b": 1}),
        ]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(result.ok)
        self.assertEqual([s.txn_id for s in result.steps], ["T1"])

    def test_same_reviewer_blocked_on_shared_key_chain(self):
        keys = {"a": [0, 1], "b": [0, 1]}
        txns = [
            txn("T1", 10, [("a", 0, 1, 1)]),
            txn("T2", 20, [("a", 1, 0, 2), ("b", 0, 1, 1)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        data["approvals"] = [
            approval("AP-T2", "APP1", "T2", "same", {"a": 2, "b": 1}),
            approval("AP-T1", "APP1", "T1", "same", {"a": 3}),
        ]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason["code"], "E_ADJACENCY")

    def test_distinct_reviewers_unblock_the_chain(self):
        keys = {"a": [0, 1], "b": [0, 1]}
        txns = [
            txn("T1", 10, [("a", 0, 1, 1)]),
            txn("T2", 20, [("a", 1, 0, 2), ("b", 0, 1, 1)]),
        ]
        data = build_case(keys=keys, txns=txns, apps=[mk_app("APP1", "T1")])
        data["approvals"] = [
            approval("AP-T2", "APP1", "T2", "rv2", {"a": 2, "b": 1}),
            approval("AP-T1", "APP1", "T1", "rv1", {"a": 3}),
        ]
        case = load_case(data)
        result = Planner(case, case.apps["APP1"]).run()
        self.assertTrue(result.ok)
        self.assertEqual([s.txn_id for s in result.steps], ["T2", "T1"])


if __name__ == "__main__":
    unittest.main()
