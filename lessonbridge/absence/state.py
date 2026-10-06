"""Day state: the exact content of one calendar day, as a proposal sees it.

Every proposal row records the state it expects to find (``before``) and the
state it will write (``after``). Approval refuses to apply a row whose live
entry no longer matches ``before`` (LB-03), and the diff view lists every row
whose state changes (LB-04).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from ..models import CalendarEntry, EntryKind

STATE_FIELDS = (
    "exists", "lesson_id", "lesson_slug", "unit_id", "kind", "title", "merged_keys",
    "continuation", "is_sub_day", "absence_id", "replacement_activity_id", "status",
)


def entry_state(entry: Optional[CalendarEntry]) -> dict[str, Any]:
    if entry is None:
        return {"exists": False, "lesson_id": None, "lesson_slug": None, "unit_id": None, "kind": None, "title": None,
                "merged_keys": [], "continuation": 0, "is_sub_day": False, "absence_id": None, "replacement_activity_id": None, "status": "planned"}
    return {
        "exists": True,
        "lesson_id": entry.lesson_id,
        "lesson_slug": entry.lesson.slug if entry.lesson else None,
        "unit_id": entry.unit_id,
        "kind": entry.kind.value if entry.kind else None,
        "title": entry.title,
        "merged_keys": list(entry_merged_keys(entry)),
        "continuation": int(entry.continuation or 0),
        "is_sub_day": bool(entry.is_sub_day),
        "absence_id": entry.absence_id,
        "replacement_activity_id": entry.replacement_activity_id,
        "status": entry.status.value if entry.status else "planned",
    }


def entry_merged_keys(entry: CalendarEntry) -> list[str]:
    """Merged items in teaching order. Older rows only have lesson ids; derive keys from them."""
    if entry.merged_keys:
        return list(entry.merged_keys)
    if entry.merged_lesson_ids:
        from sqlalchemy.orm import object_session

        from ..models import Lesson

        sess = object_session(entry)
        out = []
        for lid in entry.merged_lesson_ids:
            l = sess.get(Lesson, lid) if sess else None
            if l is not None:
                out.append(l.slug)
        return out
    return []


def same_state(a: Optional[dict], b: Optional[dict]) -> bool:
    if a is None or b is None:
        return a is b
    return all(_norm(a.get(f)) == _norm(b.get(f)) for f in STATE_FIELDS)


def _norm(v):
    if isinstance(v, list):
        return tuple(v)
    return v


def state_keys(state: Optional[dict]) -> list[str]:
    """Identity tokens taught on the day: merged keys then the lesson's own key."""
    if not state or not state.get("exists"):
        return []
    out = list(state.get("merged_keys") or [])
    if state.get("lesson_slug"):
        cont = int(state.get("continuation") or 0)
        out.append(state["lesson_slug"] + (f"#c{cont}" if cont else ""))
    return out


def state_slugs(state: Optional[dict]) -> list[str]:
    return [k.split("#", 1)[0] for k in state_keys(state)]


def fingerprint(state: dict) -> str:
    """Stable hash of the parts of a day that a substitute plan depends on."""
    keep = {k: state.get(k) for k in ("lesson_id", "kind", "title", "merged_keys", "continuation", "is_sub_day", "replacement_activity_id")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:32]


def write_state(entry: CalendarEntry, state: dict, merged_lesson_ids: list[int]) -> None:
    """Write a day state onto an entry. Notes and progress status are left untouched."""
    entry.lesson_id = state.get("lesson_id")
    if state.get("unit_id") is not None:
        entry.unit_id = state["unit_id"]
    entry.kind = EntryKind(state.get("kind") or "flex")
    entry.title = state.get("title") or "Flex / work day"
    entry.merged_keys = list(state.get("merged_keys") or [])
    entry.merged_lesson_ids = list(merged_lesson_ids)
    entry.continuation = int(state.get("continuation") or 0)
    entry.is_sub_day = bool(state.get("is_sub_day"))
    entry.absence_id = state.get("absence_id")
    entry.replacement_activity_id = state.get("replacement_activity_id")
