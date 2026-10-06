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
    SchoolCalendarEvent,
    TeacherCourse,
)

DETAILED_HORIZON_DAYS = 21


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

    @property
    def testing_start(self) -> date | None:
        return min(self.testing) if self.testing else None

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
    conflicts: list[str] = field(default_factory=list)
    quarter_overruns: list[str] = field(default_factory=list)
    early_starts: list[str] = field(default_factory=list)
    empty_days: list[str] = field(default_factory=list)
    sol_warnings: list[str] = field(default_factory=list)
    quarter_notes: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return self.conflicts + self.quarter_overruns + self.early_starts + self.empty_days + self.sol_warnings + self.quarter_notes


@dataclass
class UnitPlan:
    """A unit (or the rest of one) to lay out: its lessons still to place and its target length."""

    unit_id: Optional[int]
    slug: str
    title: str
    quarter: Optional[int]
    planned_days: int
    lessons: list[Lesson]

    @classmethod
    def from_unit(cls, unit: CurriculumUnit, lessons: Optional[list[Lesson]] = None, planned_days: Optional[int] = None) -> "UnitPlan":
        ls = sorted(unit.lessons, key=lambda l: l.sequence) if lessons is None else lessons
        return cls(unit.id, unit.slug, unit.title, unit.quarter, max(planned_days if planned_days is not None else unit.planned_days, len(ls)), ls)


@dataclass
class PlacedDay:
    date: date
    kind: str  # lesson | flex | placeholder | unassigned
    lesson: Optional[Lesson]
    unit_id: Optional[int]
    unit_title: str


MAX_FLEX_SHARE = 0.2  # at most this share of a unit's days is pure slack; the rest become "lesson to be detailed" days


def effective_quarters(units: list[UnitPlan]) -> tuple[list[int], list[str]]:
    """Quarters along the actual sequence: never decreasing (LB-13). Out-of-order labels are reported."""
    out: list[int] = []
    notes: list[str] = []
    cur = 1
    for u in units:
        q = u.quarter or cur
        if q < cur:
            notes.append(f"'{u.title}' is labelled Q{u.quarter} but comes after Q{cur} units in the sequence; it is planned in Q{cur}.")
            q = cur
        out.append(q)
        cur = q
    return out, notes


def allocate_unit_slots(units: list[UnitPlan], days: list[SchoolDay]) -> dict[int, int]:
    """Days per unit (keyed by position): each run of same-quarter units shares that quarter's remaining days."""
    eff, _ = effective_quarters(units)
    out: dict[int, int] = {}
    i = 0
    cursor = 0
    while i < len(units):
        q = eff[i]
        j = i
        while j < len(units) and eff[j] == q:
            j += 1
        group = list(range(i, j))
        base = {k: max(units[k].planned_days, len(units[k].lessons)) for k in group}
        # Skip days of earlier quarters that nothing was planned for.
        while cursor < len(days) and days[cursor].quarter is not None and days[cursor].quarter < q:
            cursor += 1
        available = 0
        k2 = cursor
        while k2 < len(days) and (days[k2].quarter is None or days[k2].quarter == q):
            available += 1
            k2 += 1
        total = sum(base.values())
        if available <= 0:
            out.update(base)
        elif total <= available:
            extra = available - total
            shares = {k: extra * base[k] / total for k in group} if total else {k: 0 for k in group}
            floor = {k: int(shares[k]) for k in group}
            for k in sorted(group, key=lambda k: shares[k] - floor[k], reverse=True)[: extra - sum(floor.values())]:
                floor[k] += 1
            out.update({k: base[k] + floor[k] for k in group})
        else:
            over = total - available
            surplus = {k: base[k] - len(units[k].lessons) for k in group}
            ssum = sum(surplus.values())
            cut = {k: (min(surplus[k], round(over * surplus[k] / ssum)) if ssum else 0) for k in group}
            out.update({k: base[k] - cut[k] for k in group})
        cursor += sum(out[k] for k in group)  # always advance, even when the quarter is short (LB-10)
        i = j
    return out


def _spread(n_items: int, k: int) -> list[int]:
    """How many slack days go after each of n_items items (k in total, evenly spread, several per gap allowed)."""
    gaps = [0] * max(n_items, 1)
    for j in range(k):
        pos = min(n_items - 1, max(0, int((j + 1) * n_items / (k + 1)) - 1)) if n_items else 0
        gaps[pos] += 1
    return gaps


