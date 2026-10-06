"""Lesson decision engine: KEEP / MODIFY / REPLACE / POSTPONE / REORDER per affected course-day."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Optional

from .engine import Item


@dataclass
class ReplacementOption:
    slug: str
    title: str
    description: str
    duration: int
    category: str  # curriculum_preserving | skill_maintenance | emergency_filler
    materials: list[str]
    student_output: str
    tags: list[str] = field(default_factory=list)
    activity_id: Optional[int] = None


@dataclass
class DayDecision:
    date: date
    item: Optional[Item]  # copy of what was on the calendar that day
    decision: str
    rationale: str
    replacement: Optional[ReplacementOption] = None
    reorder_with: Optional[Item] = None  # copy of the lesson pulled forward
    consumes_slack: bool = False  # a REPLACE on a flex/placeholder day uses up that slack day
    inputs: dict = field(default_factory=dict)


# ------------------------------------------------------------- preferences
def pref_bool(prefs: dict, key: str, default: bool) -> bool:
    """Parse a preference as a boolean (LB-48). Unknown values fall back to the default."""
    raw = prefs.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    v = str(raw).strip().lower()
    if v in ("yes", "y", "true", "t", "1", "on"):
        return True
    if v in ("no", "n", "false", "f", "0", "off"):
        return False
    return default


def pref_int(prefs: dict, key: str, default: int, *, lo: int = 0, hi: int = 1000) -> int:
    raw = prefs.get(key)
    try:
        v = int(str(raw).strip()) if raw is not None else default
    except ValueError:
        return default
    return max(lo, min(hi, v))


# ------------------------------------------------------------ replacements
_WORD = re.compile(r"[a-z]{4,}")


def keywords(*texts: str) -> set[str]:
    out: set[str] = set()
    for t in texts:
        out |= set(_WORD.findall((t or "").lower()))
    return out


def choose_replacement(options: list[ReplacementOption], *, used: set[str], context: set[str] | None = None) -> Optional[ReplacementOption]:
    """Curriculum-preserving first, best match to the current unit next, emergency filler last; avoid repeats (LB-50)."""
    context = context or set()
    order = {"curriculum_preserving": 0, "skill_maintenance": 1, "emergency_filler": 2}

    def score(o: ReplacementOption) -> tuple:
        overlap = len(context & (set(o.tags) | keywords(o.title, o.description)))
        return (o.slug in used, order.get(o.category, 3), -overlap, o.slug)

    if not options:
        return None
    return sorted(options, key=score)[0]


# --------------------------------------------------------------- decisions
def decide_days(
    sub_days: list[date],
    items_by_date: dict[date, Optional[Item]],
    sequence: list[Item],
    *,
    substitute_type: str,
    absence_days: int,
    replacements: list[ReplacementOption],
    prefs: dict | None = None,
    unit_context: dict[str, str] | None = None,
) -> list[DayDecision]:
    """Decide what happens on each substitute day for one section.

    ``sequence`` is the section's remaining lesson sequence (including the
    affected lessons) in order; it is used to find REORDER candidates.
    ``unit_context`` maps unit slugs to text (title + summary) used to pick
    replacement activities that fit the current unit.

    A lesson is KEPT for a day-to-day substitute only when it is
    substitute-deliverable and none of its prerequisites was displaced earlier
    in the same absence (LB-06). REORDER never pulls a lesson that itself sits on
    a later day of the absence and never shares an item object with another
    decision (LB-07).
    """
    prefs = prefs or {}
    unit_context = unit_context or {}
    allow_sub_assessments = pref_bool(prefs, "subs_may_administer_assessments", True)
    reorder_window = pref_int(prefs, "reorder_lookahead", 5, lo=0, hi=20)
    used: set[str] = set()
    decisions: list[DayDecision] = []
    displaced: set[str] = set()  # lesson slugs that will not be taught on their planned day
    pulled_forward: set[str] = set()
    taught: set[str] = set()  # lesson slugs taught (kept or pulled forward) so far in this absence
    long_term = substitute_type == "long_term_sub"
    sub_day_set = set(sub_days)
    date_of = {it.key: d for d, it in items_by_date.items() if it is not None}
    idx = {it.key: i for i, it in enumerate(sequence)}
    pos_of_slug: dict[str, int] = {}
    for i, it in enumerate(sequence):
        for s in it.slugs:
            pos_of_slug.setdefault(s, i)

    def context_for(it: Optional[Item]) -> set[str]:
        if it is None:
            return set()
        return keywords(unit_context.get(it.unit, ""), it.title, it.unit.replace("-", " "))

    def pick(it: Optional[Item], *, no_filler: bool = False) -> Optional[ReplacementOption]:
        pool = [r for r in replacements if not (no_filler and r.category == "emergency_filler")] or replacements
        rep = choose_replacement(pool, used=used, context=context_for(it))
        if rep:
            used.add(rep.slug)
        return rep

    for d in sorted(sub_days):
        orig = items_by_date.get(d)
        item = replace(orig, prereqs=set(orig.prereqs), merged=list(orig.merged), payload=dict(orig.payload)) if orig else None
        inputs = {"substitute_type": substitute_type, "absence_days": absence_days, "lesson_type": item.lesson_type if item else None, "delivery": sorted(item.delivery) if item else None, "priority": item.priority if item else None}
        if item is not None and item.key in pulled_forward:
            # This lesson was already pulled forward to an earlier absence day; today is a gap.
            rep = pick(item)
            decisions.append(DayDecision(d, item, "REPLACE", f"'{item.title}' was already moved to an earlier absence day; students get curriculum-preserving independent work.", rep, consumes_slack=True, inputs=inputs))
            continue
        if item is None or item.is_flex:
            if long_term and item is not None:
                decisions.append(DayDecision(d, item, "KEEP", "Flex day stays available to the long-term substitute for catch-up or extension.", inputs=inputs))
                continue
            rep = pick(item)
            decisions.append(DayDecision(d, item, "REPLACE", "No lesson was scheduled (flex day); the substitute gives curriculum-preserving independent work.", rep, consumes_slack=item is not None, inputs=inputs))
            continue

        prereqs_displaced = bool(item.prereqs & displaced)

        if long_term:
            if prereqs_displaced:
                rep = pick(item)
                displaced.update(item.slugs)
                decisions.append(DayDecision(d, item, "REPLACE", "A prerequisite of this lesson was displaced earlier in the absence; it moves later with its prerequisite.", rep, inputs=inputs))
            elif "long_term_sub" in item.delivery or item.is_assessment:
                decisions.append(DayDecision(d, item, "KEEP", "A long-term substitute can teach new material and administer assessments; lesson stays on schedule.", inputs=inputs))
                taught.update(item.slugs)
            else:
                decisions.append(DayDecision(d, item, "MODIFY", "Lesson is marked regular-teacher-only; it stays on schedule with a long-term-substitute adaptation and is flagged for the teacher.", inputs=inputs))
                taught.update(item.slugs)
            continue

        # Day-to-day substitute.
        if item.is_assessment:
            if allow_sub_assessments and absence_days <= 2 and not prereqs_displaced:
                decisions.append(DayDecision(d, item, "KEEP", "Substitute may administer the assessment; its review was not disrupted.", inputs=inputs))
                taught.update(item.slugs)
            else:
                why = "a lesson it depends on was displaced" if prereqs_displaced else ("the absence is longer than two days" if absence_days > 2 else "teacher preference: substitutes do not give assessments")
                rep = pick(item, no_filler=True)
                displaced.update(item.slugs)
                decisions.append(DayDecision(d, item, "POSTPONE", f"Assessment postponed because {why}; students get curriculum-preserving review instead.", rep, inputs=inputs))
            continue

        if item.delivery & {"any_sub", "independent"} and not prereqs_displaced:
            decisions.append(DayDecision(d, item, "KEEP", "Lesson is substitute-deliverable as written and nothing it depends on was displaced.", inputs=inputs))
            taught.update(item.slugs)
            continue

        # Teacher-dependent lesson (or a lesson whose prerequisite moved): try to pull a later substitute-deliverable lesson forward.
        displaced.update(item.slugs)
        candidate = None
        start = idx.get(item.key, 0)
        for later in sequence[start + 1 : start + 1 + reorder_window]:
            if later.key in pulled_forward or later.is_assessment or later.is_flex or later.is_sub_day or later.unit != item.unit:
                continue
            if date_of.get(later.key) in sub_day_set:
                continue  # it is already on a day of this absence
            if not (later.delivery & {"any_sub", "independent"}):
                continue
            unmet = {p for p in later.prereqs if p in displaced or (p in pos_of_slug and pos_of_slug[p] >= start and p not in taught)}
            if unmet:
                continue
            candidate = later
            break
        if candidate is not None:
            pulled = replace(candidate, prereqs=set(candidate.prereqs), merged=list(candidate.merged), payload=dict(candidate.payload))
            pulled_forward.add(candidate.key)
            taught.update(candidate.slugs)
            why = "needs the regular teacher" if not (item.delivery & {"any_sub", "independent"}) else "depends on a lesson displaced earlier in this absence"
            decisions.append(DayDecision(d, item, "REORDER", f"'{item.title}' {why}, so it shifts later; '{candidate.title}' is substitute-deliverable with its prerequisites met, so it moves up.", reorder_with=pulled, inputs=inputs))
            continue
        rep = pick(item)
        reason = "Lesson needs the regular teacher" if not (item.delivery & {"any_sub", "independent"}) else "A lesson this one depends on was displaced"
        decisions.append(DayDecision(d, item, "REPLACE", f"{reason} and no later lesson can be pulled forward; replaced with curriculum-preserving independent work.", rep, inputs=inputs))
    return decisions
