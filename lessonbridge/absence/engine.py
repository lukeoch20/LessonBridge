"""Pure planning core shared by the decision engine and the reconciler.

Everything here operates on plain dataclasses so the algorithms can be unit
tested without a database. ``service.py`` adapts ORM rows to these shapes.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

# ----------------------------------------------------------------------- shapes
@dataclass
class Item:
    """Something that needs one course-day: a lesson, a flex day or a sub-day payload."""

    key: str
    title: str
    kind: str = "lesson"  # lesson | review | assessment | flex | filler
    lesson_id: Optional[int] = None
    lesson_type: str = "direct_instruction"
    priority: str = "required"
    duration: int = 50
    min_minutes: int = 30
    can_move: bool = True
    quarter_boundary_allowed: bool = False
    hard_date: Optional[date] = None
    prereqs: set[str] = field(default_factory=set)
    delivery: set[str] = field(default_factory=lambda: {"regular_teacher", "long_term_sub"})
    unit: str = ""
    merged: list[str] = field(default_factory=list)  # keys folded into this item
    pinned: Optional[date] = None
    is_sub_day: bool = False
    payload: dict = field(default_factory=dict)  # replacement activity etc.
    sequence: int = 0

    @property
    def is_assessment(self) -> bool:
        return self.kind == "assessment" or self.lesson_type == "assessment"

    @property
    def is_flex(self) -> bool:
        return self.kind in ("flex", "placeholder")

    @property
    def slack_rank(self) -> int:
        """Lower is dropped first: pure flex before unit placeholders."""
        return 0 if self.kind == "flex" else 1


@dataclass
class Slot:
    date: date
    quarter: Optional[int] = None
    early_release: bool = False
    minutes: int = 50
    existing_key: Optional[str] = None
    existing_title: Optional[str] = None
    existing_kind: str = "lesson"


@dataclass
class Change:
    date: date
    action: str  # shift | merge | compress | replace | defer | drop | preserve | insert | keep
    before_key: Optional[str]
    before_title: Optional[str]
    after_key: Optional[str]
    after_title: Optional[str]
    after_kind: str = "lesson"
    merged: list[str] = field(default_factory=list)
    is_sub_day: bool = False
    reason: str = ""
    new_date: Optional[date] = None
    payload: dict = field(default_factory=dict)


@dataclass
class RepairResult:
    assignments: list[tuple[Slot, Optional[Item]]]
    changes: list[Change]
    deferred: list[Item]
    dropped: list[Item]
    merged_pairs: list[tuple[Item, Item]]
    unresolved: list[str]
    soft_notes: list[str]
    hard_constraints: list[str]


MERGE_ORDER = [
    ("direct_instruction", "guided_practice"),
    ("guided_practice", "review"),
    ("guided_practice", "independent_practice"),
    ("independent_practice", "review"),
    ("review", "assessment"),
    ("reading", "reading"),
    ("direct_instruction", "independent_practice"),
    ("direct_instruction", "direct_instruction"),
]


def _merge_rank(a: Item, b: Item) -> int | None:
    if a.is_assessment and b.is_assessment:
        return None
    if a.is_assessment:  # never put something *after* an assessment on its day
        return None
    if not (a.can_move and b.can_move):
        return None
    pair = (a.lesson_type, b.lesson_type)
    for i, p in enumerate(MERGE_ORDER):
        if pair == p:
            return i
    return len(MERGE_ORDER) + 1  # allowed but least preferred


def merge_items(a: Item, b: Item, minutes: int, study_minutes: int = 0) -> Item | None:
    """Fold ``a`` into ``b``'s day if both fit in one class period (minimum viable minutes)."""
    need = a.min_minutes + b.min_minutes + (study_minutes if b.is_assessment else 0)
    if need > minutes:
        return None
    if _merge_rank(a, b) is None:
        return None
    title = f"{a.title} + {'shortened ' if b.min_minutes < b.duration else ''}{b.title}".replace("shortened Shortened", "shortened")
    if b.is_assessment:
        title = f"{a.title} + {b.title}"
    return replace(
        b,
        key=b.key,
        title=title,
        merged=[*a.merged, a.key, *b.merged],
        prereqs=(a.prereqs | b.prereqs) - {a.key, b.key},
        min_minutes=need,
        duration=minutes,
    )


# ------------------------------------------------------------------- repair
def _prereqs_ok(item: Item, placed_before: set[str], all_keys: set[str]) -> bool:
    relevant = {p for p in item.prereqs if p in all_keys}
    return relevant <= placed_before


