"""Extended-leave planning: pre-leave analysis, handoff, weekly frameworks, return reconciliation."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..curriculum.calendar import entries_between, load_year_structure, next_entries, school_days
from ..models import (
    AbsenceEvent,
    CalendarChange,
    CalendarEntry,
    EntryKind,
    EntryStatus,
    LeavePlan,
    Lesson,
    LessonDependency,
    LessonType,
    Priority,
    ProposalStatus,
    TeacherCourse,
)
from ..profile.rules import resolve_rules, rule_minutes
from ..schemas import ReturnBrief, ReturnBriefSection, WeeklyFramework
from .service import next_school_day
from .state import entry_state

HANDOFF_DAYS = 10


def _ensure_plan(session: Session, absence: AbsenceEvent) -> LeavePlan:
    if absence.leave_plan is None:
        absence.leave_plan = LeavePlan(absence_id=absence.id)
        session.add(absence.leave_plan)
        session.flush()
    return absence.leave_plan


def _ys(session: Session, absence: AbsenceEvent):
    return load_year_structure(session, absence.teacher.school.district_slug, absence.teacher.school_year)


def return_date(session: Session, absence: AbsenceEvent) -> date:
    """The first day students attend after the leave (LB-52)."""
    return next_school_day(_ys(session, absence), absence.end_date)


def pre_leave_analysis(session: Session, absence: AbsenceEvent) -> dict:
    """Units, assessments, prerequisite chains across the leave start, pull-forward candidates, undetailed days, hard deadlines."""
    ys = _ys(session, absence)
    back = return_date(session, absence)
    out = {"leave_window": {"start": absence.start_date.isoformat(), "end": absence.end_date.isoformat(), "return": back.isoformat()}, "sections": [], "hard_deadlines": []}
    for q, qe in sorted(ys.quarter_ends.items()):
        if absence.start_date <= qe <= absence.end_date:
            out["hard_deadlines"].append(f"Quarter {q} ends {qe} during leave")
    testing = sorted(d for d in ys.testing if absence.start_date <= d <= absence.end_date)
    if testing:
        out["hard_deadlines"].append(f"SOL testing window overlaps leave ({testing[0]} to {testing[-1]})")
    out["hard_deadlines"].append(f"Teacher returns {back:%A %B %d, %Y}")
    for section in absence.teacher.sections:
        entries = entries_between(session, section.id, absence.start_date, absence.end_date)
        units: list[str] = []
        for e in entries:
            if e.unit and e.unit.title not in units:
                units.append(e.unit.title)
        assessments = [f"{e.date}: {e.title}" for e in entries if e.lesson and e.lesson.is_assessment]
        sensitive = [f"{e.date}: {e.title} (needs the regular teacher)" for e in entries if e.lesson and "long_term_sub" not in (e.lesson.delivery_requirement or [])]
        # Prerequisite chains that cross the leave start: taught just before leave, continued during it.
        pre_days = school_days(ys, absence.start_date - timedelta(days=28), absence.start_date - timedelta(days=1), section.meeting_days)
        pre_entries = entries_between(session, section.id, pre_days[0].date if pre_days else absence.start_date, absence.start_date - timedelta(days=1))
        taught_before = {e.lesson.slug: e for e in pre_entries if e.lesson}
        chains = []
        for e in entries[:20]:
            if not e.lesson:
                continue
            for p in sorted(e.lesson.prerequisite_slugs):
                if p in taught_before:
                    chains.append(f"'{taught_before[p].title}' ({taught_before[p].date}) is taught just before leave; '{e.title}' ({e.date}) builds on it during leave")
        straddling = []
        for title in units[:2]:
            before_n = sum(1 for e in pre_entries if e.unit and e.unit.title == title and e.lesson)
            if before_n:
                straddling.append(f"'{title}' starts before the leave ({before_n} lesson day(s) taught by you) and continues during it")
        multi_step = [f"{e.date}: {e.title} is due during leave; drafting happens before leave" for e in entries[:25]
                      if e.lesson and e.lesson.lesson_type in (LessonType.assessment, LessonType.writing_workshop) and any(p in taught_before for p in e.lesson.prerequisite_slugs)]
        flex_before = [e.date for e in pre_entries if e.lesson_id is None and e.kind in (EntryKind.flex, EntryKind.placeholder) and e.status == EntryStatus.planned]
        launches = [e for e in entries[:5] if e.lesson and e.lesson.sequence == 1]
        pull = [f"'{e.title}' launches a unit on {e.date}; you could launch it yourself on {flex_before[-1 - i]} before leave" for i, e in enumerate(launches[: len(flex_before)])]
        undetailed = [f"{e.date}: {e.title}" for e in entries if e.lesson_id is None and e.kind == EntryKind.placeholder]
        out["sections"].append({
            "section": section.section_name, "section_id": section.id, "course_days_during_leave": len(entries), "units": units,
            "assessments": assessments, "sensitive_dependencies": sensitive + chains, "straddling_units": straddling, "multi_step_work": multi_step,
            "complete_before_leave": pull, "undetailed_days": undetailed,
            "last_day_before_leave": max((e.date for e in pre_entries), default=None).isoformat() if pre_entries else None,
        })
    plan = _ensure_plan(session, absence)
    plan.pre_leave = out
    return out


def handoff_days(session: Session, absence: AbsenceEvent, section: TeacherCourse, n: int = HANDOFF_DAYS) -> list[CalendarEntry]:
    entries = [e for e in entries_between(session, section.id, absence.start_date, absence.end_date) if e.status == EntryStatus.planned]
    return entries[:n]


def _unique_slug(section: TeacherCourse, base: str) -> str:
    taken = {l.slug for u in section.units for l in u.lessons}
    k = 1
    while f"{base}-drafted-{k}" in taken:
        k += 1
    return f"{base}-drafted-{k}"


def draft_placeholders(session: Session, absence: AbsenceEvent, *, n: int = HANDOFF_DAYS, llm=None) -> list[str]:
    """Turn 'lesson to be detailed' days among the first ``n`` leave days into drafted lessons (LB-35).

    Each drafted lesson is an extension of the lesson before it, uses only that
    unit's existing materials, never an assessment, and is marked as drafted so
    the teacher can review it. The calendar day keeps its date and substitute
    status; only its content is filled in (audited as a "draft" change).
    """
    drafted: list[str] = []
    for section in absence.teacher.sections:
        for e in handoff_days(session, absence, section, n):
            if e.lesson_id is not None or e.kind != EntryKind.placeholder or e.unit is None:
                continue
            unit = e.unit
            ordered = sorted(unit.lessons, key=lambda l: l.sequence)
            prior_days = [x for x in entries_between(session, section.id, unit_start(session, section, unit), e.date - timedelta(days=1)) if x.lesson and x.lesson.unit_id == unit.id]
            prev: Optional[Lesson] = prior_days[-1].lesson if prior_days else (ordered[0] if ordered else None)
            nxt = next((x.lesson for x in next_entries(session, section.id, e.date, 10) if x.lesson and x.lesson.unit_id == unit.id), None)
            materials = sorted({m for l in ordered for m in (l.materials or [])})
            spec = None
            if llm is not None and llm.available():
                try:
                    spec = llm.draft_lesson({"unit": unit.title, "unit_summary": unit.summary, "previous_lesson": prev.title if prev else None,
                                             "previous_objective": prev.objective if prev else None, "next_lesson": nxt.title if nxt else None, "materials": materials})
                    spec.materials = [m for m in spec.materials if m in materials] or (list(prev.materials or []) if prev else [])
                except Exception:  # noqa: BLE001 - fall back to the deterministic draft
                    spec = None
            if spec is None:
                base_title = prev.title if prev else unit.title
                title = f"Extended practice: {base_title}"
                objective = (f"Apply and extend '{prev.title}': {prev.objective}" if prev else f"Practice the core skills of {unit.title}: {unit.summary}")
                lt, mats, outs = LessonType.guided_practice, list(prev.materials or []) if prev else materials[:2], ["extended practice work"]
            else:
                title, objective, lt, mats, outs = spec.title, spec.objective, LessonType(spec.lesson_type), spec.materials, spec.student_output or ["extended practice work"]
            lesson = Lesson(unit_id=unit.id, sequence=0, slug=_unique_slug(section, unit.slug), title=title, objective=objective, lesson_type=lt,
                            duration_minutes=section.minutes_per_meeting, minimum_viable_minutes=20, priority=Priority.recommended,
                            delivery_requirement=["regular_teacher", "long_term_sub", "any_sub"], materials=mats, student_output=outs,
                            standards=list(prev.standards or []) if prev else list(unit.standards or []), required_components=["guided practice on the core task"],
                            optional_components=["extension problems"], origin="drafted", notes="Drafted by LessonBridge to fill a 'lesson to be detailed' day; review it.")
            session.add(lesson)
            session.flush()
            # Insert it right after the previous lesson in the unit sequence.
            idx = ordered.index(prev) + 1 if prev in ordered else len(ordered)
            for i, l in enumerate(ordered[:idx] + [lesson] + ordered[idx:], start=1):
                l.sequence = i
            if prev is not None:
                session.add(LessonDependency(lesson_id=lesson.id, depends_on_lesson_id=prev.id, kind="before"))
            before = entry_state(e)
            e.lesson_id, e.kind, e.title = lesson.id, EntryKind.lesson, lesson.title
            session.flush()
            session.refresh(e)
            session.add(CalendarChange(teacher_id=absence.teacher_id, teacher_course_id=section.id, change_type="draft", date=e.date, before=before,
                                       after=entry_state(e), reason="placeholder filled with a drafted lesson for the leave handoff"))
            drafted.append(f"{section.section_name} {e.date}: {lesson.title}")
    session.flush()
    return drafted


def unit_start(session: Session, section: TeacherCourse, unit) -> date:
    first = session.scalar(select(CalendarEntry.date).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.unit_id == unit.id).order_by(CalendarEntry.date))
    return first or date.min


def _rules_with_when(session: Session, absence: AbsenceEvent, section: Optional[TeacherCourse] = None) -> list[dict]:
    rules = resolve_rules(session, absence.teacher_id, section=section, include_assessment_rules=True)
    return [{"text": r.text, "applies": r.applies, "category": r.category} for r in rules]


def build_handoff(session: Session, absence: AbsenceEvent, sub_plan_ids_by_entry: dict[int, int] | None = None) -> dict:
    """Handoff packet: first 10 instructional days (plans generated separately), every rule and when it applies, unit context, assessment guidance, boundaries (LB-34)."""
    teacher = absence.teacher
    study = rule_minutes(resolve_rules(session, teacher.id, is_assessment=True), "study_period")
    packet = {"start": absence.start_date.isoformat(), "return": return_date(session, absence).isoformat(), "detailed_days": HANDOFF_DAYS,
              "sections": [], "classroom_rules": _rules_with_when(session, absence), "flexibility_boundaries": [
        "Keep assessments on their scheduled dates unless an approved calendar change moved them.",
        "New instruction may be taught; follow the suggested sequence and do not skip required lessons.",
        "Do not reorder across units; within a unit, a day may be split or extended by one day if students need it (record it as a slip).",
        "Collect every deliverable listed; grading guidance follows the teacher's rules.",
    ]}
    for section in teacher.sections:
        days = handoff_days(session, absence, section)
        units = {}
        for e in days:
            if e.unit:
                units.setdefault(e.unit.title, {"summary": e.unit.summary, "standards": e.unit.standards})
        later = entries_between(session, section.id, (days[-1].date + timedelta(days=1)) if days else absence.start_date, absence.end_date)
        study_note = f" Give the {study}-minute pre-quiz study period first." if study else ""
        packet["sections"].append({
            "section": section.section_name, "section_id": section.id, "period": section.period, "minutes": section.minutes_per_meeting,
            "days": [{"date": e.date.isoformat(), "title": e.title, "kind": e.kind.value, "entry_id": e.id, "sub_plan_id": (sub_plan_ids_by_entry or {}).get(e.id),
                      "drafted": bool(e.lesson and e.lesson.origin == "drafted")} for e in days],
            "unit_context": units,
            "section_rules": [r for r in _rules_with_when(session, absence, section) if r not in packet["classroom_rules"]],
            "assessment_guidance": [f"{e.date}: {e.title} — administer as scheduled.{study_note} Collect and leave for the teacher unless the rules say otherwise."
                                    for e in days + later if e.lesson and e.lesson.is_assessment],
        })
    plan = _ensure_plan(session, absence)
    plan.handoff = packet
    return packet


def weekly_frameworks(session: Session, absence: AbsenceEvent) -> list[WeeklyFramework]:
    """After the detailed days: a weekly pacing framework per section for the rest of the leave."""
    frameworks: list[WeeklyFramework] = []
    ys = _ys(session, absence)
    for section in absence.teacher.sections:
        detailed = handoff_days(session, absence, section)
        start = (detailed[-1].date + timedelta(days=1)) if detailed else absence.start_date
        entries = entries_between(session, section.id, start, absence.end_date)
        by_week: dict[date, list[CalendarEntry]] = defaultdict(list)
        for e in entries:
            by_week[e.date - timedelta(days=e.date.isoweekday() - 1)].append(e)
        constraints = [f"{r['text']} ({r['applies']})" for r in _rules_with_when(session, absence, section)]  # every rule, no truncation (LB-34)
        for wk in sorted(by_week):
            es = by_week[wk]
            units, objectives, standards, materials = [], [], [], []
            for e in es:
                if e.unit and e.unit.title not in units:
                    units.append(e.unit.title)
                if e.lesson:
                    objectives.append(f"{e.date:%a}: {e.lesson.objective or e.title}")
                    standards += [s for s in e.lesson.standards or [] if s not in standards]
                    materials += [m for m in e.lesson.materials or [] if m not in materials]
                else:
                    objectives.append(f"{e.date:%a}: {e.title}")
            deadlines = [f"Quarter {q} ends {qe}" for q, qe in ys.quarter_ends.items() if wk <= qe <= wk + timedelta(days=6)]
            frameworks.append(WeeklyFramework(
                week_start=wk, week_end=wk + timedelta(days=4), course=section.section_name, section_id=section.id, unit_titles=units, objectives=objectives,
                required_assessments=[f"{e.date}: {e.title}" for e in es if e.lesson and e.lesson.is_assessment], deadlines=deadlines, standards=standards,
                suggested_sequence=[f"{e.date:%a %b %d}: {e.title}" for e in es], materials=materials, constraints=constraints,
                flexibility_notes="Days may be split or extended within the week; keep assessments and deadlines fixed. Record any slip so the calendar can be reconciled.",
            ))
    plan = _ensure_plan(session, absence)
    plan.weekly_frameworks = [f.model_dump(mode="json") for f in frameworks]
    return frameworks


def return_brief(session: Session, absence: AbsenceEvent) -> ReturnBrief:
    """What was completed, moved, skipped; assessment status; outstanding work; re-entry point; first week back.

    Calendar changes come only from this absence's own approved proposals, and
    rows that changed nothing are left out (LB-36).
    """
    sections: list[ReturnBriefSection] = []
    own = [p.id for p in absence.proposals if p.status in (ProposalStatus.approved, ProposalStatus.reverted)]
    rows = list(session.scalars(select(CalendarChange).where(CalendarChange.proposal_id.in_(own or [-1])).order_by(CalendarChange.date))) if own else []
    rows = [c for c in rows if c.change_type != "keep" and c.before != c.after]
    today = date.today()
    for section in absence.teacher.sections:
        entries = entries_between(session, section.id, absence.start_date, absence.end_date)
        completed = [f"{e.date}: {e.title}" for e in entries if e.status == EntryStatus.completed]
        skipped = [f"{e.date}: {e.title}" for e in entries if e.status == EntryStatus.skipped]
        moved = [f"{c.date}: {c.change_type} — {(c.before or {}).get('title') or 'no entry'} → {(c.after or {}).get('title')}" for c in rows if c.teacher_course_id == section.id]
        assessments = [f"{e.date}: {e.title} — {e.status.value}" for e in entries if e.lesson and e.lesson.is_assessment]
        outstanding = [f"{e.date}: {e.title} (not marked complete)" for e in entries if e.status == EntryStatus.planned and e.date <= today]
        owed = [f"Owed (not on the calendar): {o.title}" for o in section.owed if o.open]
        collect = [f"Collect: {', '.join(e.lesson.student_output)} from {e.date:%b %d}" for e in entries if e.lesson and e.lesson.student_output and e.status != EntryStatus.skipped][-3:]
        nxt = next_entries(session, section.id, absence.end_date, 5)
        reentry = f"Resume with '{nxt[0].title}' on {nxt[0].date}" if nxt else "No planned entries after the absence"
        if skipped:
            reentry += f"; first address skipped: {skipped[0]}"
        sections.append(ReturnBriefSection(course=section.section_name, completed=completed, moved=moved, skipped=skipped, assessment_status=assessments,
                                           outstanding_work=outstanding + owed + collect, recommended_reentry_point=reentry, first_week_back=[f"{e.date:%a %b %d}: {e.title}" for e in nxt]))
    summary = []
    for s in sections:
        n = len(s.moved)
        if n:
            summary.append(f"{s.course}: {n} day(s) changed by this absence's plan")
    follow = []
    for s in sections:
        follow.extend(s.outstanding_work[:2])
        follow.append(s.recommended_reentry_point)
    brief = ReturnBrief(absence_id=absence.id, start_date=absence.start_date, end_date=absence.end_date, sections=sections, calendar_changes=summary, follow_up=follow)
    if absence.end_date < today and absence.status.value == "active":
        from ..models import AbsenceStatus

        absence.status = AbsenceStatus.completed
    plan = _ensure_plan(session, absence)
    plan.return_brief = brief.model_dump(mode="json")
    return brief
