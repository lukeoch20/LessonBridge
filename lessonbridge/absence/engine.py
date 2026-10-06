"""Pure planning core shared by the decision engine and the reconciler.

Everything here operates on plain dataclasses so the algorithms can be unit
tested without a database. ``service.py`` adapts ORM rows to these shapes.

The engine never deletes anything. Items that do not fit in a window are
returned in ``deferred`` (in sequence order) so the caller can carry them into
the next quarter segment or, as a last resort, the owed-lesson backlog.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable, Optional


# ----------------------------------------------------------------------- shapes
@dataclass
class Item:
    """Something that needs one course-day: a lesson, a slack day or a substitute-day payload."""

    key: str
    title: str
    kind: str = "lesson"  # lesson | review | assessment | flex | placeholder | filler
    lesson_id: Optional[int] = None
    lesson_slug: Optional[str] = None
    lesson_type: str = "direct_instruction"
    priority: str = "required"
    duration: int = 50
    min_minutes: int = 30
    can_move: bool = True
    quarter_boundary_allowed: bool = False
    hard_date: Optional[date] = None
    prereqs: set[str] = field(default_factory=set)  # lesson slugs
    delivery: set[str] = field(default_factory=lambda: {"regular_teacher", "long_term_sub"})
    unit: str = ""
    unit_id: Optional[int] = None
    merged: list[str] = field(default_factory=list)  # keys folded into this item, in teaching order
    continuation: int = 0
    pinned: Optional[date] = None
    is_sub_day: bool = False
    absence_id: Optional[int] = None
    replacement_id: Optional[int] = None
    payload: dict = field(default_factory=dict)
    sequence: float = 0
    origin_date: Optional[date] = None
    required_components: list[str] = field(default_factory=list)
    optional_components: list[str] = field(default_factory=list)

    @property
    def is_assessment(self) -> bool:
        return self.kind == "assessment" or self.lesson_type == "assessment"

    @property
    def is_flex(self) -> bool:
        return self.kind in ("flex", "placeholder")

    @property
    def slack_rank(self) -> int:
        """Lower is absorbed first: pure flex before unit placeholders."""
        return 0 if self.kind == "flex" else 1

    @property
    def slugs(self) -> list[str]:
        """Lesson slugs taught on this item's day, merged ones first."""
        out = [k.split("#", 1)[0] for k in self.merged]
        if self.lesson_slug:
            out.append(self.lesson_slug)
        elif self.kind not in ("flex", "placeholder", "filler"):
            out.append(self.key.split("#", 1)[0])  # engine-level items identify lessons by key
        return out

    @property
    def keys(self) -> list[str]:
        """Identity tokens of everything taught on this item's day (conservation checks use these)."""
        out = list(self.merged)
        if self.lesson_slug or self.kind not in ("flex", "placeholder", "filler"):
            out.append(self.key)
        return out


@dataclass
class Slot:
    date: date
    quarter: Optional[int] = None
    early_release: bool = False
    minutes: int = 50
    existing_key: Optional[str] = None
    existing_title: Optional[str] = None
    existing_kind: str = "lesson"
    # Day of a long-term-substitute absence: free for repair, but whatever lands here stays a substitute day.
    sub_absence_id: Optional[int] = None


@dataclass
class Change:
    date: date
    action: str  # shift | merge | compress | replace | defer | drop | preserve | keep
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
    deferred: list[Item]  # did not fit; caller carries them forward (never lost)
    dropped: list[Item]  # optional / recommended content removed to make room (goes to the backlog)
    merged_pairs: list[tuple[Item, Item]]
    unresolved: list[str]
    soft_notes: list[str]
    hard_constraints: list[str]
    absorbed: list[Item] = field(default_factory=list)  # slack days consumed


MERGE_ORDER = [
    ("direct_instruction", "guided_practice"),
    ("guided_practice", "review"),
    ("guided_practice", "independent_practice"),
    ("independent_practice", "review"),
    ("review", "assessment"),
    ("reading", "reading"),
    ("direct_instruction", "independent_practice"),
    ("writing_workshop", "writing_workshop"),
    ("direct_instruction", "direct_instruction"),
]


def _merge_rank(a: Item, b: Item) -> int | None:
    if a.is_assessment and b.is_assessment:
        return None
    if a.is_assessment:  # never put something *after* an assessment on its day
        return None
    if not (a.can_move and b.can_move):
        return None
    if a.is_flex or b.is_flex or a.is_sub_day or b.is_sub_day:
        return None
    pair = (a.lesson_type, b.lesson_type)
    for i, p in enumerate(MERGE_ORDER):
        if pair == p:
            return i
    return len(MERGE_ORDER) + 1  # allowed but least preferred