def repair(
    slots: list[Slot],
    items: list[Item],
    *,
    minutes: int,
    study_minutes: int = 0,
    boundary_label: str = "quarter end",
    allow_defer: bool = True,
    soft_prefs: Optional[dict] = None,
) -> RepairResult:
    """Fit ``items`` (in sequence order) into ``slots`` under hard constraints.

    Hard constraints: pinned dates, the window boundary (last slot), prerequisite
    order. Repair actions in order of preference: absorb slack (flex), drop
    optional, merge/compress, drop recommended, defer across the boundary when
    the lesson allows it; anything left is reported unresolved.
    """
    soft_prefs = soft_prefs or {}
    items = [replace(i, merged=list(i.merged), prereqs=set(i.prereqs)) for i in items]
    all_keys = {i.key for i in items} | {m for i in items for m in i.merged}
    hard: list[str] = [f"Window ends at {boundary_label} ({slots[-1].date})" if slots else "No slots available"]
    unresolved: list[str] = []
    soft_notes: list[str] = []
    dropped: list[Item] = []
    deferred: list[Item] = []
    merged_pairs: list[tuple[Item, Item]] = []
    changes_reasons: dict[str, str] = {}

    slot_dates = [s.date for s in slots]
    # Un-pin anything pinned outside the window (it will be placed in order).
    pinned = [i for i in items if i.pinned in slot_dates]
    for i in items:
        if i.hard_date and i.hard_date in slot_dates and not i.pinned:
            i.pinned = i.hard_date
            hard.append(f"{i.title} is fixed on {i.hard_date}")
    pinned = [i for i in items if i.pinned in slot_dates]
    for p in pinned:
        if p.is_sub_day:
            hard.append(f"{p.pinned}: substitute day ({p.title})")

    def free_items() -> list[Item]:
        return [i for i in items if i.pinned not in slot_dates]

    def free_slots() -> list[Slot]:
        taken = {i.pinned for i in items if i.pinned in slot_dates}
        return [s for s in slots if s.date not in taken]

    # Validate pinned lessons against prerequisite order; unpin when a prerequisite was displaced after them.
    for _ in range(10):
        changed = False
        order_keys = [i.key for i in sorted(items, key=lambda x: x.sequence)]
        for p in [i for i in items if i.pinned in slot_dates and not i.is_sub_day and not i.hard_date]:
            later_prereqs = [q for q in free_items() if q.key in p.prereqs]
            # A free prerequisite will be placed in sequence order; it lands before p only if a free slot precedes p's date.
            earlier_free = [s for s in free_slots() if s.date < p.pinned]
            need = sum(1 for q in free_items() if q.sequence < p.sequence and not q.is_flex)
            if later_prereqs and need > len(earlier_free):
                p.pinned = None
                changes_reasons[p.key] = "prerequisite instruction was displaced, so this moved with it"
                changed = True
        if not changed:
            break

    capacity = len(free_slots())
    pool = sorted(free_items(), key=lambda x: x.sequence)

    def over() -> int:
        return len([i for i in pool]) - capacity

    # 1. Absorb slack: the nearest pure flex day disappears first (then placeholders), so the cascade stays short.
    while over() > 0 and any(i.is_flex for i in pool):
        best_idx = min((i for i in range(len(pool)) if pool[i].is_flex), key=lambda i: (pool[i].slack_rank, i))
        pool.pop(best_idx)
    # 2. Drop optional lessons (from the end).
    while over() > 0 and any(i.priority == "optional" for i in pool):
        for idx in range(len(pool) - 1, -1, -1):
            if pool[idx].priority == "optional":
                dropped.append(pool.pop(idx))
                break
    # 3. Merge / compress adjacent compatible lessons.
    while over() > 0:
        best = None
        for idx in range(len(pool) - 1):
            a, b = pool[idx], pool[idx + 1]
            if a.is_flex or b.is_flex or a.unit != b.unit:
                continue
            m = merge_items(a, b, minutes, study_minutes)
            if m is None:
                continue
            rank = _merge_rank(a, b) or 0
            if best is None or rank < best[0]:
                best = (rank, idx, m)
        if best is None:
            break
        _, idx, m = best
        merged_pairs.append((pool[idx], pool[idx + 1]))
        pool[idx : idx + 2] = [m]
    # 4. Drop recommended lessons.
    while over() > 0 and any(i.priority == "recommended" for i in pool):
        for idx in range(len(pool) - 1, -1, -1):
            if pool[idx].priority == "recommended":
                dropped.append(pool.pop(idx))
                break
    # 5. Defer across the boundary when allowed (take from the end to keep order).
    while over() > 0 and allow_defer:
        candidates = [i for i in pool if i.quarter_boundary_allowed or (not i.is_assessment and i.priority != "required")]
        if not candidates:
            # Required instruction in the next unit may legitimately start next quarter: defer trailing items that
            # are not assessments and whose unit differs from the first item's unit.
            trailing = [i for i in pool[::-1] if not i.is_assessment and i.unit != pool[0].unit]
            candidates = trailing[:1]
        if not candidates:
            break
        item = candidates[-1]
        pool.remove(item)
        deferred.insert(0, item)
    if over() > 0:
        unresolved.append(f"{over()} required item(s) cannot fit before the {boundary_label}: " + ", ".join(i.title for i in pool[capacity:]))
        # Still produce a plan: overflow items are carried as deferred so the teacher sees them.
        overflow = pool[capacity:]
        pool = pool[:capacity]
        deferred = overflow + deferred

    # 6. Assign.
    fs = free_slots()
    assigned: dict[date, Item] = {i.pinned: i for i in items if i.pinned in slot_dates}
    for slot, item in zip(fs, pool):
        assigned[slot.date] = item
    # 7. Soft constraints: no assessments on Monday when a harmless swap exists; review before quiz.
    dates = [s.date for s in slots]
    if soft_prefs.get("no_monday_assessments", True):
        for i, d in enumerate(dates):
            it = assigned.get(d)
            if it and it.is_assessment and d.isoweekday() == 1 and not it.pinned:
                nxt = dates[i + 1] if i + 1 < len(dates) else None
                nx = assigned.get(nxt) if nxt else None
                if nx and not nx.pinned and not nx.is_assessment and it.key not in nx.prereqs and nx.unit != it.unit:
                    assigned[d], assigned[nxt] = nx, it
                    soft_notes.append(f"Moved {it.title} off Monday {d} to {nxt} (swapped with {nx.title}).")
                else:
                    soft_notes.append(f"{it.title} lands on a Monday ({d}); no harmless swap was available.")
    for i, d in enumerate(dates):
        it = assigned.get(d)
        if it and it.is_assessment and i > 0:
            prev = assigned.get(dates[i - 1])
            if prev and prev.kind not in ("review",) and "review" not in prev.title.lower() and not prev.is_sub_day:
                soft_notes.append(f"No review day immediately before {it.title} ({d}).")

    # 8. Diff.
    changes: list[Change] = []
    for slot in slots:
        it = assigned.get(slot.date)
        before_key, before_title = slot.existing_key, slot.existing_title
        if it is None:
            if before_key:
                changes.append(Change(slot.date, "shift", before_key, before_title, None, "Flex / work day", "flex", reason="slack after reconciliation"))
            continue
        if it.is_sub_day:
            action = "replace" if it.kind == "filler" else "keep"
            reason = it.payload.get("rationale", "")
            changes.append(Change(slot.date, action, before_key, before_title, it.key, it.title, it.kind, list(it.merged), True, reason, payload=dict(it.payload)))
        elif it.merged:
            changes.append(Change(slot.date, "merge" if "+" in it.title else "compress", before_key, before_title, it.key, it.title, it.kind, list(it.merged), False, "combined to recover a lost day"))
        elif it.key == before_key or (it.is_flex and slot.existing_kind in ("flex", "placeholder") and it.title == before_title):
            changes.append(Change(slot.date, "preserve" if (it.hard_date or it.pinned) else "keep", before_key, before_title, it.key, it.title, it.kind))
        else:
            changes.append(Change(slot.date, "shift", before_key, before_title, it.key, it.title, it.kind, reason=changes_reasons.get(it.key, "moved to keep the sequence intact")))
    for d in dropped:
        changes.append(Change(slots[-1].date if slots else date.today(), "drop", d.key, d.title, None, None, reason=f"{d.priority} content removed to fit before the {boundary_label}"))
    for d in deferred:
        changes.append(Change(slots[-1].date if slots else date.today(), "defer", d.key, d.title, None, None, reason=f"moved past the {boundary_label}"))
    assignments = [(s, assigned.get(s.date)) for s in slots]
    return RepairResult(assignments, changes, deferred, dropped, merged_pairs, unresolved, soft_notes, hard)
