"""Atomic daily-plan generation.

A multi-day sequence is planned holistically by the reconciler; each detailed
course-day is generated in its own call, validated, stored, and retried with
explicit feedback when the quality gate fails.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, settings as default_settings
from ..curriculum.calendar import entries_between, entry_on, next_entries, section_days
from ..models import AbsenceEvent, AbsenceType, CalendarEntry, EntryStatus, GenerationAttempt, ReplacementActivity, SubPlan, SubPlanStatus, TeacherCourse
from ..profile.rules import resolve_rules
from ..render.markdown import render_sub_plan
from ..schemas import SubPlanContent
from .llm import GenerationError, LLMClient
from .template import generate_template_plan
from .validate import validate_plan

GENERAL_MATERIALS = ["paper", "pencils", "independent reading books"]


def build_context(session: Session, absence: AbsenceEvent, section: TeacherCourse, entry: CalendarEntry) -> dict[str, Any]:
    lesson = entry.lesson
    prev = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date < entry.date).order_by(CalendarEntry.date.desc()))
    nxt = next_entries(session, section.id, entry.date, 1)
    rules = resolve_rules(session, absence.teacher_id, section=section, lesson=lesson, unit=entry.unit, is_assessment=bool(lesson and lesson.is_assessment))
    activity: Optional[ReplacementActivity] = None
    if entry.is_sub_day and entry.kind.value == "filler":
        # Find the replacement activity by title prefix.
        base = entry.title.replace(" [SUB]", "")
        activity = session.scalar(select(ReplacementActivity).where(ReplacementActivity.title == base))
    materials: list[str] = []
    outputs: list[str] = []
    standards: list[str] = []
    merged_titles: list[str] = []
    if lesson:
        materials += list(lesson.materials or [])
        outputs += list(lesson.student_output or [])
        standards += list(lesson.standards or [])
    for mid in entry.merged_lesson_ids or []:
        from ..models import Lesson

        ml = session.get(Lesson, mid)
        if ml:
            merged_titles.append(ml.title)
            materials += list(ml.materials or [])
            outputs += list(ml.student_output or [])
    if activity:
        materials += list(activity.materials or [])
        outputs.append(activity.student_output)
    is_slack = lesson is None and activity is None
    if is_slack and entry.unit:
        # Flex / to-be-detailed day: catch up and extend using materials already introduced in the unit.
        prior_lessons = [e.lesson for e in entries_between(session, section.id, entry.date - timedelta(days=60), entry.date - timedelta(days=1)) if e.lesson and e.unit_id == entry.unit_id]
        for pl in prior_lessons[-4:]:
            materials += list(pl.materials or [])
            standards += list(pl.standards or [])
        outputs += ["unfinished work from earlier in the unit", "extension practice"]
    materials += GENERAL_MATERIALS
    if lesson:
        objective = lesson.objective
    elif activity:
        objective = activity.description
    elif entry.unit:
        objective = f"Catch up on unfinished work and extend practice in {entry.unit.title}: {entry.unit.summary}".strip()
    else:
        objective = entry.title
    if merged_titles:
        objective = f"{objective} Also cover the essentials of: {', '.join(merged_titles)}."
    return {
        "date": entry.date.isoformat(), "weekday": entry.date.strftime("%A"), "period": section.period, "course": section.section_name,
        "class_minutes": section.minutes_per_meeting, "substitute_type": absence.substitute_type.value,
        "kind": "filler" if activity else ("flex" if is_slack else entry.kind.value), "lesson_title": entry.title, "lesson_type": lesson.lesson_type.value if lesson else ("filler" if activity else "flex"),
        "objective": objective, "is_assessment": bool(lesson and lesson.is_assessment), "unit": entry.unit.title if entry.unit else None,
        "unit_summary": entry.unit.summary if entry.unit else None, "standards": list(dict.fromkeys(standards)),
        "available_materials": list(dict.fromkeys(materials)), "expected_outputs": list(dict.fromkeys(outputs)) or ["completed class work"],
        "rules": [{"text": r.text, "category": r.category, "structured": r.structured} for r in rules],
        "prior_state": f"{prev.title} ({prev.status.value})" if prev else "first day of the sequence",
        "next_day": nxt[0].title if nxt else None,
        "next_day_note": (f"Tomorrow is '{nxt[0].title}'; note anything students did not finish so it can be addressed first." if nxt else "Note anything unfinished for the teacher."),
        "activity_description": activity.description if activity else None,
        "is_meeting_day": entry.date.isoweekday() in [int(d) for d in section.meeting_days],
        "merged_lessons": merged_titles,
    }


def _finish(plan: SubPlanContent, ctx: dict[str, Any]) -> SubPlanContent:
    from datetime import date as _date

    return plan.model_copy(update={"date": _date.fromisoformat(ctx["date"]), "period": str(ctx["period"]), "course": ctx["course"], "substitute_type": ctx["substitute_type"], "standards": ctx.get("standards", [])})


def generate_for_entry(session: Session, absence: AbsenceEvent, section: TeacherCourse, entry: CalendarEntry, *, llm: LLMClient | None = None, cfg: Settings | None = None) -> SubPlan:
    cfg = cfg or default_settings
    llm = llm or LLMClient(cfg)
    ctx = build_context(session, absence, section, entry)
    plan_row = session.scalar(select(SubPlan).where(SubPlan.absence_id == absence.id, SubPlan.teacher_course_id == section.id, SubPlan.date == entry.date))
    if plan_row is None:
        plan_row = SubPlan(absence_id=absence.id, teacher_course_id=section.id, calendar_entry_id=entry.id, date=entry.date)
        session.add(plan_row)
        session.flush()
    for att in list(plan_row.generation_attempts):
        session.delete(att)
    plan_row.attempts = 0

    use_llm = cfg.generator in ("auto", "claude") and llm.available()
    feedback: list[str] = []
    final: SubPlanContent | None = None
    final_validation = None
    generator = "claude" if use_llm else "template"
    attempts = cfg.max_generation_attempts if use_llm else 1
    for n in range(1, attempts + 1):
        try:
            if use_llm:
                candidate = _finish(llm.generate_sub_plan(ctx, feedback), ctx)
            else:
                candidate = generate_template_plan(ctx)
        except GenerationError as exc:
            feedback = [str(exc)]
            session.add(GenerationAttempt(sub_plan_id=plan_row.id, attempt_no=n, passed=False, failures=[str(exc)], generator=generator))
            plan_row.attempts = n
            continue
        result = validate_plan(candidate, ctx)
        session.add(GenerationAttempt(sub_plan_id=plan_row.id, attempt_no=n, passed=result.passed, failures=result.failures, generator=generator, raw_output=candidate.model_dump(mode="json")))
        plan_row.attempts = n
        final, final_validation = candidate, result
        if result.passed:
            break
        feedback = result.failures
    if final is None or (final_validation and not final_validation.passed):
        # Fall back to the template so the substitute always has a usable plan.
        fallback = generate_template_plan(ctx)
        fb_result = validate_plan(fallback, ctx)
        session.add(GenerationAttempt(sub_plan_id=plan_row.id, attempt_no=plan_row.attempts + 1, passed=fb_result.passed, failures=fb_result.failures, generator="template"))
        plan_row.attempts += 1
        final, final_validation, generator = fallback, fb_result, "template-fallback"
    plan_row.content = final.model_dump(mode="json")
    plan_row.generator = generator
    plan_row.model = cfg.model if generator == "claude" else None
    plan_row.validation = final_validation.model_dump() if final_validation else {}
    plan_row.status = SubPlanStatus.accepted if (final_validation and final_validation.passed) else SubPlanStatus.failed_validation
    plan_row.rendered_markdown = render_sub_plan(final, teacher_name=absence.teacher.name, validation=final_validation)
    entry.sub_plan_id = plan_row.id
    session.flush()
    return plan_row


def generate_sub_plans(session: Session, absence: AbsenceEvent, *, days_limit: int | None = None, llm: LLMClient | None = None, cfg: Settings | None = None) -> list[SubPlan]:
    """Generate one plan per course-day in the absence (first 10 instructional days for extended leave)."""
    cfg = cfg or default_settings
    llm = llm or LLMClient(cfg)
    if days_limit is None:
        days_limit = 10 if absence.absence_type == AbsenceType.extended_leave else 10_000
    plans: list[SubPlan] = []
    for section in absence.teacher.sections:
        entries = [e for e in entries_between(session, section.id, absence.start_date, absence.end_date) if e.status == EntryStatus.planned]
        for entry in entries[:days_limit]:
            plans.append(generate_for_entry(session, absence, section, entry, llm=llm, cfg=cfg))
    return plans
