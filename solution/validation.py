"""Case loading and strict structural/semantic validation.

Every failure raises :class:`LedgerError` with a stable code and a locatable
JSON-pointer style ``at`` path.
"""

from __future__ import annotations

from typing import Any

from .errors import LedgerError
from .model import (
    App,
    Approval,
    Case,
    Constraint,
    KeySpec,
    Requirement,
    Txn,
    Write,
)


# --------------------------------------------------------------------------- #
# low level type helpers
# --------------------------------------------------------------------------- #
def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _need(obj: Any, key: str, typ: type | tuple[type, ...], at: str, *,
          default: Any = ()) -> Any:
    if not isinstance(obj, dict):
        raise LedgerError("E_TYPE", f"expected object, got {type(obj).__name__}", at=at)
    if key not in obj:
        if default != ():
            return default
        raise LedgerError("E_MISSING_FIELD", f"missing required field '{key}'",
                          at=f"{at.rstrip('/')}/{key}")
    val = obj[key]
    if not isinstance(val, typ) or (typ is int and isinstance(val, bool)):
        name = typ.__name__ if isinstance(typ, type) else "/".join(t.__name__ for t in typ)
        raise LedgerError(
            "E_TYPE", f"field '{key}' must be {name}, got {type(val).__name__}",
            at=f"{at.rstrip('/')}/{key}",
        )
    return val


def _need_str(obj: dict, key: str, at: str) -> str:
    val = _need(obj, key, str, at)
    if not val:
        raise LedgerError("E_VALUE", f"field '{key}' must be non-empty", at=f"{at}/{key}")
    return val


def _need_int(obj: dict, key: str, at: str) -> int:
    return _need(obj, key, int, at)


# --------------------------------------------------------------------------- #
# section parsers
# --------------------------------------------------------------------------- #
def _parse_keys(raw: Any) -> dict[str, KeySpec]:
    at = "/keys"
    if not isinstance(raw, list):
        raise LedgerError("E_TYPE", "keys must be an array", at=at)
    out: dict[str, KeySpec] = {}
    for i, item in enumerate(raw):
        p = f"{at}/{i}"
        key = _need_str(item, "key", p)
        if key in out:
            raise LedgerError("E_DUPLICATE", f"duplicate key '{key}'", at=f"{p}/key")
        domain = _need(item, "domain", list, p)
        if not domain:
            raise LedgerError("E_VALUE", "domain must be non-empty", at=f"{p}/domain")
        initial = _need(item, "initial_value", object, p)
        if initial not in domain:
            raise LedgerError(
                "E_DOMAIN",
                f"initial_value {initial!r} outside declared domain",
                at=f"{p}/initial_value",
            )
        out[key] = KeySpec(key=key, domain=tuple(domain), initial_value=initial)
    if not out:
        raise LedgerError("E_VALUE", "at least one key is required", at=at)
    return out


def _parse_constraints(raw: Any, keys: dict[str, KeySpec]) -> tuple[Constraint, ...]:
    at = "/constraints"
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LedgerError("E_TYPE", "constraints must be an array", at=at)
    out: list[Constraint] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        p = f"{at}/{i}"
        cid = _need_str(item, "constraint_id", p)
        if cid in seen_ids:
            raise LedgerError("E_DUPLICATE", f"duplicate constraint_id '{cid}'", at=f"{p}/constraint_id")
        seen_ids.add(cid)
        active_key = _need_str(item, "active_key", p)
        if active_key not in keys:
            raise LedgerError("E_REF", f"unknown key '{active_key}'", at=f"{p}/active_key")
        active_values = _need(item, "active_values", list, p)
        for v in active_values:
            if v not in keys[active_key].domain:
                raise LedgerError(
                    "E_DOMAIN", f"active value {v!r} outside domain of '{active_key}'",
                    at=f"{p}/active_values",
                )
        reqs_raw = _need(item, "requires", list, p)
        reqs: list[Requirement] = []
        seen_rk: set[str] = set()
        for j, rr in enumerate(reqs_raw):
            rp = f"{p}/requires/{j}"
            rk = _need_str(rr, "key", rp)
            if rk not in keys:
                raise LedgerError("E_REF", f"unknown key '{rk}'", at=f"{rp}/key")
            if rk in seen_rk:
                raise LedgerError("E_DUPLICATE", f"duplicate requirement on '{rk}'", at=f"{rp}/key")
            seen_rk.add(rk)
            allowed = _need(rr, "allowed", list, rp)
            if not allowed:
                raise LedgerError("E_VALUE", "allowed must be non-empty", at=f"{rp}/allowed")
            for v in allowed:
                if v not in keys[rk].domain:
                    raise LedgerError(
                        "E_DOMAIN", f"allowed value {v!r} outside domain of '{rk}'", at=f"{rp}/allowed"
                    )
            reqs.append(Requirement(key=rk, allowed=tuple(allowed)))
        out.append(
            Constraint(
                constraint_id=cid,
                active_key=active_key,
                active_values=tuple(active_values),
                requires=tuple(reqs),
            )
        )
    return tuple(out)


