"""Live status check: ask the business's state registry about it right now (one API call), and say
what changed since our last monthly pull. Read-only: it never rewrites our data (the monthly pull
does that), it only reports.

Supported for CO, NY, CT, OR and TX (Socrata APIs). Iowa's data hub has no single-record API.
Results are cached per business for 10 minutes so repeated views don't hit the state again.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone

import psycopg

from ..ingest import states
from ..normalize import normalize_name
from ..states_meta import STATE_NAMES
from .business import NotFound

CACHE_TTL = 600
_CACHE_MAX = 5000
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _cached(key: str) -> dict | None:
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1]
    return None


def _store(key: str, value: dict) -> None:
    with _lock:
        if len(_cache) >= _CACHE_MAX:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 10]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def check_business(conn: psycopg.Connection, business_id: str) -> dict:
    try:
        bid = uuid.UUID(str(business_id))
    except ValueError:
        raise NotFound(f"No business with id {business_id!r}")
    b = conn.execute(
        "SELECT b.id, b.legal_name, b.status, b.address_line1, b.state, b.primary_source, "
        "       sr.source_id, sr.pulled_at "
        "  FROM businesses b LEFT JOIN source_records sr ON sr.id = b.id WHERE b.id = %s", (bid,)
    ).fetchone()
    if not b:
        raise NotFound(f"No business with id {business_id!r}")

    state_name = STATE_NAMES.get(b["state"] or "", b["state"] or "the state")
    base = {"business_id": str(bid), "state": b["state"],
            "our_data_as_of": b["pulled_at"].date().isoformat() if b["pulled_at"] else None}
    if b["primary_source"] not in states.SOURCES:
        return {**base, "checked": False, "verdict": "unsupported",
                "message": "This business came from SBA loan data, not a state registry, so there's no registry "
                           "record to check."}
    src = states.SOURCES[b["primary_source"]]
    if not src.live_id_field:
        return {**base, "checked": False, "verdict": "unsupported",
                "message": f"{state_name}'s registry doesn't offer a live lookup. Status is as of our last pull "
                           f"({base['our_data_as_of']})."}

    cache_key = f"{src.key}:{b['source_id']}"
    res = _cached(cache_key)
    cached = res is not None
    if res is None:
        res = states.live_lookup(src.key, b["source_id"])
        res["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if res.get("ok"):                 # don't cache failures; let the next click retry
            _store(cache_key, res)

    out = {**base, "source": src.attribution, "registry_id": b["source_id"], "cached": cached,
           "checked_at": res.get("checked_at"), "registry_status_raw": res.get("raw_status") or {}, "changes": []}
    if not res.get("ok"):
        return {**out, "checked": False, "verdict": "error", "message": res.get("error", "Lookup failed.")}
    if not res["found"]:
        return {**out, "checked": True, "verdict": "not_found",
                "message": f"No longer on {state_name}'s list of active businesses. It may have dissolved, merged "
                           f"or lapsed since our {base['our_data_as_of']} data. Confirm before reaching out."}
    if not res["active"] and res.get("record") is None:
        raw = ", ".join(f"{v}" for v in out["registry_status_raw"].values()) or res.get("status")
        return {**out, "checked": True, "verdict": "inactive",
                "message": f"{state_name} now lists it as: {raw}."}

    live = res["record"]
    changes = []
    if res["status"] != b["status"]:
        changes.append({"field": "status", "ours": b["status"], "live": res["status"]})
    if normalize_name(live["legal_name"]) != normalize_name(b["legal_name"]):
        changes.append({"field": "name", "ours": b["legal_name"], "live": live["legal_name"]})
    if live.get("address_line1") and live["address_line1"] != b["address_line1"]:
        changes.append({"field": "address", "ours": b["address_line1"], "live": live["address_line1"]})
    raw = ", ".join(str(v) for v in out["registry_status_raw"].values())
    verdict = "active" if res["status"] == "active" else res["status"]
    msg = (f"Still {'active' if verdict == 'active' else verdict} with {state_name}"
           + (f" ({raw})" if raw else "") + ".")
    if changes:
        msg += " Changed since our last pull: " + ", ".join(c["field"] for c in changes) + "."
    return {**out, "checked": True, "verdict": verdict, "message": msg, "changes": changes, "live_record": live}
