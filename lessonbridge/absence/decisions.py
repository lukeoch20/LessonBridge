"""Lesson decision engine: KEEP / MODIFY / REPLACE / POSTPONE / REORDER per affected course-day."""
from __future__ import annotations

from dataclasses import dataclass, field
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
    item: Optional[Item]  # what was on the calendar
    decision: str
    rationale: str
    replacement: Optional[ReplacementOption] = None
    reorder_with: Optional[Item] = None
    inputs: dict = field(default_factory=dict)


def choose_replacement(options: list[ReplacementOption], *, used: set[str], prefer_tags: set[str] | None = None) -> Optional[ReplacementOption]:
    """Curriculum-preserving first, then skill maintenance, emergency filler last; avoid repeats."""
    prefer_tags = prefer_tags or set()
    order = {"curriculum_preserving": 0, "skill_maintenance": 1, "emergency_filler": 2}

    def score(o: ReplacementOption) -> tuple:
        return (o.slug in used, order.get(o.category, 3), -len(prefer_tags & set(o.tags)), o.slug)

    if not options:
        return None
    return sorted(options, key=score)[0]


def decide_days(
    sub_days: list[date],
    items_by_date: dict[date, Optional[Item]],
    sequence: list[Item],
    *,
    substitute_type: str,
    absence_days: int,
    replacements: list[ReplacementOption],
    prefs: dict | None = None,
) -> list[DayDecision]:
    """Decide what happens on each substitute day for one section.

    ``sequence`` is the section's remaining lesson sequence (including the
    affected lessons) in order; used to find REORDER candidates.
    """
    prefs = prefs or {}
    allow_sub_assessments = str(prefs.get("subs_may_administer_assessments", "yes")).lower() in ("yes", "true", "1")
    reorder_window = int(prefs.get("reorder_lookahead", 5))
    used: set[str] = set()
    decisions: list[DayDecision] = []
    displaced: set[str] = set()
    pulled_forward: set[str] = set()
    completed_or_kept: set[str] = set()
    long_term = substitute_type == "long_term_sub"
    idx = {it.key: i for i, it in enumerate(sequence)}

    for d in sub_days:
        item = items_by_date.get(d)
        inputs = {"substitute_type": substitute_type, "absence_days": absence_days, "lesson_type": item.lesson_type if item else None, "delivery": sorted(item.delivery) if item else None, "priority": item.priority if item else None}
        if (item is None or item.is_flex) and long_term:
            if item is not None:
                decisions.append(DayDecision(d, item, "KEEP", "Flex day stays available to the long-term substitute for catch-up or extension.", inputs=inputs))
                completed_or_kept.add(item.key)
                continue
        if item is None or item.is_flex:
            rep = choose_replacement(replacements, used=used, prefer_tags={item.unit} if item else set())
            if rep:
                used.add(rep.slug)
            decisions.append(DayDecision(d, item, "REPLACE", "No lesson was scheduled (flex day); the substitute gives curriculum-preserving independent work.", rep, inputs=inputs))
            continue

        if long_term:
            if "long_term_sub" in item.delivery or item.is_assessment:
                decisions.append(DayDecision(d, item, "KEEP", "A long-term substitute can teach new material and administer assessments; lesson stays on schedule.", inputs=inputs))
                completed_or_kept.add(item.key)
            else:
                decisions.append(DayDecision(d, item, "MODIFY", "Lesson is marked regular-teacher-only; it stays on schedule with a long-term-substitute adaptation and is flagged for the teacher.", inputs=inputs))
                completed_or_kept.add(item.key)
            continue

        # Short absence with a day-to-day substitute.
        if item.is_assessment:
            prereqs_displaced = bool(item.prereqs & displaced)
            if allow_sub_assessments and absence_days <= 2 and not prereqs_displaced:
                decisions.append(DayDecision(d, item, "KEEP", "Substitute may administer the assessment; its review was not disrupted.", inputs=inputs))
                completed_or_kept.add(item.key)
            else:
                why = "its review lesson was displaced" if prereqs_displaced else ("the absence is longer than two days" if absence_days > 2 else "teacher preference: substitutes do not give assessments")
                rep = choose_replacement([r for r in replacements if r.category != "emergency_filler"] or replacements, used=used, prefer_tags={item.unit})
                if rep:
                    used.add(rep.slug)
                decisions.append(DayDecision(d, item, "POSTPONE", f"Assessment postponed because {why}; students get curriculum-preserving review instead.", rep, inputs=inputs))
                displaced.add(item.key)
            continue

        if item.delivery & {"any_sub", "independent"}:
            decisions.append(DayDecision(d, item, "KEEP", "Lesson is substitute-deliverable as written.", inputs=inputs))
            completed_or_kept.add(item.key)
            continue

        # Teacher-dependent lesson: try to pull a later substitute-deliverable lesson forward.
        candidate = None
        start = idx.get(item.key, 0)
        for later in sequence[start + 1 : start + 1 + reorder_window]:
            if later.key in pulled_forward or later.is_assessment or later.is_flex or later.unit != item.unit:
                continue
            if not (later.delivery & {"any_sub", "independent"}):
                continue
            unmet = {p for p in later.prereqs if p in idx and idx[p] >= start} - completed_or_kept
            if unmet:
                continue
            candidate = later
            break
        if candidate is not None:
            pulled_forward.add(candidate.key)
            displaced.add(item.key)
            decisions.append(DayDecision(d, item, "REORDER", f"'{candidate.title}' is substitute-deliverable and has no unmet prerequisites, so it moves up; '{item.title}' shifts later.", reorder_with=candidate, inputs=inputs))
            completed_or_kept.add(candidate.key)
            continue
        rep = choose_replacement(replacements, used=used, prefer_tags={item.unit})
        if rep:
            used.add(rep.slug)
        displaced.add(item.key)
        decisions.append(DayDecision(d, item, "REPLACE", "Lesson needs the regular teacher and no later lesson can be pulled forward; replaced with curriculum-preserving independent work.", rep, inputs=inputs))
    return decisions