def _parse_transactions(raw: Any, keys: dict[str, KeySpec]) -> tuple[Txn, ...]:
    at = "/transactions"
    if not isinstance(raw, list) or not raw:
        raise LedgerError("E_VALUE", "transactions must be a non-empty array", at=at)
    out: list[Txn] = []
    seen_ids: set[str] = set()
    seen_times: set[int] = set()
    for i, item in enumerate(raw):
        p = f"{at}/{i}"
        tid = _need_str(item, "txn_id", p)
        if tid in seen_ids:
            raise LedgerError("E_DUPLICATE", f"duplicate txn_id '{tid}'", at=f"{p}/txn_id")
        seen_ids.add(tid)
        when = _need_int(item, "committed_at", p)
        if when < 0 or when in seen_times:
            raise LedgerError("E_VALUE", "committed_at must be unique and non-negative", at=f"{p}/committed_at")
        seen_times.add(when)
        writes_raw = _need(item, "writes", list, p)
        if not writes_raw:
            raise LedgerError("E_VALUE", "transaction must write at least one key", at=f"{p}/writes")
        writes: list[Write] = []
        seen_keys: set[str] = set()
        for j, wr in enumerate(writes_raw):
            wp = f"{p}/writes/{j}"
            k = _need_str(wr, "key", wp)
            if k not in keys:
                raise LedgerError("E_REF", f"unknown key '{k}'", at=f"{wp}/key")
            if k in seen_keys:
                raise LedgerError("E_DUPLICATE", f"key '{k}' written twice in one transaction", at=f"{wp}/key")
            seen_keys.add(k)
            old_v = _need(wr, "old_value", object, wp)
            new_v = _need(wr, "new_value", object, wp)
            if old_v not in keys[k].domain:
                raise LedgerError("E_DOMAIN", f"old_value outside domain of '{k}'", at=f"{wp}/old_value")
            if new_v not in keys[k].domain:
                raise LedgerError("E_DOMAIN", f"new_value outside domain of '{k}'", at=f"{wp}/new_value")
            ver = _need_int(wr, "new_version", wp)
            if ver <= 0:
                raise LedgerError("E_VALUE", "new_version must be positive", at=f"{wp}/new_version")
            writes.append(Write(key=k, old_value=old_v, new_value=new_v, new_version=ver))
        weight = _need(item, "weight", int, p, default=1)
        if weight < 0:
            raise LedgerError("E_VALUE", "weight must be non-negative", at=f"{p}/weight")
        category = _need(item, "category", str, p, default="default")
        rb_raw = item.get("rolled_back")
        rolled_back = False
        rollback_at = 0
        if rb_raw is not None:
            if not isinstance(rb_raw, dict):
                raise LedgerError("E_TYPE", "rolled_back must be an object", at=f"{p}/rolled_back")
            rb_at = _need_int(rb_raw, "at", f"{p}/rolled_back")
            if rb_at <= when:
                raise LedgerError(
                    "E_VALUE", "rolled_back.at must be after committed_at",
                    at=f"{p}/rolled_back/at",
                )
            rolled_back = True
            rollback_at = rb_at
        out.append(
            Txn(
                txn_id=tid,
                committed_at=when,
                writes=tuple(writes),
                weight=weight,
                rolled_back=rolled_back,
                rollback_at=rollback_at,
                category=category,
            )
        )
    out.sort(key=lambda t: t.committed_at)
    return tuple(out)


