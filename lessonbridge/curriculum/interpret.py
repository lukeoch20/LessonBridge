"""Curriculum interpretation.

Turns the teacher's syllabus / pacing guide (plus the district default
sequence) into units and lessons. Source precedence: teacher-confirmed >
teacher syllabus/pacing guide > district curriculum > state standards >
LessonBridge inference. Conflicts are surfaced, never silently resolved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CurriculumUnit, Lesson, LessonDependency, LessonType, Priority, TeacherCourse
from ..providers.defaults import DEFAULT_CURRICULA
from ..schemas import CurriculumSpec, ExtractedTeacherContext, LessonSpec, RuleSpec, UnitSpec

_STOP = {"the", "and", "of", "a", "an", "to", "in", "unit", "quarter", "week", "weeks", "q1", "q2", "q3", "q4", "with", "for"}

_UNIT_LINE = re.compile(r"^\s*(?:unit|module|topic)\s*(\d+)?\s*[:\-–—.]\s*(.+?)\s*$", re.I)
_QUARTER_LINE = re.compile(r"^\s*(?:quarter|q|marking period|mp)\s*([1-4])\b[:\-–—. ]*(.*)$", re.I)
_WEEK_LINE = re.compile(r"^\s*weeks?\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*[:\-–—.]\s*(.+?)\s*$", re.I)
_DAYS_HINT = re.compile(r"\((\d+)\s*(?:days|class periods|periods|lessons)\)", re.I)
_RULE_HINT = re.compile(r"\b(quiz|test|assessment|minutes|late work|early finisher|substitute|sub\b|homework|chromebook|bathroom|hall pass|collect|grade|grading|independent reading|procedure|policy|expectation)", re.I)
_ASSESSMENT_HINT = re.compile(r"\b(quiz|test|exam|essay due|project due|assessment)\b", re.I)
_MATERIAL_HINT = re.compile(r"\b(textbook|novel|packet|slides|workbook|chromebook|notebook|binder|handout)\b", re.I)


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "unit"


# ------------------------------------------------------------ heuristic extraction
def heuristic_extract(text: str, subject: str) -> ExtractedTeacherContext:
    """Cheap, deterministic extraction used when no LLM is configured (and as a safety net)."""
    units: list[UnitSpec] = []
    rules: list[RuleSpec] = []
    materials: set[str] = set()
    assessments: list[str] = []
    prefs: dict[str, str] = {}
    quarter: Optional[int] = None
    seen_units: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip(" \t-•*·")
        if not line:
            continue
        mq = _QUARTER_LINE.match(line)
        if mq and len(line) < 80:
            quarter = int(mq.group(1))
            rest = mq.group(2).strip(" :-–—")
            if rest and len(rest) > 3 and _slug(rest) not in seen_units and not _RULE_HINT.search(rest):
                units.append(UnitSpec(slug=_slug(rest), title=rest, quarter=quarter, planned_days=_days(rest, 10), source="syllabus"))
                seen_units.add(_slug(rest))
            continue
        mu = _UNIT_LINE.match(line) or _WEEK_LINE.match(line)
        if mu and len(line) < 140:
            title = mu.groups()[-1].strip()
            title = _DAYS_HINT.sub("", title).strip(" :-–—")
            if title and _slug(title) not in seen_units:
                days = _days(line, 10)
                if isinstance(mu.re, re.Pattern) and mu.re is _WEEK_LINE and mu.group(2):
                    days = (int(mu.group(2)) - int(mu.group(1)) + 1) * 5
                elif mu.re is _WEEK_LINE:
                    days = 5
                units.append(UnitSpec(slug=_slug(title), title=title, quarter=quarter, planned_days=days, source="syllabus"))
                seen_units.add(_slug(title))
            continue
        if _RULE_HINT.search(line) and 15 < len(line) < 300 and not line.lower().startswith(("unit", "week", "students need", "students bring", "bring ", "you will need", "materials")):
            if re.search(r"\b(will|must|should|may|are|is|receive|get|have|no|always|never|only)\b", line, re.I):
                rules.append(RuleSpec(text=line, category="procedure"))
        if _ASSESSMENT_HINT.search(line) and len(line) < 120:
            assessments.append(line)
        for m in _MATERIAL_HINT.finditer(line):
            materials.add(m.group(1).lower())
        lower = line.lower()
        if "prefer" in lower and len(line) < 200:
            prefs[_slug(line)[:40]] = line

    # Merge successive week lines into one unit when they repeat a title.
    return ExtractedTeacherContext(units=units, rules=rules, preferences=prefs, materials=sorted(materials), assessments=assessments[:30], confidence=0.4 if units else 0.2)


def _days(text: str, default: int) -> int:
    m = _DAYS_HINT.search(text)
    return int(m.group(1)) if m else default


# -------------------------------------------------------------------- merging
@dataclass
class MergeResult:
    spec: CurriculumSpec
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _match_score(teacher_title: str, default_title: str, default_summary: str = "") -> float:
    """How well a teacher's unit title matches a default unit: title coverage first, summary as a weaker signal."""
    ka, kt = _keywords(teacher_title), _keywords(default_title)
    if not ka or not kt:
        return 0.0
    title_cov = len(ka & kt) / len(ka)
    jacc = len(ka & kt) / len(ka | kt)
    ks = _keywords(default_summary)
    summary_cov = len(ka & ks) / len(ka) if ks else 0.0
    return max(0.6 * title_cov + 0.4 * jacc, 0.5 * summary_cov)


