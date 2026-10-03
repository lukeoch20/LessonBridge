"""Teacher rules and preferences.

Rules are mandatory and participate in generation *and* validation. They are
scoped (lesson > unit > course > teacher > default) and the most specific
active rule for a category wins when two rules conflict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CurriculumUnit, Lesson, LessonType, RuleScope, TeacherCourse, TeacherPreference, TeacherRule

SCOPE_RANK = {RuleScope.lesson: 0, RuleScope.unit: 1, RuleScope.course: 2, RuleScope.teacher: 3, RuleScope.default: 4}

DEFAULT_RULES: list[dict] = [
    {"category": "collection", "text": "Collect all student work at the end of class and leave it in the labeled tray on the teacher's desk.", "structured": {"trigger": "always", "action": "collect_work"}},
    {"category": "attendance", "text": "Take attendance in the first five minutes using the seating chart.", "structured": {"trigger": "always", "action": "attendance"}},
    {"category": "devices", "text": "Chromebooks are only used when the plan calls for them and stay closed during instructions.", "structured": {"trigger": "always", "action": "device_policy"}},
    {"category": "early_finisher", "text": "Early finishers read independently or complete unfinished work; no free time on devices.", "structured": {"trigger": "always", "action": "early_finisher"}},
]

# Phrases that map free-text rules to machine-readable triggers.
_RULE_PATTERNS = [
    (re.compile(r"(\d+)\s*-?\s*minutes?\s+(?:to\s+)?(?:study|review)\s+before\s+(?:the\s+)?(?:quiz|test|assessment)", re.I), lambda m: {"trigger": "assessment", "action": "study_period", "minutes": int(m.group(1))}),
    (re.compile(r"(?:quiz|test|assessment).{0,40}?(\d+)\s*-?\s*minutes?\s+(?:to\s+)?(?:study|review)", re.I), lambda m: {"trigger": "assessment", "action": "study_period", "minutes": int(m.group(1))}),
    (re.compile(r"substitutes?\s+(?:may|can|should)\s+not\s+grade", re.I), lambda m: {"trigger": "always", "action": "no_sub_grading"}),
    (re.compile(r"(?:homework|hw)\s+is\s+(?:due|collected)", re.I), lambda m: {"trigger": "always", "action": "homework_collection"}),
    (re.compile(r"(?:bathroom|hall)\s*pass", re.I), lambda m: {"trigger": "always", "action": "hall_pass"}),
    (re.compile(r"early\s+finishers?", re.I), lambda m: {"trigger": "always", "action": "early_finisher"}),
    (re.compile(r"late\s+work", re.I), lambda m: {"trigger": "always", "action": "late_work"}),
    (re.compile(r"independent\s+reading", re.I), lambda m: {"trigger": "always", "action": "independent_reading"}),
]

_CATEGORY_HINTS = {
    "study_period": "assessment_routine", "no_sub_grading": "grading", "homework_collection": "homework",
    "hall_pass": "procedure", "early_finisher": "early_finisher", "late_work": "late_work", "independent_reading": "reading",
}


def structure_rule(text: str) -> tuple[str, dict]:
    """Infer category + structured form from free text. Returns (category, structured)."""
    for pattern, builder in _RULE_PATTERNS:
        m = pattern.search(text)
        if m:
            structured = builder(m)
            return _CATEGORY_HINTS.get(structured["action"], "procedure"), structured
    return "procedure", {"trigger": "always", "action": "note"}


def _norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def add_rule(session: Session, teacher_id: int, text: str, *, scope: RuleScope = RuleScope.teacher, scope_id: int | None = None, category: str | None = None, structured: dict | None = None, source: str = "teacher") -> TeacherRule:
    inferred_category, inferred_struct = structure_rule(text)
    final_struct = structured or inferred_struct
    existing = list(session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id, TeacherRule.active.is_(True))))
    for r in existing:
        if _norm_text(r.text) == _norm_text(text):
            return r  # never store the same rule twice
    # A teacher-provided rule supersedes a default rule covering the same routine.
    action = final_struct.get("action")
    if action and action != "note" and scope != RuleScope.default:
        for r in existing:
            if r.scope == RuleScope.default and (r.structured or {}).get("action") == action:
                r.active = False
    if category in (None, "procedure") and inferred_category != "procedure":
        category = inferred_category
    rule = TeacherRule(
        teacher_id=teacher_id, scope=scope, scope_id=scope_id, category=category or inferred_category,
        text=text.strip(), structured=final_struct, source=source,
    )
    session.add(rule)
    session.flush()
    return rule


def set_preference(session: Session, teacher_id: int, key: str, value: str, *, source: str = "onboarding", confidence: float = 1.0) -> TeacherPreference:
    pref = session.scalar(select(TeacherPreference).where(TeacherPreference.teacher_id == teacher_id, TeacherPreference.key == key))
    if pref is None:
        pref = TeacherPreference(teacher_id=teacher_id, key=key, value=value, source=source, confidence=confidence)
        session.add(pref)
    else:
        pref.value, pref.source, pref.confidence = value, source, confidence
    session.flush()
    return pref


def get_preferences(session: Session, teacher_id: int) -> dict[str, str]:
    return {p.key: p.value for p in session.scalars(select(TeacherPreference).where(TeacherPreference.teacher_id == teacher_id))}


@dataclass
class ResolvedRule:
    text: str
    category: str
    scope: str
    structured: dict


def resolve_rules(session: Session, teacher_id: int, *, section: TeacherCourse | None = None, lesson: Lesson | None = None, unit: CurriculumUnit | None = None, is_assessment: bool | None = None) -> list[ResolvedRule]:
    """Return the rules that apply to a course-day, most specific scope winning per category."""
    if lesson is not None and unit is None:
        unit = lesson.unit
    if is_assessment is None:
        is_assessment = bool(lesson and lesson.lesson_type == LessonType.assessment)
    rules = list(session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id, TeacherRule.active.is_(True))))
    applicable: list[TeacherRule] = []
    for r in rules:
        if r.scope == RuleScope.lesson and (lesson is None or r.scope_id != lesson.id):
            continue
        if r.scope == RuleScope.unit and (unit is None or r.scope_id != unit.id):
            continue
        if r.scope == RuleScope.course and (section is None or r.scope_id not in (section.id, section.course_id)):
            continue
        trigger = (r.structured or {}).get("trigger", "always")
        if trigger == "assessment" and not is_assessment:
            continue
        applicable.append(r)
    # Most specific per (category, action) wins.
    best: dict[tuple[str, str], TeacherRule] = {}
    for r in sorted(applicable, key=lambda r: SCOPE_RANK[r.scope]):
        action = (r.structured or {}).get("action", "note")
        key = ("", action) if action != "note" else (r.category, r.text)
        best.setdefault(key, r)
    out = [ResolvedRule(r.text, r.category, r.scope.value, dict(r.structured or {})) for r in best.values()]
    out.sort(key=lambda x: (x.category, x.text))
    return out


def rule_minutes(rules: Iterable[ResolvedRule], action: str) -> int:
    for r in rules:
        if r.structured.get("action") == action:
            return int(r.structured.get("minutes", 0))
    return 0


def seed_default_rules(session: Session, teacher_id: int) -> int:
    existing = {r.text for r in session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id))}
    n = 0
    for d in DEFAULT_RULES:
        if d["text"] in existing:
            continue
        session.add(TeacherRule(teacher_id=teacher_id, scope=RuleScope.default, category=d["category"], text=d["text"], structured=d["structured"], source="default"))
        n += 1
    return n
