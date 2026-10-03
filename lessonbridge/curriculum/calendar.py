"""Instructional calendar: school days, quarters and curriculum -> date mapping.

The curriculum sequence (units and lessons) is stored separately from the
calendar mapping, so lessons can move without rewriting the curriculum.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    CalendarEntry,
    CalendarEventType,
    CurriculumUnit,
    DetailLevel,
    EntryKind,
    EntryStatus,
    Lesson,
    Priority,
    SchoolCalendarEvent,
    TeacherCourse,
)

DETAILED_HORIZON_DAYS = 21
MAX_FLEX_SHARE = 0.2  # at most this share of a unit's days is pure slack; the rest become unit-level placeholders


@dataclass
class SchoolDay:
    date: date
    quarter: int | None
    early_release: bool = False
    quarter_end: bool = False
    testing_window: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def weekday(self) -> int:
        return self.date.isoweekday()


@dataclass
class YearStructure:
    first_day: date
    last_day: date
    quarter_ends: dict[int, date]
    quarter_starts: dict[int, date]
    closed: set[date]
    early_release: set[date]
    testing: set[date]

    def quarter_for(self, d: date) -> int | None:
        for q in sorted(self.quarter_ends):
            if d <= self.quarter_ends[q]:
                return q
        return None

    def quarter_end_on_or_after(self, d: date) -> date | None:
        for q in sorted(self.quarter_ends):
            if self.quarter_ends[q] >= d:
                return self.quarter_ends[q]
        return None


def _expand(ev: SchoolCalendarEvent) -> Iterable[date]:
    end = ev.end_date or ev.date
    d = ev.date
    while d <= end:
        yield d
        d += timedelta(days=1)


def load_year_structure(session: Session, district_slug: str, school_year: str) -> YearStructure:
    events = list(session.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.district_slug == district_slug, SchoolCalendarEvent.school_year == school_year)))
    if not events:
        raise ValueError(f"No school calendar events loaded for {district_slug} {school_year}; run the public-source sync first.")
    closed: set[date] = set()
    early: set[date] = set()
    testing: set[date] = set()
    q_ends: dict[int, date] = {}
    q_starts: dict[int, date] = {}
    first = last = None
    for ev in events:
        if ev.event_type in (CalendarEventType.holiday, CalendarEventType.teacher_workday, CalendarEventType.no_school):
            closed.update(_expand(ev))
        elif ev.event_type == CalendarEventType.early_release:
            early.add(ev.date)
        elif ev.event_type == CalendarEventType.testing_window:
            testing.update(_expand(ev))
        elif ev.event_type == CalendarEventType.quarter_end and ev.quarter:
            q_ends[ev.quarter] = ev.date
        elif ev.event_type == CalendarEventType.quarter_start and ev.quarter:
            q_starts[ev.quarter] = ev.date
        elif ev.event_type == CalendarEventType.first_day:
            first = ev.date
        elif ev.event_type == CalendarEventType.last_day:
            last = ev.date
            early.add(ev.date)
    if first is None or last is None:
        raise ValueError("School calendar is missing first_day or last_day events.")
    q_starts.setdefault(1, first)
    return YearStructure(first, last, q_ends, q_starts, closed, early, testing)


def school_days(ys: YearStructure, start: date, end: date, meeting_days: Iterable[int] = (1, 2, 3, 4, 5)) -> list[SchoolDay]:
    meet = set(int(x) for x in meeting_days)
    out: list[SchoolDay] = []
    d = max(start, ys.first_day)
    end = min(end, ys.last_day)
    while d <= end:
        if d.isoweekday() in meet and d not in ys.closed:
            sd = SchoolDay(d, ys.quarter_for(d), early_release=d in ys.early_release, quarter_end=d in ys.quarter_ends.values(), testing_window=d in ys.testing)
            if sd.early_release:
                sd.notes.append("early release")
            if sd.quarter_end:
                sd.notes.append("quarter ends")
            out.append(sd)
        d += timedelta(days=1)
    return out


def section_days(session: Session, section: TeacherCourse, start: date, end: date) -> list[SchoolDay]:
    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    return school_days(ys, start, end, section.meeting_days)


# ------------------------------------------------------------- calendar build
@dataclass
class BuildReport:
    entries_created: int
    flex_days: int
    conflicts: list[str]
    quarter_overruns: list[str]


def _flex_title(unit: CurriculumUnit, n: int) -> str:
    return f"Flex / work day ({unit.title})"


def _interleave_flex(plan: list[tuple[str, Optional[Lesson]]], n_flex: int, kind: str = "flex") -> list[tuple[str, Optional[Lesson]]]:
    """Distribute slack days through a unit: one before the final assessment (flex only), the rest spread evenly."""
    if n_flex <= 0 or not plan:
        return plan
    lessons = list(plan)
    tail: list[tuple[str, Optional[Lesson]]] = []
    if kind == "flex" and lessons[-1][1] is not None and lessons[-1][1].is_assessment:
        tail = [("flex", None), lessons[-1]]
        lessons = lessons[:-1]
        n_flex -= 1
    if n_flex <= 0 or not lessons:
        return lessons + tail if tail else lessons + [(kind, None)] * max(0, n_flex)
    out: list[tuple[str, Optional[Lesson]]] = []
    gap = max(1, round(len(lessons) / (n_flex + 1)))
    remaining = n_flex
    for i, item in enumerate(lessons, start=1):
        out.append(item)
        if remaining and i % gap == 0 and i < len(lessons):
            out.append((kind, None))
            remaining -= 1
    out += [(kind, None)] * remaining
    return out + tail


def allocate_unit_slots(units: list[CurriculumUnit], days: list[SchoolDay]) -> dict[int, int]:
    """Decide how many course-days each unit gets.

    Units that declare a quarter share that quarter's school days: their planned
    days are scaled up (extra slack spread proportionally) or, when the quarter
    is too short, flex is removed first and the overrun is left for the report.
    Units without a quarter simply keep their planned days.
    """
    out: dict[int, int] = {}
    by_quarter: dict[int | None, list[CurriculumUnit]] = {}
    for u in units:
        by_quarter.setdefault(u.quarter, []).append(u)
    day_quarters = [d.quarter for d in days]
    cursor = 0
    for q in sorted(by_quarter, key=lambda x: (x is None, x or 0)):
        group = by_quarter[q]
        base = {u.id: max(u.planned_days, len(u.lessons)) for u in group}
        if q is None:
            out.update(base)
            continue
        available = sum(1 for i, dq in enumerate(day_quarters) if i >= cursor and dq == q)
        total = sum(base.values())
        if available <= 0:
            out.update(base)
            continue
        if total < available:
            extra = available - total
            weights = {uid: base[uid] for uid in base}
            wsum = sum(weights.values()) or 1
            shares = {uid: extra * weights[uid] / wsum for uid in base}
            floor = {uid: int(shares[uid]) for uid in base}
            leftover = extra - sum(floor.values())
            for uid in sorted(base, key=lambda k: shares[k] - floor[k], reverse=True)[:leftover]:
                floor[uid] += 1
            out.update({uid: base[uid] + floor[uid] for uid in base})
        else:
            # Shrink flex proportionally, never below the lesson count.
            over = total - available
            flex = {u.id: base[u.id] - len(u.lessons) for u in group}
            fsum = sum(flex.values())
            cut = {uid: (min(flex[uid], round(over * flex[uid] / fsum)) if fsum else 0) for uid in base}
            out.update({uid: base[uid] - cut[uid] for uid in base})
        cursor += sum(out[u.id] for u in group)
    return out


def build_section_calendar(session: Session, section: TeacherCourse, *, start: Optional[date] = None, today: Optional[date] = None, replace: bool = True) -> BuildReport:
    """Map the section's curriculum sequence onto its meeting days.

    Each unit gets ``planned_days`` days: its lessons in order followed by flex
    days (slack the reconciler can later absorb). Units are kept inside their
    quarter when one is declared; overruns are reported rather than silently
    pushed across a grading-period boundary.
    """
    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    start = start or ys.first_day
    today = today or date.today()
    days = school_days(ys, start, ys.last_day, section.meeting_days)
    if replace:
        for e in list(section.calendar):
            if e.date >= start and e.status == EntryStatus.planned:
                session.delete(e)
        session.flush()
        session.refresh(section)
    existing_dates = {e.date for e in section.calendar}
    days = [d for d in days if d.date not in existing_dates]

    conflicts: list[str] = []
    overruns: list[str] = []
    created = flex = 0
    cursor = 0
    units = sorted(section.units, key=lambda u: u.sequence)
    detailed_until = today + timedelta(days=DETAILED_HORIZON_DAYS)
    slots_by_unit = allocate_unit_slots(units, days)

    for unit in units:
        lessons = sorted(unit.lessons, key=lambda l: l.sequence)
        slots = slots_by_unit.get(unit.id, max(unit.planned_days, len(lessons)))
        surplus = max(0, slots - len(lessons))
        n_flex = min(surplus, max(1 if surplus else 0, round(slots * MAX_FLEX_SHARE)))
        n_placeholder = surplus - n_flex
        plan: list[tuple[str, Optional[Lesson]]] = [("lesson", l) for l in lessons]
        # Placeholders are unit-level "lesson to be detailed" days spread through the unit;
        # flex is spread too, with the last flex day before the final assessment so review has slack.
        plan = _interleave_flex(plan, n_placeholder, kind="placeholder")
        plan = _interleave_flex(plan, n_flex)

        for kind, lesson in plan:
            if cursor >= len(days):
                conflicts.append(f"Ran out of school days while placing {unit.title}" + (f" / {lesson.title}" if lesson else ""))
                break
            day = days[cursor]
            if unit.quarter and day.quarter and day.quarter > unit.quarter and lesson is not None:
                overruns.append(f"{unit.title}: '{lesson.title}' lands on {day.date} (Q{day.quarter}) after the planned Q{unit.quarter} boundary")
            if lesson is not None and lesson.hard_date and lesson.hard_date != day.date:
                conflicts.append(f"{lesson.title} has a hard date {lesson.hard_date} but the sequence reaches it on {day.date}")
            entry = CalendarEntry(
                teacher_course_id=section.id,
                date=day.date,
                lesson_id=lesson.id if lesson else None,
                unit_id=unit.id,
                kind=EntryKind.assessment if (lesson and lesson.is_assessment) else (EntryKind.review if (lesson and lesson.lesson_type.value == "review") else (EntryKind.flex if kind == "flex" else (EntryKind.placeholder if kind == "placeholder" else EntryKind.lesson))),
                title=lesson.title if lesson else (_flex_title(unit, 0) if kind == "flex" else f"{unit.title}: lesson to be detailed"),
                detail_level=DetailLevel.unit if kind == "placeholder" else (DetailLevel.detailed if day.date <= detailed_until else (DetailLevel.lesson if (day.quarter == ys.quarter_for(today)) else DetailLevel.unit)),
                notes="; ".join(day.notes),
                origin="generated",
            )
            session.add(entry)
            created += 1
            flex += kind in ("flex", "placeholder")
            cursor += 1
    session.flush()
    session.refresh(section)
    return BuildReport(created, flex, conflicts, overruns)


def entries_between(session: Session, section_id: int, start: date, end: date) -> list[CalendarEntry]:
    return list(session.scalars(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date >= start, CalendarEntry.date <= end).order_by(CalendarEntry.date)))


def entry_on(session: Session, section_id: int, d: date) -> CalendarEntry | None:
    return session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date == d))


def next_entries(session: Session, section_id: int, after: date, n: int = 5) -> list[CalendarEntry]:
    return list(session.scalars(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date > after, CalendarEntry.status == EntryStatus.planned).order_by(CalendarEntry.date).limit(n)))


def lesson_is_optional(lesson: Lesson | None) -> bool:
    return bool(lesson and lesson.priority in (Priority.optional, Priority.recommended))