def _unit_sequence(u: UnitPlan, slots: int) -> list[tuple[str, Optional[Lesson]]]:
    """Lessons with slack spread through the instruction; the final assessment stays last, after one flex day (LB-25)."""
    lessons = list(u.lessons)
    final = lessons.pop() if lessons and lessons[-1].is_assessment else None
    surplus = max(0, slots - len(u.lessons))
    n_flex = min(surplus, max(1 if surplus else 0, round(slots * MAX_FLEX_SHARE)))
    n_place = surplus - n_flex
    tail_flex = 1 if (final is not None and n_flex) else 0
    # Spread the remaining flex days among the placeholders.
    flex_mid = n_flex - tail_flex
    mix: list[str] = []
    fgaps = _spread(max(n_place, 1), flex_mid) if n_place else [flex_mid]
    for j in range(max(n_place, 1)):
        if j < n_place:
            mix.append("placeholder")
        mix += ["flex"] * fgaps[j]
    if not lessons:
        out: list[tuple[str, Optional[Lesson]]] = [(k, None) for k in mix]
    else:
        gaps = _spread(len(lessons), len(mix))
        out = []
        it = iter(mix)
        for l, g in zip(lessons, gaps):
            out.append(("lesson", l))
            out += [(next(it), None) for _ in range(g)]
    if final is not None:
        out += [("flex", None)] * tail_flex + [("lesson", final)]
    return out


def layout_units(units: list[UnitPlan], days: list[SchoolDay]) -> tuple[list[PlacedDay], list[Lesson], BuildReport]:
    """Pure layout of units onto days. Returns placed days, lessons that found no day, and a report."""
    report = BuildReport(0, 0)
    eff, notes = effective_quarters(units)
    report.quarter_notes += notes
    alloc = allocate_unit_slots(units, days)
    placed: list[PlacedDay] = []
    leftover: list[Lesson] = []
    cursor = 0
    for k, u in enumerate(units):
        q = eff[k]
        # Days of an earlier quarter with no unit stay unassigned and are reported (never filled by a later quarter's unit).
        skipped = []
        while cursor < len(days) and days[cursor].quarter is not None and days[cursor].quarter < q:
            skipped.append(days[cursor])
            placed.append(PlacedDay(days[cursor].date, "unassigned", None, None, ""))
            cursor += 1
        if skipped:
            report.empty_days.append(f"Q{skipped[0].quarter}: {len(skipped)} day(s) from {skipped[0].date} have no unit planned.")
        seq = _unit_sequence(u, alloc.get(k, len(u.lessons)))
        for kind, lesson in seq:
            if cursor >= len(days):
                if lesson is not None:
                    leftover.append(lesson)
                continue
            day = days[cursor]
            if lesson is not None and day.quarter and day.quarter > q:
                report.quarter_overruns.append(f"{u.title}: '{lesson.title}' lands on {day.date} (Q{day.quarter}) after its Q{q}")
            if lesson is not None and day.quarter and u.quarter and day.quarter < u.quarter:
                report.early_starts.append(f"{u.title}: '{lesson.title}' lands on {day.date} (Q{day.quarter}) before its Q{u.quarter}")
            placed.append(PlacedDay(day.date, kind, lesson, u.unit_id, u.title))
            cursor += 1
    if cursor < len(days):
        report.empty_days.append(f"{len(days) - cursor} school day(s) from {days[cursor].date} have no unit planned.")
        for day in days[cursor:]:
            placed.append(PlacedDay(day.date, "unassigned", None, None, ""))
    if leftover:
        report.conflicts.append("Ran out of school days for: " + ", ".join(l.title for l in leftover))
    report.flex_days = sum(1 for p in placed if p.kind != "lesson")
    return placed, leftover, report


SOL_TESTED = {"english", "civics"}


def sol_warnings(ys: YearStructure, subject: str, placed: list[PlacedDay]) -> list[str]:
    """Required instruction for an SOL-tested course placed on or after the testing window opens (LB-51)."""
    start = ys.testing_start
    if start is None or subject not in SOL_TESTED:
        return []
    late = [p for p in placed if p.lesson is not None and p.date >= start and p.lesson.lesson_type.value not in ("review",) and p.lesson.priority.value == "required"]
    if not late:
        return []
    names = ", ".join(sorted({p.unit_title for p in late}))
    return [f"{len(late)} required lesson day(s) fall on or after the SOL testing window opens ({start}): {names}. Confirm the school's test date and move SOL-tested content earlier if needed."]


def _placed_state(p: PlacedDay, status: str = "planned") -> dict:
    if p.lesson is not None:
        kind = "assessment" if p.lesson.is_assessment else ("review" if p.lesson.lesson_type.value == "review" else "lesson")
        return {"exists": True, "lesson_id": p.lesson.id, "lesson_slug": p.lesson.slug, "unit_id": p.unit_id, "kind": kind, "title": p.lesson.title, "merged_keys": [],
                "continuation": 0, "is_sub_day": False, "absence_id": None, "replacement_activity_id": None, "status": status}
    if p.kind == "unassigned":
        title, kind = "Unassigned day: no unit planned yet", "placeholder"
    elif p.kind == "flex":
        title, kind = f"Flex / work day ({p.unit_title})", "flex"
    else:
        title, kind = f"{p.unit_title}: lesson to be detailed", "placeholder"
    return {"exists": True, "lesson_id": None, "lesson_slug": None, "unit_id": p.unit_id, "kind": kind, "title": title, "merged_keys": [],
            "continuation": 0, "is_sub_day": False, "absence_id": None, "replacement_activity_id": None, "status": status}