def merge_items(a: Item, b: Item, minutes: int, study_minutes: int = 0) -> Item | None:
    """Fold ``a`` into ``b``'s day if both cores fit in one class period.

    Each lesson keeps its required components (its minimum viable minutes) and
    drops its optional ones; the pre-quiz study period is added when ``b`` is an
    assessment.
    """
    need = a.min_minutes + b.min_minutes + (study_minutes if b.is_assessment else 0)
    if need > minutes:
        return None
    if _merge_rank(a, b) is None:
        return None
    shortened = "shortened " if b.min_minutes < b.duration and not b.is_assessment else ""
    title = f"{a.title} + {shortened}{b.title}"
    compressed = dict(b.payload.get("compressed", {}))
    for it in (a, b):
        if it.optional_components or it.required_components:
            compressed[it.key] = {"keep": list(it.required_components), "skip": list(it.optional_components)}
    return replace(
        b,
        title=title,
        merged=[*a.merged, a.key, *b.merged],
        prereqs=(a.prereqs | b.prereqs) - set(a.slugs) - set(b.slugs),
        min_minutes=need,
        duration=minutes,
        sequence=a.sequence,
        payload={**b.payload, "compressed": compressed} if compressed else dict(b.payload),
    )


# ------------------------------------------------------------- ordering checks
def prerequisite_violations(assigned: Iterable[tuple[date, Item]], fixed_dates: dict[str, date] | None = None) -> list[tuple[date, Item, str, date]]:
    """Return (date, item, prerequisite slug, prerequisite date) for every lesson taught before a prerequisite.

    ``fixed_dates`` maps lesson slugs to the dates they are taught outside the
    window (earlier completed days, days before the window); a prerequisite seen
    only there is satisfied when that date is not after the dependent's date.
    """
    first_seen: dict[str, date] = dict(fixed_dates or {})
    rows = sorted(((d, it) for d, it in assigned if it is not None), key=lambda x: x[0])
    for d, it in rows:
        for s in it.slugs:
            if s not in first_seen or d < first_seen[s]:
                first_seen[s] = d
    out = []
    for d, it in rows:
        taught_here = set(it.slugs)
        for p in it.prereqs:
            if p in taught_here:
                continue
            pd = first_seen.get(p)
            if pd is not None and pd > d:
                out.append((d, it, p, pd))
    return out


def _depended_on(pool: list[Item]) -> set[str]:
    return {p for it in pool for p in it.prereqs}


