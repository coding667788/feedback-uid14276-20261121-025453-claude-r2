import os
import tempfile
import unittest

from solution.errors import LedgerError
from solution.validation import load_case

from helpers import cli, write_case


def base_case():
    return {
        "case_id": "c", "logical_time": 100, "policy_generation": 1,
        "keys": [
            {"key": "x", "domain": [0, 1], "initial_value": 0},
            {"key": "y", "domain": [0, 1], "initial_value": 0},
        ],
        "constraints": [
            {"constraint_id": "g", "active_key": "x", "active_values": [1],
             "requires": [{"key": "y", "allowed": [1]}]}
        ],
        "transactions": [
            {"txn_id": "T1", "committed_at": 10, "writes": [
                {"key": "x", "old_value": 0, "new_value": 1, "new_version": 1},
                {"key": "y", "old_value": 0, "new_value": 1, "new_version": 1},
            ]},
            {"txn_id": "T2", "committed_at": 20, "writes": [
                {"key": "x", "old_value": 1, "new_value": 0, "new_version": 2},
            ]},
        ],
        "applications": [],
    }


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_valid_case_loads(self):
        case = load_case(base_case())
        self.assertEqual(len(case.transactions), 2)
        self.assertEqual(case.keys["x"].domain, (0, 1))

    def test_missing_field_has_path(self):
        data = base_case()
        del data["transactions"][0]["writes"][0]["key"]
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_MISSING_FIELD")
        self.assertIn("/transactions/0/writes/0/key", ctx.exception.at)

    def test_wrong_type_has_path(self):
        data = base_case()
        data["logical_time"] = "100"
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_TYPE")
        self.assertEqual(ctx.exception.at, "/logical_time")

    def test_domain_violation(self):
        data = base_case()
        data["keys"][0]["initial_value"] = 9
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_DOMAIN")

    def test_per_key_version_must_increment_by_one(self):
        data = base_case()
        data["transactions"][1]["writes"][0]["new_version"] = 5
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_VERSION")

    def test_history_invariant_checkpoint(self):
        # Every historical event is checkpointed. T1 yields (x=1,y=1); T3 then
        # forces y=0 while x=1, violating g at T3's commit checkpoint.
        data = base_case()
        data["transactions"] = [
            {
                "txn_id": "T1", "committed_at": 10,
                "writes": [
                    {"key": "x", "old_value": 0, "new_value": 1, "new_version": 1},
                    {"key": "y", "old_value": 0, "new_value": 1, "new_version": 1},
                ],
            },
            {
                "txn_id": "T3", "committed_at": 30,
                "writes": [{"key": "y", "old_value": 1, "new_value": 0, "new_version": 2}],
            },
        ]
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_INVARIANT")
        self.assertEqual(ctx.exception.details["constraint_id"], "g")
        self.assertIn("after commit", ctx.exception.details["stage"])

    def test_historical_rollback_checkpoint_is_replayed(self):
        # legal history with a historical rollback; prove rollback events are
        # replayed (value restored, version advanced) via World in a separate
        # dedicated test, here just confirm this legal shape loads.
        data = base_case()
        data["transactions"] = [
            {
                "txn_id": "T1", "committed_at": 10,
                "writes": [
                    {"key": "x", "old_value": 0, "new_value": 1, "new_version": 1},
                    {"key": "y", "old_value": 0, "new_value": 1, "new_version": 1},
                ],
            },
            {
                "txn_id": "T2", "committed_at": 20,
                "writes": [{"key": "x", "old_value": 1, "new_value": 0, "new_version": 2}],
                "rolled_back": {"at": 50},
            },
        ]
        case = load_case(data)  # no exception: every checkpoint legal
        from solution.engine import World
        world = World(case)
        self.assertEqual(world.state["x"], 1)
        self.assertEqual(world.versions["x"], 3)

    def test_duplicate_txn_id(self):
        data = base_case()
        data["transactions"][1]["txn_id"] = "T1"
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_DUPLICATE")

    def test_unknown_key_reference(self):
        data = base_case()
        data["constraints"][0]["requires"][0]["key"] = "ghost"
        with self.assertRaises(LedgerError) as ctx:
            load_case(data)
        self.assertEqual(ctx.exception.code, "E_REF")

    def test_historical_rollback_replay(self):
        data = base_case()
        # roll T2 back historically: x goes 0 -> 1 again, version 3
        data["transactions"][1]["rolled_back"] = {"at": 50}
        case = load_case(data)
        from solution.engine import World
        world = World(case)
        self.assertEqual(world.versions["x"], 3)
        self.assertEqual(world.state["x"], 1)
        self.assertNotIn("T2", world.active)

    def test_cli_json_error_and_missing_file(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        code, out = cli(["validate", bad])
        self.assertEqual(code, 2)
        self.assertEqual(out["code"], "E_JSON")
        self.assertIn("/line/", out["at"])

        code, out = cli(["validate", os.path.join(self.tmp, "nope.json")])
        self.assertEqual(out["code"], "E_FILE")

    def test_cli_validate_success(self):
        path = write_case(self.tmp, "ok.json", base_case())
        code, out = cli(["validate", path])
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "valid")
        self.assertEqual(out["current_versions"], {"x": 2, "y": 1})


if __name__ == "__main__":
    unittest.main()
