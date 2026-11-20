"""Programmatic fictional-case builders for the test suite."""

from __future__ import annotations

import random
from typing import Any

from solution.engine import World
from solution.validation import load_case


def build_case(
    *,
    case_id: str = "t",
    logical_time: int = 100,
    policy_generation: int = 1,
    keys: dict[str, list[Any]] | None = None,
    initial: dict[str, Any] | None = None,
    constraints: list[dict[str, Any]] | None = None,
    txns: list[dict[str, Any]] | None = None,
    apps: list[dict[str, Any]] | None = None,
    approvals: list[dict[str, Any]] | None = None,
    fault: dict[str, Any] | None = None,
) -> dict[str, Any]:
    keys = keys or {}
    initial = initial or {k: v[0] for k, v in keys.items()}
    out: dict[str, Any] = {
        "case_id": case_id,
        "logical_time": logical_time,
        "policy_generation": policy_generation,
        "keys": [
            {"key": k, "domain": list(dom), "initial_value": initial[k]}
            for k, dom in keys.items()
        ],
        "constraints": constraints or [],
        "transactions": txns or [],
        "applications": apps or [],
        "approvals": approvals or [],
    }
    if fault is not None:
        out["fault"] = fault
    return out


def txn(tid: str, at: int, writes: list[tuple[str, Any, Any, int]],
        *, weight: int = 1, rolled_back: int | None = None,
        category: str = "default") -> dict[str, Any]:
    t: dict[str, Any] = {
        "txn_id": tid, "committed_at": at, "weight": weight, "category": category,
        "writes": [
            {"key": k, "old_value": o, "new_value": n, "new_version": v}
            for (k, o, n, v) in writes
        ],
    }
    if rolled_back is not None:
        t["rolled_back"] = {"at": rolled_back}
    return t


def app(aid: str, target: str, *, idem: str | None = None,
        window=(0, 200), requested: int = 50) -> dict[str, Any]:
    return {
        "app_id": aid, "target_txn_id": target,
        "idempotency_key": idem or f"idem-{aid}",
        "window_start": window[0], "window_end": window[1],
        "requested_at": requested,
    }


def approval(apid: str, aid: str, tid: str, reviewer: str,
             bound: dict[str, int], *, decided: int = 60, gen: int = 1,
             revoked: bool = False) -> dict[str, Any]:
    d = {
        "approval_id": apid, "app_id": aid, "txn_id": tid,
        "reviewer": reviewer, "bound_versions": dict(bound),
        "decided_at": decided, "policy_generation": gen,
    }
    if revoked:
        d["revoked"] = True
    return d


def versions_of(case_data: dict[str, Any]) -> dict[str, int]:
    return World(load_case(case_data)).versions


# --------------------------------------------------------------------------- #
# random valid case generation for exhaustive cross-checking
# --------------------------------------------------------------------------- #
def random_case(seed: int, n_txn: int = 5, n_keys: int = 4) -> dict[str, Any]:
    rnd = random.Random(seed)
    key_names = [f"k{i}" for i in range(n_keys)]
    keys = {k: [0, 1, 2] for k in key_names}
    state = {k: 0 for k in key_names}
    ver = {k: 0 for k in key_names}
    txns: list[dict[str, Any]] = []
    for i in range(n_txn):
        n_writes = rnd.randint(1, min(2, n_keys))
        chosen = rnd.sample(key_names, n_writes)
        writes = []
        for k in chosen:
            old = state[k]
            new = rnd.choice(keys[k])
            ver[k] += 1
            writes.append((k, old, new, ver[k]))
            state[k] = new
        txns.append(txn(f"T{i}", 10 * (i + 1), writes))
    data = build_case(
        case_id=f"rand-{seed}", keys=keys, txns=txns,
        apps=[app("APP1", "T0")],
    )
    # For every transaction emit approvals covering every dependency-version
    # vector that can occur, each with a unique reviewer so adjacency never
    # blocks.  Both planners see exactly the same approval pool.
    case = load_case(data)
    max_ver = dict(ver)
    approvals: list[dict[str, Any]] = []
    for t in case.transactions:
        deps = sorted(t.keys)
        vectors = [{}]
        for k in deps:
            vectors = [
                {**vec, k: v} for vec in vectors for v in range(0, max_ver[k] + 1)
            ]
        for j, vec in enumerate(vectors):
            approvals.append(approval(
                f"AP-{t.txn_id}-{j}", "APP1", t.txn_id,
                f"rv-{t.txn_id}-{j}", vec,
            ))
    data["approvals"] = approvals
    return data
