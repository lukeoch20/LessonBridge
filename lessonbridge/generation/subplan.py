"""Atomic daily-plan generation.

A multi-day sequence is planned holistically by the reconciler; each detailed
course-day is generated in its own call, validated, stored, and retried with
explicit feedback when the quality gate fails.

Work is split into three phases so a long run never holds the database write
lock (LB-41): contexts are built in a short read, model calls run outside any
transaction, and each plan is stored in its own short transaction. A failure
on one day falls back to the template for that day only (LB-62). Sections in
the same plan group with identical days share one generation (LB-55).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..absence.state import entry_state, fingerprint
from ..config import Settings, settings as default_settings
from ..curriculum.calendar import entries_between, load_year_structure, next_entries
from ..models import AbsenceEvent, AbsenceType, CalendarEntry, EntryStatus, GenerationAttempt, SubPlan, SubPlanStatus, TeacherCourse
from ..profile.rules import resolve_rules
from ..render.markdown import render_sub_plan
from ..schemas import SubPlanContent, ValidationResult
from .llm import GenerationError, LLMClient
from .template import generate_template_plan
from .validate import validate_plan

log = logging.getLogger(__name__)
GENERAL_MATERIALS = ["paper", "pencils", "independent reading books"]


def _split(key: str) -> str:
    return key.split("#", 1)[0]


def build_context(session: Session, absence: AbsenceEvent, section: TeacherCourse, entry: CalendarEntry) -> dict[str, Any]:
    lesson = entry.lesson
    prev = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date < entry.date).order_by(CalendarEntry.date.desc()))
    nxt = next_entries(session, section.id, entry.date, 1)
    rules = resolve_rules(session, absence.teacher_id, section=section, lesson=lesson, unit=entry.unit, is_assessment=bool(lesson and lesson.is_assessment))
    activity = entry.replacement_activity if entry.is_sub_day else None  # by id, never by title
    materials: list[str] = []
    outputs: list[str] = []
    standards: list[str] = []
    merged_titles: list[str] = []
    compressed: list[str] = []
    lessons_by_slug = {l.slug: l for u in section.units for l in u.lessons}
    for key in entry.merged_keys or []:
        ml = lessons_by_slug.get(_split(key))
        if ml:
            merged_titles.append(ml.title)
            materials += list(ml.materials or [])
            outputs += list(ml.student_output or [])
            standards += list(ml.standards or [])
            if ml.required_components:
                compressed.append(f"{ml.title}: {', '.join(ml.required_components)}")
    if lesson:
        materials += list(lesson.materials or [])
        outputs += list(lesson.student_output or [])
        standards += list(lesson.standards or [])
        if merged_titles and lesson.required_components:
            compressed.append(f"{lesson.title}: {', '.join(lesson.required_components)}")
    if activity:
        materials += list(activity.materials or [])
        outputs.append(activity.student_output)
    is_slack = lesson is None and activity is None
    if is_slack and entry.unit:
        # Flex / to-be-detailed day: catch up and extend using materials already introduced in the unit.
        prior = [e.lesson for e in entries_between(session, section.id, entry.date - timedelta(days=60), entry.date - timedelta(days=1)) if e.lesson and e.unit_id == entry.unit_id]
        for pl in prior[-4:]:
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
        objective = "Catch up on unfinished work and read independently."
    if merged_titles:
        objective = f"{objective} Also cover the essentials of: {', '.join(merged_titles)}."
    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    early = entry.date in ys.early_release
    title = entry.title + (" (continued)" if entry.continuation and "(continued)" not in entry.title else "")
    return {
        "date": entry.date.isoformat(), "weekday": entry.date.strftime("%A"), "period": section.period, "course": section.section_name,
        "class_minutes": section.minutes_on(early), "early_release": early, "substitute_type": absence.substitute_type.value,
        "kind": "filler" if activity else ("flex" if is_slack else entry.kind.value), "lesson_title": title,
        "lesson_type": lesson.lesson_type.value if lesson else ("filler" if activity else "flex"),
        "objective": objective, "is_assessment": bool(lesson and lesson.is_assessment and not entry.continuation), "unit": entry.unit.title if entry.unit else None,
        "unit_summary": entry.unit.summary if entry.unit else None, "standards": list(dict.fromkeys(standards)),
        "available_materials": list(dict.fromkeys(materials)), "expected_outputs": list(dict.fromkeys(outputs)) or ["completed class work"],
        "rules": [{"text": r.text, "category": r.category, "structured": r.structured} for r in rules],
        "prior_state": f"{prev.title} ({prev.status.value})" if prev else "first day of the sequence",
        "next_day": nxt[0].title if nxt else None,
        "next_day_note": (f"Next class is '{nxt[0].title}'; note anything students did not finish so it can be addressed first." if nxt else "Note anything unfinished for the teacher."),
        "activity_description": activity.description if activity else None,
        "is_meeting_day": entry.date.isoweekday() in [int(d) for d in section.meeting_days],
        "merged_lessons": merged_titles, "compressed": compressed,
        "drafted": bool(lesson and lesson.origin == "drafted"),
    }


# ------------------------------------------------------------------ jobs
@dataclass
class PlanJob:
    absence_id: int
    section_id: int
    entry_id: int
    date: date
    ctx: dict[str, Any]
    fingerprint: str
    group: tuple = ()


@dataclass
class PlanResult:
    content: SubPlanContent
    validation: ValidationResult
    generator: str
    attempts: list[dict] = field(default_factory=list)


def _finish(plan: SubPlanContent, ctx: dict[str, Any]) -> SubPlanContent:
    return plan.model_copy(update={"date": date.fromisoformat(ctx["date"]), "period": str(ctx["period"]), "course": ctx["course"], "substitute_type": ctx["substitute_type"], "standards": ctx.get("standards", [])})


def run_job(ctx: dict[str, Any], llm: LLMClient, cfg: Settings) -> PlanResult:
    """Generate and validate one course-day plan. Never raises: any failure falls back to the template."""
    attempts: list[dict] = []
    use_llm = cfg.generator in ("auto", "claude") and llm.available()
    if use_llm:
        feedback: list[str] = []
        for n in range(1, cfg.max_generation_attempts + 1):
            try:
                candidate = _finish(llm.generate_sub_plan(ctx, feedback), ctx)
            except GenerationError as exc:
                attempts.append({"generator": "claude", "passed": False, "failures": [str(exc)], "raw": {}})
                if not llm.available():
                    break  # credentials failed: stop calling, fall back below
                feedback = [str(exc)]
                continue
            except Exception as exc:  # noqa: BLE001 - one bad day must not abort the run
                log.exception("Unexpected generation failure")
                attempts.append({"generator": "claude", "passed": False, "failures": [f"unexpected error: {exc}"], "raw": {}})
                break
            result = validate_plan(candidate, ctx)
            attempts.append({"generator": "claude", "passed": result.passed, "failures": result.failures, "raw": candidate.model_dump(mode="json")})
            if result.passed:
                return PlanResult(candidate, result, "claude", attempts)
            feedback = result.failures
    plan = generate_template_plan(ctx)
    result = validate_plan(plan, ctx)
    attempts.append({"generator": "template", "passed": result.passed, "failures": result.failures, "raw": {}})
    return PlanResult(plan, result, "template-fallback" if use_llm else "template", attempts)


def prepare_jobs(session: Session, absence: AbsenceEvent, *, days_limit: Optional[int] = None, section_ids: Optional[set[int]] = None) -> list[PlanJob]:
    if days_limit is None:
        days_limit = 10 if absence.absence_type == AbsenceType.extended_leave else 10_000
    jobs: list[PlanJob] = []
    for section in absence.teacher.sections:
        if section_ids is not None and section.id not in section_ids:
            continue
        entries = [e for e in entries_between(session, section.id, absence.start_date, absence.end_date) if e.status == EntryStatus.planned]
        for entry in entries[:days_limit]:
            ctx = build_context(session, absence, section, entry)
            st = entry_state(entry)
            group_key = (section.plan_group or f"section-{section.id}", entry.date, st.get("lesson_slug"), st.get("title"), tuple(st.get("merged_keys") or []),
                         st.get("replacement_activity_id"), st.get("kind"), ctx["class_minutes"])
            jobs.append(PlanJob(absence.id, section.id, entry.id, entry.date, ctx, fingerprint(st), group_key))
    return jobs


def store_result(session: Session, job: PlanJob, res: PlanResult, cfg: Settings) -> SubPlan:
    absence = session.get(AbsenceEvent, job.absence_id)
    entry = session.get(CalendarEntry, job.entry_id)
    plan_row = session.scalar(select(SubPlan).where(SubPlan.absence_id == job.absence_id, SubPlan.teacher_course_id == job.section_id, SubPlan.date == job.date))
    if plan_row is None:
        plan_row = SubPlan(absence_id=job.absence_id, teacher_course_id=job.section_id, calendar_entry_id=job.entry_id, date=job.date)
        session.add(plan_row)
        session.flush()
    for att in list(plan_row.generation_attempts):
        session.delete(att)
    session.flush()
    content = res.content.model_copy(update={"period": str(job.ctx["period"]), "course": job.ctx["course"]})
    for i, a in enumerate(res.attempts, start=1):
        session.add(GenerationAttempt(sub_plan_id=plan_row.id, attempt_no=i, passed=a["passed"], failures=a["failures"], generator=a["generator"], raw_output=a["raw"]))
    plan_row.attempts = len(res.attempts)
    plan_row.calendar_entry_id = job.entry_id
    plan_row.content = content.model_dump(mode="json")
    plan_row.generator = res.generator
    plan_row.model = cfg.model if res.generator == "claude" else None
    plan_row.validation = res.validation.model_dump()
    plan_row.status = SubPlanStatus.accepted if res.validation.passed else SubPlanStatus.failed_validation
    plan_row.entry_fingerprint = job.fingerprint
    plan_row.rendered_markdown = render_sub_plan(content, teacher_name=absence.teacher.name, validation=res.validation, drafted=job.ctx.get("drafted", False))
    if entry is not None:
        entry.sub_plan_id = plan_row.id
    session.flush()
    return plan_row


def _run_grouped(jobs: list[PlanJob], llm: LLMClient, cfg: Settings) -> list[tuple[PlanJob, PlanResult]]:
    """One generation per plan group and day; parallel sections reuse it (validated against their own context)."""
    cache: dict[tuple, PlanResult] = {}
    out = []
    for job in jobs:
        res = cache.get(job.group)
        if res is None:
            res = run_job(job.ctx, llm, cfg)
            cache[job.group] = res
        else:
            content = res.content.model_copy(update={"period": str(job.ctx["period"]), "course": job.ctx["course"]})
            res = PlanResult(content, validate_plan(content, job.ctx), res.generator, res.attempts)
        out.append((job, res))
    return out


def generate_sub_plans(session: Session, absence: AbsenceEvent, *, days_limit: int | None = None, llm: LLMClient | None = None, cfg: Settings | None = None) -> list[SubPlan]:
    """In-session generation (tests, template generator). Use generate_plans_for_absence for Claude runs."""
    cfg = cfg or default_settings
    llm = llm or LLMClient(cfg)
    jobs = prepare_jobs(session, absence, days_limit=days_limit)
    return [store_result(session, job, res, cfg) for job, res in _run_grouped(jobs, llm, cfg)]


def generate_plans_for_absence(absence_id: int, *, days_limit: int | None = None, llm: LLMClient | None = None, cfg: Settings | None = None, only_stale: bool = False) -> list[int]:
    """Generate plans without holding a write transaction during model calls (LB-41). Returns plan ids."""
    from .. import db

    cfg = cfg or default_settings
    llm = llm or LLMClient(cfg)
    with db.session_scope() as s:
        absence = s.get(AbsenceEvent, absence_id)
        if absence is None:
            raise ValueError(f"No absence {absence_id}")
        jobs = prepare_jobs(s, absence, days_limit=days_limit)
        if only_stale:
            fresh = {(p.teacher_course_id, p.date) for p in absence.sub_plans if p.status != SubPlanStatus.stale}
            jobs = [j for j in jobs if (j.section_id, j.date) not in fresh]
    ids: list[int] = []
    for job, res in _run_grouped(jobs, llm, cfg):
        with db.session_scope() as s:
            ids.append(store_result(s, job, res, cfg).id)
    return ids