def build_section_calendar(session: Session, section: TeacherCourse, *, start: Optional[date] = None, today: Optional[date] = None) -> BuildReport:
    """Initial calendar for a section with no entries yet. Later rebuilds go through ``propose_rebuild``."""
    if section.calendar:
        raise ValueError(f"{section.section_name} already has a calendar; use a rebuild proposal instead.")
    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    start = start or ys.first_day
    today = today or date.today()
    days = school_days(ys, start, ys.last_day, section.meeting_days)
    units = [UnitPlan.from_unit(u) for u in sorted(section.active_units, key=lambda u: u.sequence)]
    placed, leftover, report = layout_units(units, days)
    detailed_until = today + timedelta(days=DETAILED_HORIZON_DAYS)
    by_date = {d.date: d for d in days}
    for p in placed:
        st = _placed_state(p)
        day = by_date[p.date]
        session.add(CalendarEntry(
            teacher_course_id=section.id, date=p.date, lesson_id=st["lesson_id"], unit_id=st["unit_id"], kind=EntryKind(st["kind"]), title=st["title"],
            detail_level=DetailLevel.unit if p.lesson is None else (DetailLevel.detailed if p.date <= detailed_until else (DetailLevel.lesson if day.quarter == ys.quarter_for(today) else DetailLevel.unit)),
            notes="; ".join(day.notes), origin="generated",
        ))
    report.entries_created = len(placed)
    report.sol_warnings += sol_warnings(ys, section.course.subject.value, placed)
    session.flush()
    session.refresh(section)
    return report


