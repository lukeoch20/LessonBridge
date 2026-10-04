"""Extended-leave planning: pre-leave analysis, handoff, weekly frameworks, return reconciliation."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..curriculum.calendar import entries_between, load_year_structure, next_entries, school_days
from ..models import AbsenceEvent, CalendarChange, CalendarEntry, EntryStatus, LeavePlan, TeacherCourse
from ..profile.rules import resolve_rules
from ..schemas import ReturnBrief, ReturnBriefSection, WeeklyFramework

HANDOFF_DAYS = 10


def _ensure_plan(session: Session, absence: AbsenceEvent) -> LeavePlan:
    if absence.leave_plan is None:
        absence.leave_plan = LeavePlan(absence_id=absence.id)
        session.add(absence.leave_plan)
        session.flush()
    return absence.leave_plan


def pre_leave_analysis(session: Session, absence: AbsenceEvent) -> dict:
    """Understand the leave window per section: units, assessments, sensitive dependencies, pull-forward candidates, hard deadlines."""
    ys = load_year_structure(session, absence.teacher.school.district_slug, absence.teacher.school_year)
    out = {"leave_window": {"start": absence.start_date.isoformat(), "end": absence.end_date.isoformat()}, "sections": [], "hard_deadlines": []}
    for q, qe in sorted(ys.quarter_ends.items()):
        if absence.start_date <= qe <= absence.end_date:
            out["hard_deadlines"].append(f"Quarter {q} ends {qe} during leave")
    testing = sorted(d for d in ys.testing if absence.start_date <= d <= absence.end_date)
    if testing:
        out["hard_deadlines"].append(f"SOL testing window overlaps leave ({testing[0]} to {testing[-1]})")
    out["hard_deadlines"].append(f"Teacher return date {absence.end_date + timedelta(days=1)}")
    for section in absence.teacher.sections:
        entries = entries_between(session, section.id, absence.start_date, absence.end_date)
        units = []
        seen = set()
        for e in entries:
            if e.unit and e.unit.id not in seen:
                seen.add(e.unit.id)
                units.append(e.unit.title)
        assessments = [f"{e.date}: {e.title}" for e in entries if e.lesson and e.lesson.is_assessment]
        sensitive = [f"{e.date}: {e.title}" for e in entries if e.lesson and "long_term_sub" not in (e.lesson.delivery_requirement or [])]
        # Candidates to complete before leave: teacher-only lessons in the first two weeks of leave, when flex exists before leave.
        pre_days = school_days(ys, max(ys.first_day, absence.start_date - timedelta(days=21)), absence.start_date - timedelta(days=1), section.meeting_days)
        pre_entries = {e.date: e for e in entries_between(session, section.id, pre_days[0].date if pre_days else absence.start_date, absence.start_date - timedelta(days=1))}
        flex_before = [d.date for d in pre_days if d.date in pre_entries and pre_entries[d.date].lesson_id is None]
        early_sensitive = [e for e in entries if e.lesson and "long_term_sub" not in (e.lesson.delivery_requirement or []) and e.date <= absence.start_date + timedelta(days=14)]
        pull_forward = [f"'{e.title}' ({e.date}) could move into the flex day on {flex_before[i]} before leave" for i, e in enumerate(early_sensitive[: len(flex_before)])]
        undetailed = [f"{e.date}: {e.title}" for e in entries if e.lesson_id is None and e.kind.value == "placeholder"]
        out["sections"].append({
            "section": section.section_name, "section_id": section.id, "course_days_during_leave": len(entries), "units": units,
            "undetailed_days": undetailed,
            "assessments": assessments, "sensitive_dependencies": sensitive, "complete_before_leave": pull_forward,
            "last_day_before_leave": max(pre_entries).isoformat() if pre_entries else None,
        })
    plan = _ensure_plan(session, absence)
    plan.pre_leave = out
    return out


def handoff_days(session: Session, absence: AbsenceEvent, section: TeacherCourse, n: int = HANDOFF_DAYS) -> list[CalendarEntry]:
    entries = [e for e in entries_between(session, section.id, absence.start_date, absence.end_date) if e.status == EntryStatus.planned]
    return entries[:n]


def build_handoff(session: Session, absence: AbsenceEvent, sub_plan_ids_by_entry: dict[int, int] | None = None) -> dict:
    """Handoff packet metadata: first 10 instructional days (plans generated separately), rules, unit context, assessment guidance, boundaries."""
    teacher = absence.teacher
    packet = {"start": absence.start_date.isoformat(), "detailed_days": HANDOFF_DAYS, "sections": [], "classroom_rules": [r.text for r in resolve_rules(session, teacher.id)], "flexibility_boundaries": [
        "Keep assessments on their scheduled dates unless the reconciliation proposal moved them.",
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
        packet["sections"].append({
            "section": section.section_name, "section_id": section.id, "period": section.period, "minutes": section.minutes_per_meeting,
            "days": [{"date": e.date.isoformat(), "title": e.title, "kind": e.kind.value, "entry_id": e.id, "sub_plan_id": (sub_plan_ids_by_entry or {}).get(e.id)} for e in days],
            "unit_context": units,
            "assessment_guidance": [f"{e.date}: {e.title} — administer as scheduled; collect and leave for the teacher unless rules say otherwise." for e in days + later if e.lesson and e.lesson.is_assessment],
        })
    plan = _ensure_plan(session, absence)
    plan.handoff = packet
    return packet


def weekly_frameworks(session: Session, absence: AbsenceEvent) -> list[WeeklyFramework]:
    """After the first 10 detailed days: a weekly pacing framework per section for the rest of the leave."""
    frameworks: list[WeeklyFramework] = []
    ys = load_year_structure(session, absence.teacher.school.district_slug, absence.teacher.school_year)
    for section in absence.teacher.sections:
        detailed = handoff_days(session, absence, section)
        start = (detailed[-1].date + timedelta(days=1)) if detailed else absence.start_date
        entries = [e for e in entries_between(session, section.id, start, absence.end_date)]
        by_week: dict[date, list[CalendarEntry]] = defaultdict(list)
        for e in entries:
            by_week[e.date - timedelta(days=e.date.isoweekday() - 1)].append(e)
        for wk in sorted(by_week):
            es = by_week[wk]
            units, objectives, standards, materials = [], [], [], []
            for e in es:
                if e.unit and e.unit.title not in units:
                    units.append(e.unit.title)
                if e.lesson:
                    objectives.append(f"{e.date:%a}: {e.lesson.objective or e.title}")
                    for s in e.lesson.standards or []:
                        if s not in standards:
                            standards.append(s)
                    for m in e.lesson.materials or []:
                        if m not in materials:
                            materials.append(m)
                else:
                    objectives.append(f"{e.date:%a}: {e.title}")
            deadlines = [f"Quarter {q} ends {qe}" for q, qe in ys.quarter_ends.items() if wk <= qe <= wk + timedelta(days=6)]
            constraints = [r.text for r in resolve_rules(session, absence.teacher_id, section=section)][:4]
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
    """What was completed, moved, skipped; assessment status; outstanding work; re-entry point; first week back."""
    sections: list[ReturnBriefSection] = []
    for section in absence.teacher.sections:
        entries = entries_between(session, section.id, absence.start_date, absence.end_date)
        completed = [f"{e.date}: {e.title}" for e in entries if e.status == EntryStatus.completed]
        skipped = [f"{e.date}: {e.title}" for e in entries if e.status == EntryStatus.skipped]
        moved_rows = session.scalars(select(CalendarChange).where(CalendarChange.teacher_course_id == section.id, CalendarChange.date >= absence.start_date, CalendarChange.change_type.in_(["shift", "merge", "compress", "defer", "replace", "drop"])).order_by(CalendarChange.date))
        moved = [f"{c.date}: {c.change_type} — {c.before.get('title') or c.before.get('lesson') or ''} → {c.after.get('title') or c.change_type}" for c in moved_rows]
        assessments = [f"{e.date}: {e.title} — {e.status.value}" for e in entries if e.lesson and e.lesson.is_assessment]
        outstanding = [f"{e.date}: {e.title} (not marked complete)" for e in entries if e.status == EntryStatus.planned and e.date <= date.today()]
        collect = [f"Collect: {', '.join(e.lesson.student_output)} from {e.date:%b %d}" for e in entries if e.lesson and e.lesson.student_output and e.status in (EntryStatus.completed, EntryStatus.planned)][-3:]
        nxt = next_entries(session, section.id, absence.end_date, 5)
        reentry = f"Resume with '{nxt[0].title}' on {nxt[0].date}" if nxt else "No planned entries after the absence"
        if skipped:
            reentry += f"; first address skipped: {skipped[0]}"
        sections.append(ReturnBriefSection(course=section.section_name, completed=completed, moved=moved, skipped=skipped, assessment_status=assessments, outstanding_work=outstanding + collect, recommended_reentry_point=reentry, first_week_back=[f"{e.date:%a %b %d}: {e.title}" for e in nxt]))
    changes = [f"{c.date}: {c.change_type} ({c.reason})" for c in session.scalars(select(CalendarChange).where(CalendarChange.teacher_id == absence.teacher_id, CalendarChange.date >= absence.start_date).order_by(CalendarChange.date))][:20]
    follow = []
    for s in sections:
        follow.extend(s.outstanding_work[:2])
        follow.append(s.recommended_reentry_point)
    brief = ReturnBrief(absence_id=absence.id, start_date=absence.start_date, end_date=absence.end_date, sections=sections, calendar_changes=changes, follow_up=follow)
    plan = _ensure_plan(session, absence)
    plan.return_brief = brief.model_dump(mode="json")
    return brief