def _parse_apps(raw: Any, txns: tuple[Txn, ...]) -> dict[str, App]:
    at = "/applications"
    if not isinstance(raw, list):
        raise LedgerError("E_TYPE", "applications must be an array", at=at)
    txn_ids = {t.txn_id for t in txns}
    out: dict[str, App] = {}
    seen_idem: set[str] = set()
    for i, item in enumerate(raw):
        p = f"{at}/{i}"
        aid = _need_str(item, "app_id", p)
        if aid in out:
            raise LedgerError("E_DUPLICATE", f"duplicate app_id '{aid}'", at=f"{p}/app_id")
        target = _need_str(item, "target_txn_id", p)
        if target not in txn_ids:
            raise LedgerError("E_REF", f"unknown target transaction '{target}'", at=f"{p}/target_txn_id")
        if next(t for t in txns if t.txn_id == target).rolled_back:
            raise LedgerError(
                "E_TXN_UNDONE", f"target transaction '{target}' is already undone",
                at=f"{p}/target_txn_id",
            )
        idem = _need_str(item, "idempotency_key", p)
        if idem in seen_idem:
            raise LedgerError("E_DUPLICATE", "duplicate idempotency_key", at=f"{p}/idempotency_key")
        seen_idem.add(idem)
        ws = _need_int(item, "window_start", p)
        we = _need_int(item, "window_end", p)
        ra = _need_int(item, "requested_at", p)
        if ws < 0 or we <= ws:
            raise LedgerError("E_VALUE", "require window_start >= 0 and window_end > window_start", at=p)
        if not (ws <= ra <= we):
            raise LedgerError("E_VALUE", "requested_at must lie inside the window", at=f"{p}/requested_at")
        out[aid] = App(
            app_id=aid, target_txn_id=target, idempotency_key=idem,
            window_start=ws, window_end=we, requested_at=ra,
        )
    return out


def _dep_keys(txn: Txn, constraints: tuple[Constraint, ...]) -> frozenset[str]:
    """Keys whose versions pin a step: written keys plus invariant read-only
    keys appearing in constraint groups touched by this transaction."""
    touched: set[str] = set(txn.keys)
    for c in constraints:
        if txn.keys & c.keys:
            touched.update(c.keys)
    return frozenset(touched)


def _parse_approvals(raw: Any, apps: dict[str, App], txns: tuple[Txn, ...],
                     keys: dict[str, KeySpec],
                     constraints: tuple[Constraint, ...]) -> dict[str, Approval]:
    at = "/approvals"
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise LedgerError("E_TYPE", "approvals must be an array", at=at)
    txn_by_id = {t.txn_id: t for t in txns}
    out: dict[str, Approval] = {}
    for i, item in enumerate(raw):
        p = f"{at}/{i}"
        apid = _need_str(item, "approval_id", p)
        if apid in out:
            raise LedgerError("E_DUPLICATE", f"duplicate approval_id '{apid}'", at=f"{p}/approval_id")
        aid = _need_str(item, "app_id", p)
        if aid not in apps:
            raise LedgerError("E_REF", f"unknown application '{aid}'", at=f"{p}/app_id")
        tid = _need_str(item, "txn_id", p)
        if tid not in txn_by_id:
            raise LedgerError("E_REF", f"unknown transaction '{tid}'", at=f"{p}/txn_id")
        reviewer = _need_str(item, "reviewer", p)
        bv = _need(item, "bound_versions", dict, p)
        if not bv:
            raise LedgerError("E_VALUE", "bound_versions must be non-empty", at=f"{p}/bound_versions")
        dep = _dep_keys(txn_by_id[tid], constraints)
        for bk, bver in bv.items():
            if bk not in keys:
                raise LedgerError("E_REF", f"unknown key '{bk}'", at=f"{p}/bound_versions")
            if not _is_int(bver) or bver < 0:
                raise LedgerError("E_TYPE", "bound versions must be non-negative integers",
                                  at=f"{p}/bound_versions/{bk}")
            if bk not in dep:
                raise LedgerError(
                    "E_APPROVAL_SCOPE",
                    f"key '{bk}' is not a dependency key of transaction '{tid}'",
                    at=f"{p}/bound_versions/{bk}",
                )
        decided = _need_int(item, "decided_at", p)
        if decided < 0:
            raise LedgerError("E_VALUE", "decided_at must be non-negative", at=f"{p}/decided_at")
        gen = _need_int(item, "policy_generation", p)
        if gen <= 0:
            raise LedgerError("E_VALUE", "policy_generation must be positive", at=f"{p}/policy_generation")
        revoked = bool(item.get("revoked", False))
        out[apid] = Approval(
            approval_id=apid, app_id=aid, txn_id=tid, reviewer=reviewer,
            bound_versions=dict(bv), decided_at=decided,
            policy_generation=gen, revoked=revoked,
        )
    return out