def _scaffold_lessons(unit: UnitSpec, subject: str) -> list[LessonSpec]:
    """Generic lesson scaffold for a teacher-named unit we have no default lessons for."""
    base = unit.slug
    n = max(4, min(unit.planned_days, 12))
    lessons = [
        LessonSpec(slug=f"{base}-intro", title=f"Introduction: {unit.title}", objective=f"Introduce key concepts and vocabulary for {unit.title}.", lesson_type="direct_instruction", materials=[f"{base}_slides", "guided_notes"], student_output=["completed guided notes"], standards=unit.standards),
    ]
    for i in range(2, n - 1):
        prev = lessons[-1].slug
        kind = "guided_practice" if i % 2 == 0 else "independent_practice"
        lessons.append(LessonSpec(slug=f"{base}-day-{i}", title=f"{unit.title}: {'guided' if kind == 'guided_practice' else 'independent'} practice {i - 1}", objective=f"Practice and extend {unit.title} skills.", lesson_type=kind, delivery_requirement=["regular_teacher", "long_term_sub", "any_sub"] if kind == "independent_practice" else ["regular_teacher", "long_term_sub"], prerequisites=[prev], materials=[f"{base}_practice_{i-1}"], student_output=["practice work"], standards=unit.standards))
    lessons.append(LessonSpec(slug=f"{base}-review", title=f"Review: {unit.title}", objective=f"Review {unit.title} before the assessment.", lesson_type="review", delivery_requirement=["regular_teacher", "long_term_sub", "any_sub"], prerequisites=[lessons[-1].slug], materials=[f"{base}_review_packet"], student_output=["review packet"], standards=unit.standards))
    lessons.append(LessonSpec(slug=f"{base}-assessment", title=f"Assessment: {unit.title}", objective=f"Demonstrate mastery of {unit.title}.", lesson_type="assessment", delivery_requirement=["regular_teacher", "long_term_sub", "any_sub"], prerequisites=[f"{base}-review"], materials=[f"{base}_assessment"], student_output=[f"completed {unit.title} assessment"], standards=unit.standards, minimum_viable_minutes=45, required_components=["assessment administered"]))
    return lessons