def propose_rebuild(session: Session, section: TeacherCourse, *, start: Optional[date] = None):
    """Rebuild planned days from ``start`` as a proposal (LB-10, LB-43).

    Completed and skipped days, day-to-day substitute days and everything before
    ``start`` stay as they are. Only lessons not already on a kept day are laid
    out again, continuing the sequence after the last kept lesson, so finished
    units are not re-taught and the end of the year is not lost. Long-term
    substitute days are rebuilt like any day but stay substitute days. A kept
    substitute day whose lesson would now come before its prerequisite becomes
    independent work, and the lesson is laid out after its prerequisite.
    """
    from ..absence.decisions import choose_replacement, keywords
    from ..absence.service import WindowPlan, explain_rows, replacement_options, save_proposal, unit_context
    from ..absence.state import entry_state, same_state, state_slugs
    from ..models import SubstituteType
    from ..schemas import BacklogChange, DiffItem

    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    start = start or ys.first_day
    entries = {e.date: e for e in section.calendar}
    long_term = {a.id for a in section.teacher.absences if a.substitute_type == SubstituteType.long_term_sub}
    lessons = {l.slug: l for u in section.units for l in u.lessons}
    reps = replacement_options(session, section.teacher_id, section.course.subject.value)
    ctx = unit_context(section)
    converted: dict[date, dict] = {}  # kept substitute days turned into independent work

    def is_fixed(d: date, e: CalendarEntry) -> bool:
        if d < start or e.status != EntryStatus.planned or d in converted:
            return True
        return bool(e.is_sub_day and e.absence_id not in long_term)

    for _attempt in range(6):
        fixed = {d for d, e in entries.items() if is_fixed(d, e)}
        kept_states = {d: (converted[d] if d in converted else entry_state(entries[d])) for d in fixed}
        placed_slugs: set[str] = set()
        used_days: dict[int, int] = {}
        for d, st in kept_states.items():
            placed_slugs |= set(state_slugs(st))
            if st.get("unit_id") is not None:
                used_days[st["unit_id"]] = used_days.get(st["unit_id"], 0) + 1
        units: list[UnitPlan] = []
        for u in sorted(section.active_units, key=lambda u: u.sequence):
            remaining = [l for l in sorted(u.lessons, key=lambda l: l.sequence) if l.slug not in placed_slugs]
            if remaining:
                units.append(UnitPlan.from_unit(u, remaining, max(len(remaining), u.planned_days - used_days.get(u.id, 0))))
        days = [d for d in school_days(ys, start, ys.last_day, section.meeting_days) if d.date not in fixed]
        placed, leftover, report = layout_units(units, days)
        # Check prerequisite order against kept substitute days that hold lessons.
        first: dict[str, date] = {}
        for d, st in kept_states.items():
            for sl in state_slugs(st):
                first[sl] = min(first.get(sl, d), d)
        for p in placed:
            if p.lesson is not None:
                first[p.lesson.slug] = min(first.get(p.lesson.slug, p.date), p.date)
        def convertible(d: Optional[date]) -> bool:
            e = entries.get(d) if d else None
            return bool(e is not None and d >= start and e.status == EntryStatus.planned and e.is_sub_day and e.absence_id not in long_term and d not in converted and e.lesson_id)

        day_of: dict[str, date] = {}
        for d, st in kept_states.items():
            for sl in state_slugs(st):
                day_of.setdefault(sl, d)
        for p in placed:
            if p.lesson is not None:
                day_of.setdefault(p.lesson.slug, p.date)
        bad: set[date] = set()
        for sl, d in day_of.items():
            l = lessons.get(sl)
            if l is None:
                continue
            for pre in l.prerequisite_slugs:
                pd = first.get(pre)
                if pd is None or pd <= d:
                    continue
                # Either the dependent sits on a kept substitute day, or the prerequisite does: free that day.
                if convertible(d):
                    bad.add(d)
                elif convertible(pd):
                    bad.add(pd)
        if not bad:
            break
        for d in bad:
            e = entries[d]
            unit_slug = e.unit.slug if e.unit else ""
            rep = choose_replacement(reps, used=set(), context=keywords(ctx.get(unit_slug, ""), e.title))
            st = dict(entry_state(e))
            st.update({"lesson_id": None, "lesson_slug": None, "kind": "filler", "merged_keys": [], "continuation": 0,
                       "title": f"{rep.title if rep else 'Independent work'} [SUB]", "replacement_activity_id": rep.activity_id if rep else None})
            converted[d] = st

    report.sol_warnings += sol_warnings(ys, section.course.subject.value, placed)
    rows = []

    def row(d: date, before: dict, after: dict, reason: str) -> None:
        changed = not same_state(before, after)
        rows.append(DiffItem(section_id=section.id, date=d, action=("insert" if not before["exists"] else ("shift" if changed else "keep")), before=before, after=after,
                             changed=changed, reason=reason if changed else "", before_title=before.get("title"), after_title=after.get("title"),
                             before_lesson_slug=before.get("lesson_slug"), after_lesson_slug=after.get("lesson_slug"), after_kind=after.get("kind") or "flex",
                             is_sub_day=bool(after.get("is_sub_day"))))

    for d, st in converted.items():
        row(d, entry_state(entries[d]), st, "its lesson would now come before a prerequisite, so the substitute gives independent work and the lesson moves later")
    for p in placed:
        e = entries.get(p.date)
        before = entry_state(e)
        after = _placed_state(p, before["status"])
        if e is not None and e.is_sub_day and e.absence_id in long_term:
            after["is_sub_day"], after["absence_id"] = True, e.absence_id
        row(p.date, before, after, "rebuilt from the curriculum sequence")
    rows.sort(key=lambda r: r.date)
    adds = [BacklogChange(lesson_id=l.id, key=l.slug, title=l.title, kind="owed", reason="no school day left in the year", is_assessment=l.is_assessment).model_dump() for l in leftover]
    rebuilt_slugs = {p.lesson.slug for p in placed if p.lesson is not None}
    resolve = [o.id for o in section.owed if o.open and (o.key.split("#", 1)[0] in rebuilt_slugs or o.key.split("#", 1)[0] in placed_slugs)]
    plan = WindowPlan(rows, {"add": adds, "resolve": resolve}, [], report.problems, [], [f"{a['title']} has no school day left and goes to the owed-lesson list." for a in adds if a["is_assessment"]],
                      rows[0].date if rows else start, rows[-1].date if rows else start)
    header = [f"{section.section_name}: rebuild planned days from {start} ({len(units)} unit(s) with lessons still to teach; completed, skipped and day-to-day substitute days stay as they are)."]
    header += [f"Note: {p}" for p in report.problems]
    return save_proposal(session, section, plan, kind="rebuild", explanation=explain_rows(section, plan, header)), report


def entries_between(session: Session, section_id: int, start: date, end: date) -> list[CalendarEntry]:
    return list(session.scalars(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date >= start, CalendarEntry.date <= end).order_by(CalendarEntry.date)))


def entry_on(session: Session, section_id: int, d: date) -> CalendarEntry | None:
    return session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date == d))


def next_entries(session: Session, section_id: int, after: date, n: int = 5) -> list[CalendarEntry]:
    return list(session.scalars(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section_id, CalendarEntry.date > after, CalendarEntry.status == EntryStatus.planned).order_by(CalendarEntry.date).limit(n)))