# --------------------------------------------------------------------------- #
# history replay
# --------------------------------------------------------------------------- #
def _check_invariants(state: dict[str, Any], constraints: tuple[Constraint, ...],
                      at: str, stage: str) -> None:
    for c in constraints:
        if state[c.active_key] not in c.active_values:
            continue
        for r in c.requires:
            if state[r.key] not in r.allowed:
                raise LedgerError(
                    "E_INVARIANT",
                    (f"constraint '{c.constraint_id}' violated at {stage}: "
                     f"{c.active_key} active but '{r.key}'={state[r.key]!r} "
                     f"not in {list(r.allowed)!r}"),
                    at=at,
                    details={"constraint_id": c.constraint_id, "key": r.key, "stage": stage},
                )


def replay_history(case_data: dict, keys: dict[str, KeySpec],
                   constraints: tuple[Constraint, ...],
                   txns: tuple[Txn, ...]) -> dict[str, int]:
    """Replay every historical event in logical order, validating the stored
    old/new values, per-key versions and every invariant checkpoint.

    Returns the current per-key version (count of write events, both commits
    and rollback compensations, starting at version 0).
    """
    state = {k: spec.initial_value for k, spec in keys.items()}
    versions = {k: 0 for k in keys}
    _check_invariants(state, constraints, "/keys", "initial state")

    # events: (logical time, ordering, txn, kind)
    events: list[tuple[int, int, Txn, str]] = []
    for idx, t in enumerate(txns):
        events.append((t.committed_at, idx, t, "commit"))
        if t.rolled_back:
            rb_at = case_data["transactions"][idx]["rolled_back"]["at"]
            events.append((rb_at, idx, t, "rollback"))
    events.sort(key=lambda e: (e[0], 0 if e[3] == "commit" else 1))

    for when, idx, t, kind in events:
        at = f"/transactions/{idx}"
        for w in t.writes:
            if kind == "commit":
                if state[w.key] != w.old_value:
                    raise LedgerError(
                        "E_HISTORY",
                        f"stored old_value mismatch on '{w.key}' in '{t.txn_id}'",
                        at=f"{at}/writes",
                    )
                if versions[w.key] + 1 != w.new_version:
                    raise LedgerError(
                        "E_VERSION",
                        (f"per-key version of '{w.key}' must advance by exactly one: "
                         f"have {versions[w.key]}, new_version {w.new_version}"),
                        at=f"{at}/writes",
                    )
                state[w.key] = w.new_value
                versions[w.key] = w.new_version
            else:  # historical rollback: restore the recorded old value, version +1
                if state[w.key] != w.new_value:
                    raise LedgerError(
                        "E_HISTORY",
                        (f"historical rollback of '{t.txn_id}' expected current value "
                         f"{w.new_value!r} on '{w.key}'"),
                        at=f"{at}/rolled_back",
                    )
                state[w.key] = w.old_value
                versions[w.key] += 1
        _check_invariants(
            state, constraints, at,
            f"after {kind} of {t.txn_id}",
        )
    return versions


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def load_case(data: Any) -> Case:
    if not isinstance(data, dict):
        raise LedgerError("E_TYPE", "top-level case must be an object", at="/")
    case_id = _need_str(data, "case_id", "/")
    logical_time = _need_int(data, "logical_time", "/")
    if logical_time < 0:
        raise LedgerError("E_VALUE", "logical_time must be non-negative", at="/logical_time")
    gen = _need_int(data, "policy_generation", "/")
    if gen <= 0:
        raise LedgerError("E_VALUE", "policy_generation must be positive", at="/policy_generation")

    keys = _parse_keys(_need(data, "keys", list, "/"))
    constraints = _parse_constraints(data.get("constraints"), keys)
    txns = _parse_transactions(_need(data, "transactions", list, "/"), keys)

    # full history replay (versions, values, invariant checkpoints)
    replay_history(data, keys, constraints, txns)

    apps = _parse_apps(data.get("applications", []), txns)
    approvals = _parse_approvals(
        data.get("approvals"), apps, txns, keys, constraints
    )
    fault = data.get("fault")
    if fault is not None and not isinstance(fault, dict):
        raise LedgerError("E_TYPE", "fault must be an object", at="/fault")

    return Case(
        case_id=case_id,
        logical_time=logical_time,
        policy_generation=gen,
        keys=keys,
        constraints=constraints,
        transactions=txns,
        apps=apps,
        approvals=approvals,
        fault=fault or {},
    )


def dep_keys(txn: Txn, constraints: tuple[Constraint, ...]) -> frozenset[str]:
    """Public re-export: dependency keys (write set + invariant read-only keys)."""
    return _dep_keys(txn, constraints)
