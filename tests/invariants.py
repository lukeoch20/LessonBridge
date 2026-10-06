"""Calendar invariants shared by the property-based and fuzz tests (audit recommendation 4)."""
from __future__ import annotations

from collections import Counter
from datetime import date

from sqlalchemy import select

from lessonbridge.absence.service import lessons_by_slug
from lessonbridge.absence.state import entry_state, state_keys, state_slugs
from lessonbridge.models import AbsenceEvent, AbsenceStatus, CalendarEntry, EntryStatus, OwedLesson, Proposal, ProposalStatus, TeacherCourse


def snapshot(section: TeacherCourse) -> dict[date, dict]:
    return {e.date: entry_state(e) for e in section.calendar}


def lesson_keys(states: dict[date, dict]) -> Counter:
    c: Counter = Counter()
    for st in states.values():
        for k in state_keys(st):
            c[k] += 1
    return c


def check_section(session, section: TeacherCourse, before: dict[date, dict], removable: set[str] = frozenset()) -> list[str]:
    """Problems after an approval, relative to the section's state ``before`` it."""
    session.refresh(section)
    after = snapshot(section)
    lessons = lessons_by_slug(section)
    owed = Counter(o.key for o in session.scalars(select(OwedLesson).where(OwedLesson.teacher_course_id == section.id, OwedLesson.resolved_at.is_(None))))
    problems = []
    kb, ka = lesson_keys(before), lesson_keys(after)
    for k in kb:
        if k in removable:
            continue
        if ka.get(k, 0) + owed.get(k, 0) < 1:
            problems.append(f"lost {k}")
    for k, n in ka.items():
        if n > max(kb.get(k, 0), 1):
            problems.append(f"duplicated {k}")
    first: dict[str, date] = {}
    for d in sorted(after):
        for s in state_slugs(after[d]):
            first.setdefault(s, d)
    for d, st in after.items():
        if st["status"] != "planned":
            continue  # recorded progress; the planner surfaces these as decisions instead
        here = set(state_slugs(st))
        for s in here:
            l = lessons.get(s)
            if l is None:
                continue
            for p in l.prerequisite_slugs - here:
                if p in first and first[p] > d:
                    problems.append(f"{s} on {d} before prerequisite {p} on {first[p]}")
    for d, st in before.items():
        if st["status"] != "planned" and after.get(d) != st:
            problems.append(f"fixed day {d} changed")
    approved = {p.absence_id for p in session.scalars(select(Proposal).where(Proposal.teacher_course_id == section.id, Proposal.status == ProposalStatus.approved, Proposal.kind == "absence_reconciliation"))}
    for aid in approved:
        ab = session.get(AbsenceEvent, aid)
        if ab is None or ab.status == AbsenceStatus.cancelled:
            continue
        for d, st in after.items():
            if ab.start_date <= d <= ab.end_date and st["status"] == "planned" and not st["is_sub_day"]:
                problems.append(f"absence day {d} is not a substitute day")
    return problems
