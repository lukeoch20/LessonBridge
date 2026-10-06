"""Teacher rules and preferences.

Rules are mandatory and participate in generation *and* validation. They are
scoped (lesson > unit > course > teacher > default). Precedence is resolved per
rule, not per inferred action (LB-15): for additive routines (hall passes, late
work, collection...) every rule at the most specific scope applies; for
single-valued settings (the pre-quiz study period) the newest rule at the most
specific scope wins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CurriculumUnit, Lesson, LessonType, RuleScope, Subject, TeacherCourse, TeacherPreference, TeacherRule

SCOPE_RANK = {RuleScope.lesson: 0, RuleScope.unit: 1, RuleScope.course: 2, RuleScope.teacher: 3, RuleScope.default: 4}
SINGLE_VALUED = {"study_period"}

DEFAULT_RULES: list[dict] = [
    {"category": "collection", "text": "Collect all student work at the end of class and leave it in the labeled tray on the teacher's desk.", "structured": {"trigger": "always", "action": "collect_work"}},
    {"category": "attendance", "text": "Take attendance in the first five minutes using the seating chart.", "structured": {"trigger": "always", "action": "attendance"}},
    {"category": "devices", "text": "Chromebooks are only used when the plan calls for them and stay closed during instructions.", "structured": {"trigger": "always", "action": "device_policy"}},
    {"category": "early_finisher", "text": "Early finishers read independently or complete unfinished work; no free time on devices.", "structured": {"trigger": "always", "action": "early_finisher"}},
]

NUMBER_WORDS = {
    "five": 5, "ten": 10, "twelve": 12, "fifteen": 15, "twenty": 20, "twenty-five": 25, "twenty five": 25, "thirty": 30,
    "forty": 40, "forty-five": 45, "half an hour": 30, "a half hour": 30, "quarter of an hour": 15,
}
_NUM = r"(\d{1,3}|twenty[- ]five|half an hour|a half hour|quarter of an hour|" + "|".join(sorted((k for k in NUMBER_WORDS if " " not in k and "-" not in k), key=len, reverse=True)) + r")"
_MINUTES = re.compile(_NUM + r"(?:\s*-?\s*(?:min(?:ute)?s?)\b)?", re.I)
_MINUTES_STRICT = re.compile(_NUM + r"\s*-?\s*min(?:ute)?s?\b|half an hour|a half hour|quarter of an hour", re.I)
_STUDY = re.compile(r"\b(stud(?:y|ying)|review(?:ing)?|prepare|prep)\b", re.I)
_ASSESS = re.compile(r"\b(quiz(?:zes)?|tests?|assessments?|exams?)\b", re.I)
_BEFORE = re.compile(r"\b(before|prior to|ahead of|pre-)|\bpre-?(quiz|test)\b", re.I)

_RULE_PATTERNS = [
    (re.compile(r"substitutes?\s+(?:may|can|should|must)\s+not\s+grade|(?:do not|don't|never)\s+grade", re.I), {"trigger": "always", "action": "no_sub_grading"}),
    (re.compile(r"(?:homework|hw)\b.*\b(?:due|collected|tray|turn(?:ed)? in)", re.I), {"trigger": "always", "action": "homework_collection"}),
    (re.compile(r"(?:bathroom|hall)\s*pass", re.I), {"trigger": "always", "action": "hall_pass"}),
    (re.compile(r"early\s+finishers?", re.I), {"trigger": "always", "action": "early_finisher"}),
    (re.compile(r"late\s+work", re.I), {"trigger": "always", "action": "late_work"}),
    (re.compile(r"independent\s+reading", re.I), {"trigger": "always", "action": "independent_reading"}),
    (re.compile(r"\battendance\b", re.I), {"trigger": "always", "action": "attendance"}),
    (re.compile(r"\b(cell\s*)?phones?\b", re.I), {"trigger": "always", "action": "phone_policy"}),
    (re.compile(r"\bchromebooks?|\bdevices?\b|\blaptops?\b", re.I), {"trigger": "always", "action": "device_policy"}),
    (re.compile(r"\bcollect\b", re.I), {"trigger": "always", "action": "collect_work"}),
    (re.compile(r"\bdismiss", re.I), {"trigger": "always", "action": "dismissal"}),
]

_CATEGORY_HINTS = {
    "study_period": "assessment_routine", "no_sub_grading": "grading", "homework_collection": "homework", "hall_pass": "procedure",
    "early_finisher": "early_finisher", "late_work": "late_work", "independent_reading": "reading", "attendance": "attendance",
    "phone_policy": "devices", "device_policy": "devices", "collect_work": "collection", "dismissal": "procedure",
}


def _minutes_value(token: str) -> Optional[int]:
    t = token.strip().lower()
    if t.isdigit():
        return int(t)
    return NUMBER_WORDS.get(t.replace("  ", " "))


def structure_rule(text: str) -> tuple[str, dict]:
    """Infer category + structured form from free text. Returns (category, structured).

    The pre-quiz study period is recognised in its common wordings (LB-31):
    "15-minute review before every test", "Before each quiz, give students
    fifteen minutes to study", "10 minutes of review prior to any assessment".
    A rule that mentions minutes and an assessment but does not parse is marked
    as needing confirmation instead of silently becoming an everyday note.
    """
    mins = _MINUTES_STRICT.search(text)
    if mins and _ASSESS.search(text) and _STUDY.search(text) and _BEFORE.search(text):
        token = mins.group(1) if mins.group(1) else mins.group(0)
        value = _minutes_value(token)
        if value:
            return "assessment_routine", {"trigger": "assessment", "action": "study_period", "minutes": value}
    for pattern, structured in _RULE_PATTERNS:
        if pattern.search(text):
            return _CATEGORY_HINTS.get(structured["action"], "procedure"), dict(structured)
    if mins and _ASSESS.search(text):
        return "assessment_routine", {"trigger": "assessment", "action": "note", "needs_confirmation": True}
    if _ASSESS.search(text) and re.search(r"\b(on|during|for)\s+(quiz|test|assessment)", text, re.I):
        return "assessment_routine", {"trigger": "assessment", "action": "note"}
    return "procedure", {"trigger": "always", "action": "note"}


def describe_rule(structured: dict) -> str:
    """Plain-language interpretation shown to the teacher for confirmation (LB-31)."""
    action = structured.get("action", "note")
    when = "on assessment days" if structured.get("trigger") == "assessment" else "every class"
    if action == "study_period":
        return f"{structured.get('minutes')}-minute study period before the assessment ({when})"
    if structured.get("needs_confirmation"):
        return f"applies {when}; mentions minutes but the timing was not understood, please reword"
    labels = {"no_sub_grading": "substitutes do not grade", "homework_collection": "homework collection", "hall_pass": "hall pass procedure",
              "early_finisher": "early-finisher activity", "late_work": "late work policy", "independent_reading": "independent reading",
              "attendance": "attendance procedure", "phone_policy": "phone policy", "device_policy": "device policy", "collect_work": "collecting work",
              "dismissal": "dismissal procedure", "note": "general instruction"}
    return f"{labels.get(action, action)} ({when})"


def _norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


class RuleTargetError(ValueError):
    pass


def resolve_target(session: Session, teacher_id: int, scope: RuleScope, target: Optional[str]) -> Optional[int]:
    """Turn a lesson slug, unit slug or subject into the id a scoped rule needs (LB-32)."""
    if scope in (RuleScope.teacher, RuleScope.default):
        return None
    if not target:
        raise RuleTargetError(f"A {scope.value}-scoped rule needs a target (a {'lesson slug' if scope == RuleScope.lesson else 'unit slug' if scope == RuleScope.unit else 'subject'}).")
    sections = list(session.scalars(select(TeacherCourse).where(TeacherCourse.teacher_id == teacher_id)))
    if scope == RuleScope.course:
        try:
            subj = Subject(target.strip().lower())
        except ValueError as exc:
            raise RuleTargetError(f"Unknown subject {target!r}.") from exc
        ids = {s.course_id for s in sections if s.course.subject == subj}
        if not ids:
            raise RuleTargetError(f"You have no {target} sections.")
        return ids.pop()
    for sec in sections:
        for u in sec.active_units:
            if scope == RuleScope.unit and u.slug == target:
                return u.id
            if scope == RuleScope.lesson:
                for l in u.lessons:
                    if l.slug == target:
                        return l.id
    raise RuleTargetError(f"No {scope.value} named {target!r} in your curriculum.")


def add_rule(session: Session, teacher_id: int, text: str, *, scope: RuleScope = RuleScope.teacher, scope_id: int | None = None, category: str | None = None, structured: dict | None = None, source: str = "teacher") -> TeacherRule:
    text = text.strip()
    if not text:
        raise ValueError("A rule needs text.")
    if scope in (RuleScope.lesson, RuleScope.unit, RuleScope.course) and scope_id is None:
        raise RuleTargetError(f"A {scope.value}-scoped rule needs a target.")
    inferred_category, inferred_struct = structure_rule(text)
    final_struct = structured or inferred_struct
    existing = list(session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id, TeacherRule.active.is_(True))))
    for r in existing:
        if _norm_text(r.text) == _norm_text(text) and r.scope == scope and r.scope_id == scope_id:
            return r  # never store the same rule twice
    action = final_struct.get("action")
    if action and action != "note" and scope != RuleScope.default:
        for r in existing:
            same_action = (r.structured or {}).get("action") == action
            if r.scope == RuleScope.default and same_action:
                r.active = False  # a teacher's rule supersedes the default for the same routine
            elif action in SINGLE_VALUED and same_action and r.scope == scope and r.scope_id == scope_id:
                r.active = False  # a correction replaces the earlier setting (e.g. 15 -> 20 minutes)
    if category in (None, "procedure") and inferred_category != "procedure":
        category = inferred_category
    rule = TeacherRule(teacher_id=teacher_id, scope=scope, scope_id=scope_id, category=category or inferred_category, text=text, structured=final_struct, source=source)
    session.add(rule)
    session.flush()
    return rule


def set_preference(session: Session, teacher_id: int, key: str, value, *, source: str = "onboarding", confidence: float = 1.0) -> TeacherPreference:
    if isinstance(value, bool):
        value = "yes" if value else "no"
    value = str(value)
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

    @property
    def applies(self) -> str:
        return "on assessment days" if self.structured.get("trigger") == "assessment" else "every class"


def resolve_rules(session: Session, teacher_id: int, *, section: TeacherCourse | None = None, lesson: Lesson | None = None, unit: CurriculumUnit | None = None,
                  is_assessment: bool | None = None, include_assessment_rules: bool = False) -> list[ResolvedRule]:
    """Rules that apply to a course-day.

    ``include_assessment_rules`` returns assessment-day rules too (for handoffs
    that cover many days); otherwise they apply only when ``is_assessment``.
    """
    if lesson is not None and unit is None:
        unit = lesson.unit
    if is_assessment is None:
        is_assessment = bool(lesson and lesson.lesson_type == LessonType.assessment)
    rules = list(session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id, TeacherRule.active.is_(True)).order_by(TeacherRule.created_at.desc(), TeacherRule.id.desc())))
    applicable: list[TeacherRule] = []
    for r in rules:
        if r.scope == RuleScope.lesson and (lesson is None or r.scope_id != lesson.id):
            continue
        if r.scope == RuleScope.unit and (unit is None or r.scope_id != unit.id):
            continue
        if r.scope == RuleScope.course and (section is None or r.scope_id != section.course_id):
            continue
        trigger = (r.structured or {}).get("trigger", "always")
        if trigger == "assessment" and not (is_assessment or include_assessment_rules):
            continue
        applicable.append(r)
    by_action: dict[str, list[TeacherRule]] = {}
    for r in applicable:
        by_action.setdefault((r.structured or {}).get("action", "note"), []).append(r)
    chosen: list[TeacherRule] = []
    for action, rs in by_action.items():
        rs.sort(key=lambda r: (SCOPE_RANK[r.scope], -(r.created_at.timestamp() if r.created_at else 0), -r.id))
        if action == "note":
            chosen += rs  # free-text instructions are all kept
        elif action in SINGLE_VALUED:
            chosen.append(rs[0])  # most specific scope, then newest
        else:
            best = SCOPE_RANK[rs[0].scope]
            chosen += [r for r in rs if SCOPE_RANK[r.scope] == best]  # every rule at the most specific scope
    seen: set[str] = set()
    out: list[ResolvedRule] = []
    for r in chosen:
        key = _norm_text(r.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(ResolvedRule(r.text, r.category, r.scope.value, dict(r.structured or {})))
    out.sort(key=lambda x: (x.category, x.text))
    return out


def rule_minutes(rules: Iterable[ResolvedRule], action: str) -> int:
    for r in rules:
        if r.structured.get("action") == action:
            return int(r.structured.get("minutes", 0))
    return 0


def seed_default_rules(session: Session, teacher_id: int) -> int:
    existing = list(session.scalars(select(TeacherRule).where(TeacherRule.teacher_id == teacher_id)))
    texts = {r.text for r in existing}
    teacher_actions = {(r.structured or {}).get("action") for r in existing if r.active and r.scope != RuleScope.default}
    n = 0
    for d in DEFAULT_RULES:
        if d["text"] in texts:
            continue
        session.add(TeacherRule(teacher_id=teacher_id, scope=RuleScope.default, category=d["category"], text=d["text"], structured=d["structured"], source="default",
                                active=d["structured"]["action"] not in teacher_actions))
        n += 1
    return n