def merge_curriculum(subject: str, extracted: ExtractedTeacherContext | None, default: CurriculumSpec | None = None) -> MergeResult:
    """Combine the teacher's extracted units with the default sequence. Teacher order wins."""
    default = default or DEFAULT_CURRICULA[subject]()
    if not extracted or not extracted.units:
        return MergeResult(default, notes=["No units found in teacher documents; using the district/LessonBridge default sequence."])

    used_default: set[str] = set()
    merged: list[UnitSpec] = []
    conflicts: list[str] = []
    for tu in extracted.units:
        best, best_score = None, 0.0
        for du in default.units:
            if du.slug in used_default:
                continue
            s = _match_score(tu.title, du.title, du.summary)
            if s > best_score:
                best, best_score = du, s
        if best is not None and best_score >= 0.3:
            used_default.add(best.slug)
            unit = best.model_copy(deep=True)
            unit.title = tu.title if len(tu.title) <= len(best.title) + 20 else best.title
            if tu.quarter and best.quarter and tu.quarter != best.quarter:
                conflicts.append(f"{subject}: syllabus places '{tu.title}' in Q{tu.quarter}; district default has it in Q{best.quarter}. Using the syllabus.")
            unit.quarter = tu.quarter or best.quarter
            if tu.planned_days and tu.planned_days != 10:
                unit.planned_days = max(tu.planned_days, len(unit.lessons))
            unit.source = "syllabus"
            merged.append(unit)
        else:
            unit = tu.model_copy(deep=True)
            unit.lessons = _scaffold_lessons(unit, subject)
            unit.planned_days = max(unit.planned_days, len(unit.lessons))
            unit.source = "syllabus"
            merged.append(unit)
    leftovers = [du for du in default.units if du.slug not in used_default]
    notes = []
    if leftovers:
        notes.append("Default units not mentioned in the syllabus were kept at the end of the year (teacher can delete): " + ", ".join(u.title for u in leftovers))
        merged.extend(leftovers)
    # Normalise quarters: inherit from previous unit when missing.
    last_q = 1
    for u in merged:
        if u.quarter is None:
            u.quarter = last_q
        last_q = u.quarter
    spec = CurriculumSpec(subject=subject, grade=default.grade, units=merged, source_notes=["Merged teacher syllabus with default sequence."])
    return MergeResult(spec, conflicts, notes)


# ---------------------------------------------------------------- persistence
def load_curriculum(session: Session, section: TeacherCourse, spec: CurriculumSpec, *, replace: bool = True) -> int:
    """Persist a CurriculumSpec as units/lessons/dependencies for a section. Returns lesson count."""
    if replace:
        for u in list(section.units):
            session.delete(u)
        session.flush()
    slug_to_lesson: dict[str, Lesson] = {}
    count = 0
    for ui, u in enumerate(spec.units, start=1):
        unit = CurriculumUnit(teacher_course_id=section.id, sequence=ui, slug=u.slug, title=u.title, summary=u.summary, standards=u.standards, quarter=u.quarter, planned_days=u.planned_days, source=u.source)
        session.add(unit)
        session.flush()
        for li, l in enumerate(u.lessons, start=1):
            lesson = Lesson(
                unit_id=unit.id, sequence=li, slug=l.slug, title=l.title, objective=l.objective,
                lesson_type=LessonType(l.lesson_type), duration_minutes=l.duration_minutes, minimum_viable_minutes=l.minimum_viable_minutes,
                priority=Priority(l.priority), delivery_requirement=l.delivery_requirement, materials=l.materials, student_output=l.student_output,
                standards=l.standards, required_components=l.required_components, optional_components=l.optional_components,
                can_move=l.can_move, quarter_boundary_allowed=l.quarter_boundary_allowed, hard_date=l.hard_date, notes=l.notes,
            )
            session.add(lesson)
            session.flush()
            slug_to_lesson[l.slug] = lesson
            count += 1
    for u in spec.units:
        for l in u.lessons:
            for pre in l.prerequisites:
                if pre in slug_to_lesson and l.slug in slug_to_lesson:
                    session.add(LessonDependency(lesson_id=slug_to_lesson[l.slug].id, depends_on_lesson_id=slug_to_lesson[pre].id, kind="before"))
    session.flush()
    session.refresh(section)
    return count


def extract_teacher_context(text: str, subject: str, *, llm=None) -> tuple[ExtractedTeacherContext, str]:
    """Use the LLM when available, otherwise heuristics. Returns (context, method)."""
    if llm is not None and llm.available():
        try:
            ctx = llm.extract_teacher_context(text, subject)
            if ctx.units or ctx.rules:
                return ctx, "llm"
        except Exception:
            pass
    return heuristic_extract(text, subject), "heuristic"
