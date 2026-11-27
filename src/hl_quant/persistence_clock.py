"""Wall-clock-aware persistence streaks shared by the paper engines.

Streak gates such as ``min_clean_streak=2`` were historically counted in *script invocations*.
Manual re-runs, worker restarts and bootstrap ticks could therefore satisfy a "2 x 4h" gate in a
few minutes and churn capital through fees. A streak here only advances when at least
``min_spacing_hours`` of wall-clock time has elapsed since its last increment; extra invocations
inside that window leave it unchanged (they neither advance nor reset a still-qualifying streak).

Persisted value format: ``{"n": int, "first_ms": int, "last_inc_ms": int}``. Legacy integer
values are accepted and migrated with ``last_inc_ms=0`` (the next qualifying tick advances).
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from typing import Any

MS_PER_HOUR = 3_600_000
DEFAULT_MIN_SPACING_HOURS = 3.5  # 4h cron cadence with jitter tolerance


def streak_count(value: object) -> int:
    """Return the integer streak length from a persisted streak value (legacy int or dict)."""
    if isinstance(value, dict):
        try:
            return int(value.get("n", 0) or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(value or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _normalize(value: object) -> dict[str, int]:
    if isinstance(value, dict):
        return {
            "n": streak_count(value),
            "first_ms": int(value.get("first_ms", 0) or 0),
            "last_inc_ms": int(value.get("last_inc_ms", 0) or 0),
        }
    return {"n": streak_count(value), "first_ms": 0, "last_inc_ms": 0}


def advance_streak(
    value: object,
    *,
    qualifies: bool,
    now_ms: int,
    min_spacing_hours: float = DEFAULT_MIN_SPACING_HOURS,
) -> dict[str, int]:
    """Advance (or reset) one streak honoring the minimum wall-clock spacing between increments."""
    cur = _normalize(value)
    if not qualifies:
        return {"n": 0, "first_ms": 0, "last_inc_ms": 0}
    spacing_ms = int(max(0.0, min_spacing_hours) * MS_PER_HOUR)
    if cur["n"] <= 0:
        return {"n": 1, "first_ms": now_ms, "last_inc_ms": now_ms}
    if now_ms - cur["last_inc_ms"] >= spacing_ms:
        return {"n": cur["n"] + 1, "first_ms": cur["first_ms"] or now_ms, "last_inc_ms": now_ms}
    return cur


def update_streak_map(
    prev: MutableMapping[str, Any] | None,
    qualifying_keys: Iterable[str],
    *,
    now_ms: int,
    min_spacing_hours: float = DEFAULT_MIN_SPACING_HOURS,
    keep_non_qualifying: bool = False,
) -> dict[str, dict[str, int]]:
    """Return a new streak map: qualifying keys advance, all others reset (dropped by default)."""
    prev_map = dict(prev or {})
    q = set(qualifying_keys)
    out: dict[str, dict[str, int]] = {}
    for key in q:
        out[key] = advance_streak(prev_map.get(key), qualifies=True, now_ms=now_ms, min_spacing_hours=min_spacing_hours)
    if keep_non_qualifying:
        for key in prev_map:
            if key not in q:
                out[key] = {"n": 0, "first_ms": 0, "last_inc_ms": 0}
    return out
