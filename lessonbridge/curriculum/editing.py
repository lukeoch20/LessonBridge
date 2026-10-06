"""Teacher edits: calendar days, placeholder days, pending proposals, and documents added after onboarding (LB-33).

Direct edits change one day's content without moving anything else, so they
are applied at once with an audit row; anything that moves lessons goes
through a proposal.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..absence.service import PlanningError, ProposalNotPending, lessons_by_slug, mark_stale_plans, next_school_day, year
from ..absence.state import entry_state, same_state
from ..models import (
    CalendarChange,
    CalendarEntry,
    DocumentType,
    EntryKind,
    EntryStatus,
    Lesson,
    LessonDependency,
    LessonType,
    Priority,
    Proposal,
    ProposalStatus,
    ReplacementActivity,
    Teacher,
    TeacherCourse,
)
from ..schemas import DiffItem


def _entry(session: Session, section: TeacherCourse, d: date) -> CalendarEntry:
    e = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == d))
    if e is None:
        raise PlanningError(f"{section.section_name} has no calendar day on {d}.")
    return e


def _audit(session: Session, section: TeacherCourse, e: CalendarEntry, before: dict, reason: str) -> None:
    session.flush()
    session.refresh(e)
    session.add(CalendarChange(teacher_id=section.teacher_id, teacher_course_id=section.id, change_type="edit", date=e.date, before=before, after=entry_state(e), reason=reason))
    mark_stale_plans(session, section, [e.date])


def edit_entry(session: Session, section: TeacherCourse, d: date, *, title: Optional[str] = None, notes: Optional[str] = None, lesson_slug: Optional[str] = None) -> CalendarEntry:
    """Retitle a day, add notes, or put a lesson that is not yet on the calendar on it."""
    e = _entry(session, section, d)
    before = entry_state(e)
    if lesson_slug:
        lesson = lessons_by_slug(section).get(lesson_slug)
        if lesson is None:
            raise PlanningError(f"No lesson {lesson_slug!r} in {section.section_name}'s curriculum.")
        elsewhere = [x.date for x in section.calendar if x.lesson_id == lesson.id and x.date != d and not x.continuation]
        if elsewhere:
            raise PlanningError(f"'{lesson.title}' is already on {elsewhere[0]}; use a rebuild or slip to move lessons so nothing is scheduled twice.")
        if e.lesson_id is not None and e.lesson_id != lesson.id:
            raise PlanningError(f"{d} already holds '{e.title}'; only flex or 'to be detailed' days can take another lesson directly.")
        e.lesson_id, e.unit_id, e.title = lesson.id, lesson.unit_id, lesson.title
        e.kind = EntryKind.assessment if lesson.is_assessment else (EntryKind.review if lesson.lesson_type == LessonType.review else EntryKind.lesson)
    if title:
        e.title = title.strip()
    if notes is not None:
        e.notes = notes.strip()
    _audit(session, section, e, before, "teacher edit")
    return e


def fill_placeholder(session: Session, section: TeacherCourse, d: date, *, title: str, objective: str, lesson_type: str = "guided_practice",
                     materials: list[str] | None = None, outputs: list[str] | None = None, delivery: list[str] | None = None) -> Lesson:
    """Give a 'lesson to be detailed' or flex day a real lesson the teacher describes."""
    e = _entry(session, section, d)
    if e.lesson_id is not None:
        raise PlanningError(f"{d} already holds '{e.title}'.")
    if e.status != EntryStatus.planned:
        raise PlanningError(f"{d} is marked {e.status.value}.")
    unit = e.unit or next((u for u in section.active_units), None)
    if unit is None:
        raise PlanningError("This section has no curriculum units to add a lesson to.")
    try:
        lt = LessonType(lesson_type)
    except ValueError as exc:
        raise PlanningError(f"Unknown lesson type {lesson_type!r}.") from exc
    ordered = sorted(unit.lessons, key=lambda l: l.sequence)
    prior = [x.lesson for x in sorted(section.calendar, key=lambda x: x.date) if x.date < d and x.lesson and x.lesson.unit_id == unit.id]
    prev = prior[-1] if prior else None
    base = f"{unit.slug}-teacher"
    taken = {l.slug for u in section.units for l in u.lessons}
    k = 1
    while f"{base}-{k}" in taken:
        k += 1
    lesson = Lesson(unit_id=unit.id, sequence=0, slug=f"{base}-{k}", title=title.strip(), objective=objective.strip(), lesson_type=lt,
                    duration_minutes=section.minutes_per_meeting, minimum_viable_minutes=min(30, section.minutes_per_meeting), priority=Priority.required,
                    delivery_requirement=delivery or ["regular_teacher", "long_term_sub"], materials=materials or [], student_output=outputs or ["completed class work"],
                    standards=list(unit.standards or []), origin="teacher")
    session.add(lesson)
    session.flush()
    idx = ordered.index(prev) + 1 if prev in ordered else len(ordered)
    for i, l in enumerate(ordered[:idx] + [lesson] + ordered[idx:], start=1):
        l.sequence = i
    if prev is not None:
        session.add(LessonDependency(lesson_id=lesson.id, depends_on_lesson_id=prev.id, kind="before"))
    before = entry_state(e)
    e.lesson_id, e.unit_id, e.title = lesson.id, unit.id, lesson.title
    e.kind = EntryKind.assessment if lt == LessonType.assessment else EntryKind.lesson
    _audit(session, section, e, before, "teacher filled a placeholder day")
    return lesson


def set_proposal_replacement(session: Session, proposal: Proposal, d: date, activity_slug: str) -> Proposal:
    """Choose a different replacement activity for one substitute day in a pending proposal."""
    if proposal.status != ProposalStatus.pending:
        raise ProposalNotPending(f"Proposal {proposal.id} is {proposal.status.value}; only pending proposals can be edited.")
    section = session.get(TeacherCourse, proposal.teacher_course_id)
    act = session.scalar(select(ReplacementActivity).where(ReplacementActivity.slug == activity_slug, ReplacementActivity.subject == section.course.subject,
                                                           (ReplacementActivity.teacher_id == section.teacher_id) | (ReplacementActivity.teacher_id.is_(None))))
    if act is None:
        raise PlanningError(f"No replacement activity {activity_slug!r} for {section.course.subject.value}.")
    rows = list(proposal.diff)
    for i, raw in enumerate(rows):
        r = DiffItem(**raw)
        if r.date != d:
            continue
        if not (r.after and r.after.get("kind") == "filler"):
            raise PlanningError(f"{d} is not a replacement-activity day in this proposal.")
        r.after = {**r.after, "replacement_activity_id": act.id, "title": f"{act.title} [SUB]"}
        r.after_title = r.after["title"]
        r.changed = not same_state(r.before, r.after)
        r.reason = (r.reason + " " if r.reason else "") + "(activity chosen by the teacher)"
        rows[i] = r.model_dump(mode="json")
        proposal.diff = rows
        return proposal
    raise PlanningError(f"Proposal {proposal.id} has no row for {d}.")


def interpret_upload(session: Session, teacher: Teacher, text: str, doc_type: DocumentType, *, subject: Optional[str], llm=None, label: str = "upload", today: Optional[date] = None) -> dict:
    """Read a document added after onboarding: rules are stored, a new curriculum arrives as rebuild proposals."""
    from ..curriculum.calendar import propose_rebuild
    from ..curriculum.interpret import load_curriculum, merge_curriculum
    from ..profile.onboarding import _signature_of_section, _signature_of_spec, interpret_document

    subjects = sorted({s.course.subject.value for s in teacher.sections})
    found = interpret_document(session, teacher, text, doc_type, subject=subject, subjects=subjects, llm=llm, label=label)
    proposals: list[int] = []
    notes: list[str] = list(found["warnings"]) + list(found["errors"])
    for subj, items in found["curricula"].items():
        chosen = next(((c, m, t) for c, m, t in items if c.units), None)
        if chosen is None:
            continue
        merge = merge_curriculum(subj, chosen[0])
        notes += merge.conflicts + merge.notes
        for sec in teacher.sections:
            if sec.course.subject.value != subj or _signature_of_spec(merge.spec) == _signature_of_section(sec):
                continue
            load_curriculum(session, sec, merge.spec)
            prop, rep = propose_rebuild(session, sec, start=next_school_day(year(session, sec), today or date.today()))
            proposals.append(prop.id)
            notes.append(f"{sec.section_name}: the new curriculum is waiting as proposal {prop.id}.")
    return {"rules": found["rules"], "proposals": proposals, "notes": notes}