# ------------------------------------------------------------------- repair
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

    Hard constraints: pinned dates (substitute days, hard dates), the window
    boundary (last slot) and prerequisite order. Repair actions in order of
    preference: absorb the nearest slack day, drop optional lessons, merge
    adjacent lessons on their required components, drop recommended lessons,
    then carry the latest items past the boundary. Nothing is deleted: carried
    items come back in ``deferred`` and dropped ones in ``dropped``.
    ``allow_defer=False`` marks carried items as unresolved (no later segment).
    """
    soft_prefs = soft_prefs or {}
    items = [replace(i, merged=list(i.merged), prereqs=set(i.prereqs), payload=dict(i.payload)) for i in items]
    hard: list[str] = [f"Window ends at {boundary_label} ({slots[-1].date})" if slots else "No slots available"]
    unresolved: list[str] = []
    soft_notes: list[str] = []
    dropped: list[Item] = []
    deferred: list[Item] = []
    absorbed: list[Item] = []
    merged_pairs: list[tuple[Item, Item]] = []
    changes_reasons: dict[str, str] = {}

    slot_dates = [s.date for s in slots]
    slot_set = set(slot_dates)
    for i in items:
        if i.hard_date and i.hard_date in slot_set and not i.pinned:
            i.pinned = i.hard_date
            hard.append(f"{i.title} is fixed on {i.hard_date}")
    for p in items:
        if p.pinned in slot_set and p.is_sub_day:
            hard.append(f"{p.pinned}: substitute day ({p.title})")

    def free_items() -> list[Item]:
        return [i for i in items if i.pinned not in slot_set]

    def free_slots() -> list[Slot]:
        taken = {i.pinned for i in items if i.pinned in slot_set}
        return [s for s in slots if s.date not in taken]

    # Pinned (non-substitute) lessons follow a displaced prerequisite instead of jumping ahead of it.
    for _ in range(10):
        changed = False
        for p in [i for i in items if i.pinned in slot_set and not i.is_sub_day and not i.hard_date]:
            later_prereqs = [q for q in free_items() if set(q.slugs) & p.prereqs]
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
        return len(pool) - capacity

    # 1. Absorb slack: the nearest pure flex day goes first, then placeholders, so the cascade stays short.
    while over() > 0 and any(i.is_flex for i in pool):
        best_idx = min((i for i in range(len(pool)) if pool[i].is_flex), key=lambda i: (pool[i].slack_rank, i))
        absorbed.append(pool.pop(best_idx))
    # 2. Drop optional lessons (from the end), never one a remaining lesson depends on.
    while over() > 0:
        needed = _depended_on(pool)
        idx = next((k for k in range(len(pool) - 1, -1, -1) if pool[k].priority == "optional" and not set(pool[k].slugs) & needed), None)
        if idx is None:
            break
        dropped.append(pool.pop(idx))
    # 3. Merge / compress adjacent compatible lessons on their required components.
    while over() > 0:
        best = None
        for idx in range(len(pool) - 1):
            a, b = pool[idx], pool[idx + 1]
            if a.unit != b.unit:
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
    # 4. Drop recommended lessons, never one a remaining lesson depends on.
    while over() > 0:
        needed = _depended_on(pool)
        idx = next((k for k in range(len(pool) - 1, -1, -1) if pool[k].priority == "recommended" and not set(pool[k].slugs) & needed), None)
        if idx is None:
            break
        dropped.append(pool.pop(idx))
    # 5. Carry the latest items past the boundary (sequence order is preserved, so prerequisites stay first).
    while over() > 0:
        deferred.insert(0, pool.pop())
    if deferred and not allow_defer:
        unresolved.append(f"{len(deferred)} item(s) cannot fit before the {boundary_label}: " + ", ".join(i.title for i in deferred))

    # 6. Assign in date order.
    fs = free_slots()
    assigned: dict[date, Item] = {i.pinned: i for i in items if i.pinned in slot_set}
    for slot, item in zip(fs, pool):
        assigned[slot.date] = item

    # 7. Soft constraints: no assessments on Monday when a harmless swap exists; review before an assessment.
    dates = slot_dates
    if _truthy(soft_prefs.get("no_monday_assessments", True)):
        for i, d in enumerate(dates):
            it = assigned.get(d)
            if not (it and it.is_assessment and d.isoweekday() == 1 and not it.pinned):
                continue
            nxt = dates[i + 1] if i + 1 < len(dates) else None
            nx = assigned.get(nxt) if nxt else None
            prev = assigned.get(dates[i - 1]) if i > 0 else None
            prev_supports = prev is not None and (bool(set(prev.slugs) & it.prereqs) or prev.kind == "review")
            if nx and not nx.pinned and not nx.is_assessment and not nx.is_sub_day and not (set(nx.slugs) & it.prereqs) and not (set(it.slugs) & nx.prereqs) and nx.unit != it.unit and not prev_supports:
                assigned[d], assigned[nxt] = nx, it
                soft_notes.append(f"Moved {it.title} off Monday {d} to {nxt} (swapped with {nx.title}).")
            else:
                soft_notes.append(f"{it.title} lands on a Monday ({d}); no swap was available without separating it from its review.")
    for slot in slots:
        it = assigned.get(slot.date)
        if it is not None and it.min_minutes and slot.minutes < it.min_minutes:
            what = "early-release day" if slot.early_release else "shortened day"
            soft_notes.append(f"{it.title} needs about {it.min_minutes} minutes but {slot.date} is an {what} ({slot.minutes} min); the plan for that day covers the core only.")
    for i, d in enumerate(dates):
        it = assigned.get(d)
        if it and it.is_assessment and i > 0:
            prev = assigned.get(dates[i - 1])
            if prev and prev.kind != "review" and "review" not in prev.title.lower() and not prev.is_sub_day:
                soft_notes.append(f"No review day immediately before {it.title} ({d}).")

    # 8. Diff (kept for engine-level callers and tests; the service builds full day-state diffs).
    changes: list[Change] = []
    for slot in slots:
        it = assigned.get(slot.date)
        before_key, before_title = slot.existing_key, slot.existing_title
        if it is None:
            changes.append(Change(slot.date, "shift", before_key, before_title, None, "Flex / work day", "flex", reason="slack after reconciliation"))
            continue
        if it.is_sub_day:
            action = "replace" if it.kind == "filler" and before_key != it.key else ("keep" if before_key == it.key else "shift")
            changes.append(Change(slot.date, action, before_key, before_title, it.key, it.title, it.kind, list(it.merged), True, it.payload.get("rationale", ""), payload=dict(it.payload)))
        elif it.merged and it.key != before_key:
            changes.append(Change(slot.date, "merge", before_key, before_title, it.key, it.title, it.kind, list(it.merged), False, "combined to recover a lost day"))
        elif it.merged and it.key == before_key:
            changes.append(Change(slot.date, "compress", before_key, before_title, it.key, it.title, it.kind, list(it.merged), False, "combined to recover a lost day"))
        elif it.key == before_key:
            changes.append(Change(slot.date, "preserve" if (it.hard_date or it.pinned) else "keep", before_key, before_title, it.key, it.title, it.kind))
        else:
            changes.append(Change(slot.date, "shift", before_key, before_title, it.key, it.title, it.kind, reason=changes_reasons.get(it.key, "moved to keep the sequence intact")))
    end = slots[-1].date if slots else date.today()
    for d in dropped:
        changes.append(Change(end, "drop", d.key, d.title, None, None, reason=f"{d.priority} content removed to fit before the {boundary_label}"))
    for d in deferred:
        changes.append(Change(end, "defer", d.key, d.title, None, None, reason=f"does not fit before the {boundary_label}; carried forward"))
    assignments = [(s, assigned.get(s.date)) for s in slots]
    return RepairResult(assignments, changes, deferred, dropped, merged_pairs, unresolved, soft_notes, hard, absorbed)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("no", "false", "0", "off", "n", "")
