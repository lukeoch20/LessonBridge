"""Database adapter for the planning core.

Planning reads the calendar's stored state (substitute days, merged lessons,
continuation days, progress) instead of re-deriving it from titles, repairs one
quarter segment at a time, carries anything that does not fit into the next
quarter and, only as a last resort, into the owed-lesson backlog. Every
proposal passes an invariant gate before it is saved and a stale check before
it is applied. Nothing is ever deleted from the calendar silently.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..curriculum.calendar import SchoolDay, YearStructure, load_year_structure, school_days
from ..models import (
    AbsenceDecision,
    AbsenceEvent,
    AbsenceStatus,
    AbsenceType,
    CalendarChange,
    CalendarEntry,
    Decision,
    EntryKind,
    EntryStatus,
    Lesson,
    OwedLesson,
    Proposal,
    ProposalStatus,
    ReplacementActivity,
    SubPlan,
    SubPlanStatus,
    SubstituteType,
    Teacher,
    TeacherCourse,
    utcnow,
)
from ..profile.rules import get_preferences, resolve_rules, rule_minutes
from ..schemas import BacklogChange, DiffItem
from .decisions import DayDecision, ReplacementOption, decide_days, keywords
from .engine import Item, Slot, prerequisite_violations, repair
from .state import entry_state, fingerprint, same_state, state_keys, state_slugs, write_state


# ------------------------------------------------------------------- errors
class PlanningError(ValueError):
    """A planning request that cannot be carried out as asked."""


class ProposalNotPending(PlanningError):
    pass


class StaleProposalError(PlanningError):
    pass


class DecisionRequired(PlanningError):
    pass


class BlockedProposal(PlanningError):
    pass


# ------------------------------------------------------------------ helpers
def lessons_by_slug(section: TeacherCourse) -> dict[str, Lesson]:
    out: dict[str, Lesson] = {}
    for u in sorted(section.units, key=lambda u: (u.active, u.sequence)):  # active units win on slug collisions
        for l in u.lessons:
            out[l.slug] = l
    return out


def year(session: Session, section: TeacherCourse) -> YearStructure:
    return load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)


def next_school_day(ys: YearStructure, d: date) -> date:
    """First day students attend strictly after ``d`` (LB-52)."""
    cur = d + timedelta(days=1)
    while cur <= ys.last_day + timedelta(days=120):
        if cur.isoweekday() <= 5 and cur not in ys.closed:
            return cur
        cur += timedelta(days=1)
    return d + timedelta(days=1)


def split_key(key: str) -> tuple[str, int]:
    if "#c" in key:
        slug, n = key.split("#c", 1)
        return slug, int(n or 0)
    return key, 0


def lesson_item(lesson: Lesson, *, key: Optional[str] = None, title: Optional[str] = None, seq: float = 0, continuation: int = 0) -> Item:
    kind = "assessment" if lesson.is_assessment else ("review" if lesson.lesson_type.value == "review" else "lesson")
    if continuation:
        kind = "lesson"
    return Item(
        key=key or (lesson.slug + (f"#c{continuation}" if continuation else "")),
        title=title or (lesson.title + (" (continued)" if continuation else "")),
        kind=kind,
        lesson_id=lesson.id,
        lesson_slug=lesson.slug,
        lesson_type=lesson.lesson_type.value if not continuation else "guided_practice",
        priority=lesson.priority.value,
        duration=lesson.duration_minutes,
        min_minutes=lesson.minimum_viable_minutes,
        can_move=lesson.can_move,
        quarter_boundary_allowed=lesson.quarter_boundary_allowed,
        hard_date=lesson.hard_date if not continuation else None,
        prereqs=set(lesson.prerequisite_slugs) | ({lesson.slug} if continuation else set()),
        delivery=set(lesson.delivery_requirement or []),
        unit=lesson.unit.slug,
        unit_id=lesson.unit_id,
        continuation=continuation,
        sequence=seq,
        required_components=list(lesson.required_components or []),
        optional_components=list(lesson.optional_components or []),
    )


def item_from_entry(entry: CalendarEntry, seq: float, lessons: dict[str, Lesson]) -> Item:
    """Rebuild a planner item from what the calendar actually stores (LB-02, LB-08, LB-21, LB-22)."""
    unit_slug = entry.unit.slug if entry.unit else ""
    if entry.lesson is None:
        if entry.is_sub_day and entry.kind not in (EntryKind.flex, EntryKind.placeholder):
            return Item(key=f"sub-{entry.id}", title=entry.title, kind="filler", unit=unit_slug, unit_id=entry.unit_id, is_sub_day=True,
                        absence_id=entry.absence_id, replacement_id=entry.replacement_activity_id, sequence=seq, origin_date=entry.date,
                        priority="required", min_minutes=0, duration=0, payload={"existing": True})
        kind = "placeholder" if entry.kind == EntryKind.placeholder else "flex"
        return Item(key=f"{kind}-{entry.id}", title=entry.title, kind=kind, sequence=seq, unit=unit_slug, unit_id=entry.unit_id,
                    priority="optional", min_minutes=0, duration=0, origin_date=entry.date, is_sub_day=bool(entry.is_sub_day), absence_id=entry.absence_id)
    from .state import entry_merged_keys

    cont = int(entry.continuation or 0)
    it = lesson_item(entry.lesson, title=entry.title, seq=seq, continuation=cont)
    merged = entry_merged_keys(entry)
    if merged:
        it.merged = merged
        extra = 0
        for k in merged:
            l = lessons.get(split_key(k)[0])
            if l is not None:
                extra += l.minimum_viable_minutes
                it.prereqs |= l.prerequisite_slugs
        it.prereqs -= set(it.slugs)
        it.min_minutes += extra
    it.is_sub_day = bool(entry.is_sub_day)
    it.absence_id = entry.absence_id
    it.origin_date = entry.date
    return it


def filler_item(d: date, rep: Optional[ReplacementOption], *, absence_id: Optional[int], unit: str, unit_id: Optional[int], rationale: str, decision: str, displaced: Optional[str] = None) -> Item:
    title = f"{rep.title if rep else 'Independent work'} [SUB]"
    return Item(key=f"sub-new-{d.isoformat()}", title=title, kind="filler", pinned=d, is_sub_day=True, absence_id=absence_id,
                replacement_id=rep.activity_id if rep else None, unit=unit, unit_id=unit_id, sequence=-1, priority="required",
                min_minutes=0, duration=0, payload={"decision": decision, "rationale": rationale, "displaced": displaced})


def replacement_options(session: Session, teacher_id: int, subject: str) -> list[ReplacementOption]:
    rows = list(session.scalars(select(ReplacementActivity).where(ReplacementActivity.subject == subject, (ReplacementActivity.teacher_id == teacher_id) | (ReplacementActivity.teacher_id.is_(None))).order_by(ReplacementActivity.id)))
    own = [r for r in rows if r.teacher_id == teacher_id]
    rest = [r for r in rows if r.teacher_id != teacher_id]
    return [ReplacementOption(r.slug, r.title, r.description, r.duration_minutes, r.category, list(r.materials or []), r.student_output, list(r.tags or []), r.id) for r in own + rest]


def unit_context(section: TeacherCourse) -> dict[str, str]:
    return {u.slug: f"{u.title} {u.summary}" for u in section.units}


# ------------------------------------------------------------ the window
@dataclass
class Segment:
    start: date
    end: date
    label: str
    required: bool
    days: list[SchoolDay]


def segments_for(ys: YearStructure, section: TeacherCourse, start: date, through: date) -> list[Segment]:
    """Quarter segments from ``start`` through the quarter containing ``through``, plus one overflow quarter (LB-05)."""
    through = max(start, through)
    last_required = ys.quarter_end_on_or_after(through) or ys.last_day
    segs: list[Segment] = []
    cur = start
    overflow_added = False
    while cur <= ys.last_day:
        qe = ys.quarter_end_on_or_after(cur) or ys.last_day
        q = ys.quarter_for(cur) or ys.quarter_for(qe)
        required = qe <= last_required
        if not required:
            if overflow_added:
                break
            overflow_added = True
        label = f"Q{q} end" if q else "end of year"
        segs.append(Segment(cur, qe, label, required, school_days(ys, cur, qe, section.meeting_days)))
        cur = qe + timedelta(days=1)
    return segs


@dataclass
class WindowPlan:
    rows: list[DiffItem]
    backlog: dict
    hard: list[str]
    soft: list[str]
    unresolved: list[str]
    decisions_required: list[str]
    window_start: date
    window_end: date
    absorbed: int = 0
    merged: list[str] = field(default_factory=list)


def _state_from_item(it: Optional[Item], entry: Optional[CalendarEntry], lessons: dict[str, Lesson], unit_fallback: Optional[int], unit_titles: dict[int, str], sub_absence_id: Optional[int] = None) -> dict:
    st = _state_from_item_inner(it, entry, lessons, unit_fallback, unit_titles)
    if sub_absence_id is not None and st.get("kind") != "filler":
        st["is_sub_day"], st["absence_id"] = True, sub_absence_id
    return st


def _state_from_item_inner(it: Optional[Item], entry: Optional[CalendarEntry], lessons: dict[str, Lesson], unit_fallback: Optional[int], unit_titles: dict[int, str]) -> dict:
    base = entry_state(entry)
    status = base["status"]
    if it is None:
        unit_id = unit_fallback if unit_fallback is not None else base.get("unit_id")
        title = f"Flex / work day ({unit_titles[unit_id]})" if unit_id in unit_titles else "Flex / work day"
        return {"exists": True, "lesson_id": None, "lesson_slug": None, "unit_id": unit_id, "kind": "flex", "title": title, "merged_keys": [],
                "continuation": 0, "is_sub_day": False, "absence_id": None, "replacement_activity_id": None, "status": status}
    if it.lesson_slug:
        kind = it.kind if it.kind in ("lesson", "review", "assessment") else "lesson"
        return {"exists": True, "lesson_id": it.lesson_id, "lesson_slug": it.lesson_slug, "unit_id": it.unit_id, "kind": kind, "title": it.title,
                "merged_keys": list(it.merged), "continuation": it.continuation, "is_sub_day": it.is_sub_day, "absence_id": it.absence_id if it.is_sub_day else None,
                "replacement_activity_id": None, "status": status}
    if it.kind == "filler":
        return {"exists": True, "lesson_id": None, "lesson_slug": None, "unit_id": it.unit_id if it.unit_id is not None else unit_fallback, "kind": "filler",
                "title": it.title, "merged_keys": [], "continuation": 0, "is_sub_day": True, "absence_id": it.absence_id,
                "replacement_activity_id": it.replacement_id, "status": status}
    # Slack day (flex / placeholder), possibly pinned as a long-term-substitute day; keeps its own unit (LB-49).
    return {"exists": True, "lesson_id": None, "lesson_slug": None, "unit_id": it.unit_id, "kind": it.kind, "title": it.title, "merged_keys": [],
            "continuation": 0, "is_sub_day": it.is_sub_day, "absence_id": it.absence_id if it.is_sub_day else None, "replacement_activity_id": None, "status": status}


def _action(before: dict, after: dict, it: Optional[Item]) -> str:
    if same_state(before, after):
        return "keep"
    if not before.get("exists"):
        return "insert"
    if after.get("is_sub_day") and after.get("kind") == "filler" and not (before.get("is_sub_day") and before.get("kind") == "filler"):
        return "replace"
    if after.get("is_sub_day") and not before.get("is_sub_day"):
        return "substitute"
    if before.get("is_sub_day") and not after.get("is_sub_day"):
        return "release"
    if after.get("merged_keys") and list(after.get("merged_keys")) != list(before.get("merged_keys") or []):
        return "merge"
    if state_keys(after) == state_keys(before) and after.get("kind") == before.get("kind"):
        return "update"
    return "shift"


def plan_window(
    session: Session,
    section: TeacherCourse,
    *,
    start: date,
    through: date,
    overrides: dict[date, Item] | None = None,
    inserted: list[Item] | None = None,
    owed: list[OwedLesson] | None = None,
    convert: Callable[[Item], Item] | None = None,
) -> WindowPlan:
    """Repair the section's calendar from ``start`` so it holds every owed item, quarter by quarter.

    ``overrides`` pins items on dates (substitute days, decisions); whatever the
    calendar held on an overridden date re-enters the pool unless it was slack.
    ``inserted`` adds extra items (continuation days). ``owed`` backlog rows are
    offered first. Items that do not fit the quarter carry into the next one; an
    assessment crossing a quarter needs an explicit teacher decision.
    """
    overrides = dict(overrides or {})
    inserted = list(inserted or [])
    owed = list(owed or [])
    ys = year(session, section)
    lessons = lessons_by_slug(section)
    entries = {e.date: e for e in section.calendar}
    prefs = get_preferences(session, section.teacher_id)
    long_term = {a.id for a in section.teacher.absences if a.substitute_type == SubstituteType.long_term_sub and a.status != AbsenceStatus.cancelled}
    study = rule_minutes(resolve_rules(session, section.teacher_id, section=section, is_assessment=True), "study_period")
    unit_titles = {u.id: u.title for u in section.units}
    segs = segments_for(ys, section, start, through)
    if not segs or not any(s.days for s in segs):
        raise PlanningError(f"{section.section_name} has no school days from {start}.")

    # Lessons taught before the window (or on fixed days inside it) satisfy prerequisites.
    fixed_dates: dict[str, date] = {}
    for d, e in entries.items():
        if d < start or e.status != EntryStatus.planned:
            for s in state_slugs(entry_state(e)):
                if s not in fixed_dates or d < fixed_dates[s]:
                    fixed_dates[s] = d

    owed_items = []
    for i, o in enumerate(owed):
        slug, cont = split_key(o.key)
        l = lessons.get(slug) or o.lesson
        it = lesson_item(l, key=o.key, seq=-1000 + i, continuation=cont)
        it.payload = {"owed_id": o.id}
        owed_items.append(it)

    for attempt in range(8):
        override_keys = {o.key for o in overrides.values()}
        carried: list[Item] = []
        processed: list[tuple[Segment, list[Slot], object]] = []
        hard: list[str] = []
        soft: list[str] = []
        unresolved: list[str] = []
        dropped: list[Item] = []
        absorbed = 0
        merged_titles: list[str] = []
        seq = 0.0
        for si, seg in enumerate(segs):
            if not seg.required and not carried:
                break
            is_final = (not seg.required) or si == len(segs) - 1
            slots: list[Slot] = []
            seg_items: list[Item] = []
            for day in seg.days:
                seq += 10
                e = entries.get(day.date)
                if e is not None and e.status != EntryStatus.planned:
                    continue  # completed / skipped days are fixed (LB-09, LB-20)
                if day.date in overrides:
                    o = replace(overrides[day.date], pinned=day.date, prereqs=set(overrides[day.date].prereqs), merged=list(overrides[day.date].merged), payload=dict(overrides[day.date].payload))
                    seg_items.append(o)
                    if e is not None:
                        orig = item_from_entry(e, seq, lessons)
                        # Displaced lessons re-enter the pool; displaced slack is consumed (LB-46).
                        if orig.lesson_slug and orig.key != o.key and orig.key not in override_keys and not orig.is_sub_day:
                            seg_items.append(orig)
                sub_absence = None
                if day.date not in overrides and e is not None:
                    it = item_from_entry(e, seq, lessons)
                    if it.is_sub_day and it.absence_id in long_term and it.kind != "filler":
                        # A long-term substitute teaches whatever lands on their day: the day stays free for repair
                        # (its flex days still absorb slips) and keeps its substitute flag.
                        sub_absence = it.absence_id
                        it.is_sub_day, it.absence_id = False, None
                        if it.key not in override_keys:
                            seg_items.append(it)
                    elif it.is_sub_day:
                        it.pinned = day.date  # a day-to-day substitute's day stays put (LB-02, LB-08)
                        seg_items.append(it)
                    elif it.key not in override_keys:  # a lesson pulled forward to an absence day frees its old day
                        seg_items.append(it)
                ex = entry_state(e)
                slots.append(Slot(day.date, day.quarter, day.early_release, section.minutes_on(day.early_release),
                                  existing_key=(state_keys(ex) or [None])[-1], existing_title=ex.get("title"), existing_kind=ex.get("kind") or "flex", sub_absence_id=sub_absence))
            if si == 0:
                seg_items += inserted + owed_items
            seg_items += carried
            if not slots:
                carried = [i for i in seg_items if not i.pinned]
                continue
            res = repair(slots, seg_items, minutes=section.minutes_per_meeting, study_minutes=study, boundary_label=seg.label, allow_defer=not is_final, soft_prefs=prefs)
            processed.append((seg, slots, res))
            hard += [h for h in res.hard_constraints if h not in hard]
            soft += res.soft_notes
            unresolved += res.unresolved
            dropped += res.dropped
            absorbed += len(res.absorbed)
            for a, b in res.merged_pairs:
                merged_titles.append(f"{a.title} + {b.title}")
            carried = list(res.deferred)
            if is_final:
                break
        leftover = carried

        # Prerequisite order across the whole window; a pinned substitute-day lesson whose prerequisite now
        # lands later is turned into independent work so the lesson can follow its prerequisite.
        assigned = [(s.date, it) for _, slots_, res in processed for s, it in res.assignments if it is not None]
        violations = prerequisite_violations(assigned, fixed_dates)
        convertible = [v for v in violations if v[1].is_sub_day and v[1].lesson_slug and v[1].pinned and convert is not None]
        if not convertible:
            break
        for d, it, _p, _pd in convertible:
            overrides[d] = convert(it)

    # Build rows.
    rows: list[DiffItem] = []
    unit_fallback: Optional[int] = None
    assigned_keys: set[str] = set()
    for seg, slots, res in processed:
        for slot, it in res.assignments:
            e = entries.get(slot.date)
            before = entry_state(e)
            after = _state_from_item(it, e, lessons, unit_fallback, unit_titles, slot.sub_absence_id)
            if after.get("unit_id") is not None:
                unit_fallback = after["unit_id"]
            assigned_keys |= set(state_keys(after))
            action = _action(before, after, it)
            reason = ""
            if it is not None and it.is_sub_day:
                reason = it.payload.get("rationale", "") or ("substitute day of another absence" if it.payload.get("existing") else "")
            elif action == "shift":
                if after.get("lesson_slug") is None:
                    reason = "slack day after reconciliation"
                elif it is not None and it.origin_date and ys.quarter_for(it.origin_date) != ys.quarter_for(slot.date):
                    action = "carry"
                    reason = f"does not fit before the {_seg_label(ys, it.origin_date)}; carried into the next quarter"
                else:
                    reason = "moved to keep the sequence intact"
            elif action == "merge":
                reason = "combined on their core components to recover a lost day"
            elif action == "insert":
                reason = "school day had no calendar entry"
            rows.append(DiffItem(
                section_id=section.id, date=slot.date, action=action, before=before, after=after, changed=action != "keep", reason=reason,
                before_title=before.get("title"), after_title=after.get("title"), before_lesson_slug=before.get("lesson_slug"), after_lesson_slug=after.get("lesson_slug"),
                after_kind=after.get("kind") or "flex", merged_lesson_slugs=[split_key(k)[0] for k in after.get("merged_keys") or []], is_sub_day=bool(after.get("is_sub_day")),
            ))

    adds: list[BacklogChange] = []
    for it in dropped:
        for k in it.keys:
            l = lessons.get(split_key(k)[0])
            if l is not None:
                adds.append(BacklogChange(lesson_id=l.id, key=k, title=l.title, kind="dropped", reason=f"{it.priority} content removed to make room", is_assessment=l.is_assessment))
    for it in leftover:
        for k in it.keys:
            l = lessons.get(split_key(k)[0])
            if l is not None and not it.payload.get("owed_id"):
                adds.append(BacklogChange(lesson_id=l.id, key=k, title=l.title, kind="owed", reason="no free day before the end of the planning window", is_assessment=l.is_assessment))
        unresolved.append(f"'{it.title}' has no free day in this window and goes to the owed-lesson list.")
    resolve = [o.id for o in owed if o.key in assigned_keys]

    decisions_required: list[str] = []
    for r in rows:
        if r.action == "carry" and r.after and r.after.get("kind") == "assessment":
            decisions_required.append(f"{r.after_title} moves into the next quarter ({r.date}); approve only if that is acceptable.")
    for a in adds:
        if a.is_assessment:
            decisions_required.append(f"{a.title} has no room and goes to the owed-lesson list.")

    window_start = rows[0].date if rows else start
    window_end = rows[-1].date if rows else start
    return WindowPlan(rows, {"add": [a.model_dump() for a in adds], "resolve": resolve}, hard, list(dict.fromkeys(soft)), unresolved, decisions_required, window_start, window_end, absorbed, merged_titles)


def _seg_label(ys: YearStructure, d: date) -> str:
    q = ys.quarter_for(d)
    return f"Q{q} end" if q else "end of year"


# -------------------------------------------------------- invariant gate
def check_invariants(session: Session, section: TeacherCourse, rows: list[DiffItem], backlog: dict, *, absence_ids: Iterable[int] = (), releasing: Iterable[int] = (), removable: Iterable[str] = ()) -> tuple[list[str], list[str]]:
    """Return (violations, decisions) introduced by applying ``rows`` (LB-01, LB-06; audit recommendation 4).

    Checked: every lesson day still exists exactly once or sits in the backlog;
    prerequisites come first; days of approved absences stay substitute days;
    completed and skipped days are untouched.
    """
    lessons = lessons_by_slug(section)
    before: dict[date, dict] = {e.date: entry_state(e) for e in section.calendar}
    after = dict(before)
    violations: list[str] = []
    notes: list[str] = []
    for r in rows:
        b = before.get(r.date)
        if b is not None and b.get("status") != "planned" and not same_state(b, r.after):
            violations.append(f"{r.date}: a {b.get('status')} day would be rewritten")
        after[r.date] = r.after

    def keys_of(states: dict[date, dict]) -> Counter:
        c: Counter = Counter()
        for st in states.values():
            for k in state_keys(st):
                c[k] += 1
        return c

    kb, ka = keys_of(before), keys_of(after)
    added_to_backlog = Counter(a["key"] for a in backlog.get("add", []))
    resolved = {o.key for o in session.scalars(select(OwedLesson).where(OwedLesson.id.in_(backlog.get("resolve", []) or [-1])))}
    reopened = Counter(o.key for o in session.scalars(select(OwedLesson).where(OwedLesson.id.in_(backlog.get("reopen", []) or [-1]))))
    removable = set(removable)
    for k, n in kb.items():
        if k in removable:
            continue  # an undo removes days its original proposal created (e.g. a slip's continuation day)
        if ka.get(k, 0) + added_to_backlog.get(k, 0) + reopened.get(k, 0) < min(n, 1):
            l = lessons.get(split_key(k)[0])
            violations.append(f"'{l.title if l else k}' would disappear from the calendar")
    for k, n in ka.items():
        if n > max(kb.get(k, 0), 1) and k not in resolved:
            l = lessons.get(split_key(k)[0])
            violations.append(f"'{l.title if l else k}' would be scheduled {n} times")

    def order_violations(states: dict[date, dict]) -> set[tuple[str, str]]:
        first: dict[str, date] = {}
        for d in sorted(states):
            for s in state_slugs(states[d]):
                first.setdefault(s, d)
        out = set()
        for d, st in states.items():
            here = set(state_slugs(st))
            for s in here:
                l = lessons.get(s)
                if l is None:
                    continue
                for p in l.prerequisite_slugs - here:
                    if p in first and first[p] > d:
                        out.add((s, p))
        return out

    fixed_slugs = {sl for d, st in after.items() if st and st.get("status") != "planned" for sl in state_slugs(st)}
    for s, p in sorted(order_violations(after) - order_violations(before)):
        if s in fixed_slugs:
            # The dependent lesson sits on a day already marked completed or skipped; that is a recorded fact,
            # so the teacher decides rather than the plan being blocked.
            notes.append(f"'{lessons[s].title}' is marked {('completed or skipped')} but its prerequisite '{lessons[p].title}' would now come later.")
            continue
        violations.append(f"'{lessons[s].title}' would come before its prerequisite '{lessons[p].title}'")

    own = set(absence_ids)
    approved = session.scalars(select(Proposal.absence_id).where(Proposal.teacher_course_id == section.id, Proposal.status == ProposalStatus.approved,
                                                                Proposal.kind == "absence_reconciliation", Proposal.absence_id.is_not(None))).all()
    releasing = set(releasing)
    touched = {r.date for r in rows}
    for aid in (own | {a for a in approved if a is not None}) - releasing:
        ab = session.get(AbsenceEvent, aid)
        if ab is None or ab.status == AbsenceStatus.cancelled:
            continue
        for d in sorted(touched):
            if not (ab.start_date <= d <= ab.end_date):
                continue
            st, bst = after.get(d), before.get(d)
            if not st or not st.get("exists") or st.get("status") != "planned" or st.get("is_sub_day"):
                continue
            # Only flag what this proposal breaks: an absence day that was (or is meant to become) a substitute day.
            if aid in own or (bst is not None and bst.get("is_sub_day")):
                violations.append(f"{d}: the teacher is absent but the day would not be a substitute day")
    return list(dict.fromkeys(violations)), list(dict.fromkeys(notes))


# ------------------------------------------------------------- proposals
def _supersede_overlapping(session: Session, section_id: int, start: date, end: date, *, except_id: Optional[int] = None) -> int:
    n = 0
    for p in session.scalars(select(Proposal).where(Proposal.teacher_course_id == section_id, Proposal.status == ProposalStatus.pending)):
        if p.id == except_id:
            continue
        ps, pe = p.window_start, p.window_end
        if ps is None or pe is None or (ps <= end and start <= pe):
            p.status = ProposalStatus.superseded
            p.decided_at = utcnow()
            n += 1
    return n


def explain_rows(section: TeacherCourse, plan: WindowPlan, header: list[str]) -> str:
    """Explanation generated from what the diff actually does (LB-04)."""
    rows = plan.rows
    by_action = Counter(r.action for r in rows)
    lines = list(header)
    shifted = [r for r in rows if r.action == "shift" and r.after_lesson_slug]
    if shifted:
        last = max(r.date for r in rows if r.action in ("shift", "carry"))
        lines.append(f"Moved {len(shifted)} lesson day(s) later to keep the sequence in order; the cascade ends {last:%a %b %d}, where a slack day absorbed it." if plan.absorbed else f"Moved {len(shifted)} lesson day(s) later to keep the sequence in order.")
    merges = [r for r in rows if r.action == "merge"]
    if merges:
        lines.append("Compressed (core components kept, optional parts skipped): " + "; ".join(f"{r.after_title} on {r.date:%a %b %d}" for r in merges))
    carried = [r for r in rows if r.action == "carry"]
    if carried:
        lines.append("Carried into the next quarter: " + "; ".join(f"{r.after_title} on {r.date:%b %d}" for r in carried))
    adds = plan.backlog.get("add", [])
    dropped = [a for a in adds if a["kind"] == "dropped"]
    owed = [a for a in adds if a["kind"] == "owed"]
    if dropped:
        lines.append("Dropped to make room, kept on the owed-lesson list: " + ", ".join(a["title"] for a in dropped))
    if owed:
        lines.append("No room in this window, kept on the owed-lesson list: " + ", ".join(a["title"] for a in owed))
    if plan.backlog.get("resolve"):
        lines.append(f"Schedules {len(plan.backlog['resolve'])} lesson(s) from the owed-lesson list.")
    released = by_action.get("release", 0)
    if released:
        lines.append(f"Returns {released} substitute day(s) to regular teaching.")
    if not (shifted or merges or carried or adds or by_action.get("replace") or by_action.get("substitute") or released or by_action.get("insert")):
        lines.append("No other calendar days change.")
    lines.append("No lesson is removed from the calendar: every lesson is still scheduled or listed as owed.")
    if plan.decisions_required:
        lines.append("Needs your decision: " + " ".join(plan.decisions_required))
    if plan.unresolved:
        lines.append("Unresolved: " + " ".join(plan.unresolved))
    return "\n".join(lines)


def save_proposal(session: Session, section: TeacherCourse, plan: WindowPlan, *, kind: str, explanation: str, absence: Optional[AbsenceEvent] = None, reverts: Optional[int] = None) -> Proposal:
    absence_ids = [absence.id] if absence is not None and kind == "absence_reconciliation" else []
    releasing = [absence.id] if absence is not None and kind in ("cancel_absence", "undo") else []
    removable: set[str] = set()
    if kind == "undo" and reverts:
        orig = session.get(Proposal, reverts)
        before_keys, after_keys = set(), set()
        for raw in orig.diff if orig else []:
            before_keys |= set(state_keys(raw.get("before")))
            after_keys |= set(state_keys(raw.get("after")))
        removable = after_keys - before_keys
    violations, decisions = check_invariants(session, section, plan.rows, plan.backlog, absence_ids=absence_ids, releasing=releasing, removable=removable)
    plan.decisions_required = list(dict.fromkeys(plan.decisions_required + decisions))
    prop = Proposal(
        teacher_id=section.teacher_id, absence=absence, teacher_course_id=section.id, kind=kind,
        status=ProposalStatus.blocked if violations else ProposalStatus.pending, explanation=explanation,
        diff=[r.model_dump(mode="json") for r in plan.rows], backlog=plan.backlog,
        constraints={"hard": plan.hard, "soft": plan.soft, "unresolved": plan.unresolved, "decisions_required": plan.decisions_required, "violations": violations},
        window_start=plan.window_start, window_end=plan.window_end, reverts_proposal_id=reverts,
    )
    session.add(prop)
    session.flush()
    return prop


# ----------------------------------------------------------------- absences
def count_school_days(session: Session, teacher: Teacher, start: date, end: date) -> int:
    ys = load_year_structure(session, teacher.school.district_slug, teacher.school_year)
    return len(school_days(ys, start, end))


def classify_absence(school_day_count: int) -> AbsenceType:
    """By school days, not calendar days (LB-24)."""
    if school_day_count <= 1:
        return AbsenceType.single_day
    if school_day_count <= 10:
        return AbsenceType.short
    return AbsenceType.extended_leave


def create_absence(session: Session, teacher_id: int, start: date, end: date, *, substitute_type: str | None = None, reason: str = "", notes: str = "") -> AbsenceEvent:
    if end < start:
        raise PlanningError(f"The absence ends ({end}) before it starts ({start}).")
    teacher = session.get(Teacher, teacher_id)
    if teacher is None:
        raise PlanningError(f"No teacher with id {teacher_id}.")
    n = count_school_days(session, teacher, start, end)
    if n == 0:
        raise PlanningError(f"There are no school days between {start} and {end}.")
    atype = classify_absence(n)
    if substitute_type:
        sub = SubstituteType(substitute_type)
    else:
        sub = SubstituteType.long_term_sub if atype == AbsenceType.extended_leave else SubstituteType.any_sub
    ab = AbsenceEvent(teacher_id=teacher_id, start_date=start, end_date=end, absence_type=atype, substitute_type=sub, reason=reason, notes=notes)
    session.add(ab)
    session.flush()
    return ab


def default_substitute_type(session: Session, teacher_id: int, start: date, end: date) -> str:
    teacher = session.get(Teacher, teacher_id)
    return "long_term_sub" if classify_absence(count_school_days(session, teacher, start, end)) == AbsenceType.extended_leave else "any_sub"


def _converter(session: Session, section: TeacherCourse, reps: list[ReplacementOption], ctx: dict[str, str]) -> Callable[[Item], Item]:
    from .decisions import choose_replacement

    used: set[str] = set()

    def convert(it: Item) -> Item:
        rep = choose_replacement(reps, used=used, context=keywords(ctx.get(it.unit, ""), it.title))
        if rep:
            used.add(rep.slug)
        return filler_item(it.pinned, rep, absence_id=it.absence_id, unit=it.unit, unit_id=it.unit_id, decision="REPLACE",
                           rationale=f"'{it.title}' now follows a prerequisite that moved later, so the substitute gives independent work instead.", displaced=it.title)

    return convert


def plan_absence_for_section(session: Session, absence: AbsenceEvent, section: TeacherCourse) -> tuple[list[DayDecision], Optional[WindowPlan]]:
    ys = year(session, section)
    lessons = lessons_by_slug(section)
    entries = {e.date: e for e in section.calendar}
    days = school_days(ys, absence.start_date, absence.end_date, section.meeting_days)
    sub_days: list[date] = []
    for d in days:
        e = entries.get(d.date)
        if e is not None and e.status != EntryStatus.planned:
            continue
        if e is not None and e.is_sub_day and e.absence_id not in (None, absence.id):
            continue  # already a substitute day of another absence
        sub_days.append(d.date)
    if not sub_days:
        return [], None
    horizon = ys.quarter_end_on_or_after(absence.end_date) or ys.last_day
    seq_items: list[Item] = []
    items_by_date: dict[date, Optional[Item]] = {}
    for i, e in enumerate(sorted((e for e in section.calendar if absence.start_date <= e.date <= horizon and e.status == EntryStatus.planned), key=lambda e: e.date)):
        it = item_from_entry(e, i * 10, lessons)
        seq_items.append(it)
        if e.date in sub_days:
            items_by_date[e.date] = it
    for d in sub_days:
        items_by_date.setdefault(d, None)
    reps = replacement_options(session, section.teacher_id, section.course.subject.value)
    ctx = unit_context(section)
    decisions = decide_days(sub_days, items_by_date, seq_items, substitute_type=absence.substitute_type.value, absence_days=len(days),
                            replacements=reps, prefs=get_preferences(session, section.teacher_id), unit_context=ctx)
    overrides: dict[date, Item] = {}
    prev_unit, prev_unit_id = "", None
    for dec in decisions:
        base = dec.item
        if base is not None:
            prev_unit, prev_unit_id = base.unit, base.unit_id
        if dec.decision in ("KEEP", "MODIFY") and base is not None:
            overrides[dec.date] = replace(base, pinned=dec.date, is_sub_day=True, absence_id=absence.id, payload={"decision": dec.decision, "rationale": dec.rationale})
        elif dec.decision == "REORDER" and dec.reorder_with is not None:
            overrides[dec.date] = replace(dec.reorder_with, pinned=dec.date, is_sub_day=True, absence_id=absence.id, payload={"decision": "REORDER", "rationale": dec.rationale})
        else:
            overrides[dec.date] = filler_item(dec.date, dec.replacement, absence_id=absence.id, unit=base.unit if base else prev_unit,
                                              unit_id=base.unit_id if base else prev_unit_id, decision=dec.decision, rationale=dec.rationale,
                                              displaced=base.title if base else None)
    owed = [o for o in section.owed if o.open and o.kind == "owed"]
    plan = plan_window(session, section, start=absence.start_date, through=absence.end_date, overrides=overrides, owed=owed, convert=_converter(session, section, reps, ctx))
    return decisions, plan


def plan_absence(session: Session, absence: AbsenceEvent) -> list[Proposal]:
    """Run decisions + reconciliation for every section; returns pending proposals (one per section)."""
    if absence.status == AbsenceStatus.cancelled:
        raise PlanningError("This absence was cancelled.")
    proposals: list[Proposal] = []
    for section in absence.teacher.sections:
        if any(p.status == ProposalStatus.approved and p.teacher_course_id == section.id and p.kind == "absence_reconciliation" for p in absence.proposals):
            continue  # already applied for this section
        for old in absence.proposals:
            if old.teacher_course_id == section.id and old.status in (ProposalStatus.pending, ProposalStatus.blocked):
                old.status = ProposalStatus.superseded
                old.decided_at = utcnow()
        for d in list(absence.decisions):
            if d.teacher_course_id in (section.id, None):
                session.delete(d)
        session.flush()
        decisions, plan = plan_absence_for_section(session, absence, section)
        if plan is None:
            continue
        for dec in decisions:
            entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == dec.date))
            session.add(AbsenceDecision(absence_id=absence.id, teacher_course_id=section.id, calendar_entry_id=entry.id if entry else None, lesson_id=dec.item.lesson_id if dec.item else None,
                                        date=dec.date, decision=Decision(dec.decision), rationale=dec.rationale,
                                        replacement_activity_id=dec.replacement.activity_id if dec.replacement else None, inputs=dec.inputs))
        header = [f"{section.section_name}: {len(decisions)} substitute day(s), {absence.substitute_type.value.replace('_', ' ')}."]
        for d in decisions:
            what = d.item.title if d.item else "no calendar entry"
            extra = f" -> {d.replacement.title}" if d.replacement else (f" -> {d.reorder_with.title}" if d.reorder_with else "")
            header.append(f"- {d.date:%a %b %d}: {d.decision} {what}{extra}. {d.rationale}")
        proposals.append(save_proposal(session, section, plan, kind="absence_reconciliation", explanation=explain_rows(section, plan, header), absence=absence))
    session.flush()
    return proposals


# -------------------------------------------------------------------- apply
def _merged_ids(section: TeacherCourse, keys: list[str]) -> list[int]:
    lessons = lessons_by_slug(section)
    return [lessons[split_key(k)[0]].id for k in keys if split_key(k)[0] in lessons]


def apply_proposal(session: Session, proposal: Proposal, *, acknowledge: bool = False) -> int:
    """Make a pending proposal authoritative. Returns the number of calendar days changed."""
    if proposal.status == ProposalStatus.blocked:
        raise BlockedProposal("This proposal failed the safety check and cannot be approved: " + "; ".join(proposal.constraints.get("violations", [])))
    if proposal.status != ProposalStatus.pending:
        raise ProposalNotPending(f"Proposal {proposal.id} is {proposal.status.value}, not pending.")
    needs = proposal.constraints.get("decisions_required") or []
    if needs and not acknowledge:
        raise DecisionRequired("This proposal needs your explicit decision: " + " ".join(needs))
    section = session.get(TeacherCourse, proposal.teacher_course_id)
    entries = {e.date: e for e in section.calendar}
    rows = [DiffItem(**r) for r in proposal.diff]
    # Stale check (LB-03): every row must still describe the live calendar.
    stale = [r for r in rows if r.before is None or not same_state(entry_state(entries.get(r.date)), r.before)]
    owed_rows = {o.id: o for o in section.owed}
    stale_backlog = [i for i in proposal.backlog.get("resolve", []) if i not in owed_rows or not owed_rows[i].open]
    if stale or stale_backlog:
        proposal.status = ProposalStatus.stale
        proposal.decided_at = utcnow()
        days = ", ".join(str(r.date) for r in stale[:5])
        raise StaleProposalError(f"The calendar changed since proposal {proposal.id} was made (e.g. {days or 'the owed-lesson list'}); plan it again.")
    changed = 0
    for r in rows:
        if not r.changed:
            continue
        e = entries.get(r.date)
        before = entry_state(e)
        if not r.after.get("exists"):
            if e is not None:
                session.delete(e)
        else:
            if e is None:
                e = CalendarEntry(teacher_course_id=section.id, date=r.date, title=r.after.get("title") or "Flex / work day", origin="reconciliation")
                session.add(e)
            write_state(e, r.after, _merged_ids(section, r.after.get("merged_keys") or []))
            e.origin = "reconciliation"
        session.add(CalendarChange(teacher_id=proposal.teacher_id, proposal_id=proposal.id, teacher_course_id=section.id, change_type=r.action, date=r.date,
                                   before=before, after=dict(r.after), reason=r.reason))
        changed += 1
        if r.after.get("replacement_activity_id") and r.after.get("replacement_activity_id") != before.get("replacement_activity_id"):
            act = session.get(ReplacementActivity, r.after["replacement_activity_id"])
            if act is not None:
                act.times_used = (act.times_used or 0) + 1
    for a in proposal.backlog.get("add", []):
        session.add(OwedLesson(teacher_course_id=section.id, lesson_id=a["lesson_id"], key=a["key"], title=a["title"], kind=a.get("kind", "owed"), reason=a.get("reason", ""), created_by_proposal_id=proposal.id))
    for oid in proposal.backlog.get("resolve", []):
        o = owed_rows[oid]
        o.resolved_at, o.resolved_by_proposal_id = utcnow(), proposal.id
    for oid in proposal.backlog.get("reopen", []):
        o = owed_rows.get(oid)
        if o is not None:
            o.resolved_at, o.resolved_by_proposal_id = None, None
    for oid in proposal.backlog.get("close", []):
        o = owed_rows.get(oid)
        if o is not None:
            o.resolved_at, o.resolved_by_proposal_id = utcnow(), proposal.id
    session.flush()
    mark_stale_plans(session, section, [r.date for r in rows if r.changed])
    proposal.status = ProposalStatus.approved
    proposal.decided_at = utcnow()
    _supersede_overlapping(session, section.id, proposal.window_start or date.min, proposal.window_end or date.max, except_id=proposal.id)
    ab = proposal.absence
    if proposal.kind == "absence_reconciliation" and ab is not None and ab.status == AbsenceStatus.planned:
        ab.status = AbsenceStatus.active
    if proposal.kind == "cancel_absence" and ab is not None:
        if not any(p.status == ProposalStatus.pending and p.kind == "cancel_absence" for p in ab.proposals):
            ab.status = AbsenceStatus.cancelled
            for sp in ab.sub_plans:
                sp.status = SubPlanStatus.stale
    if proposal.kind == "undo" and proposal.reverts_proposal_id:
        orig = session.get(Proposal, proposal.reverts_proposal_id)
        if orig is not None:
            orig.status = ProposalStatus.reverted
    session.flush()
    return changed


def apply_all_for_absence(session: Session, absence: AbsenceEvent, *, acknowledge: bool = False) -> list[tuple[Proposal, int]]:
    """One approval for every pending proposal of an absence (LB-54)."""
    out = []
    pending = list(session.scalars(select(Proposal).where(Proposal.absence_id == absence.id, Proposal.status == ProposalStatus.pending).order_by(Proposal.id)))
    if not pending:
        raise ProposalNotPending("This absence has no pending proposals.")
    for p in pending:
        if p.status == ProposalStatus.pending:
            out.append((p, apply_proposal(session, p, acknowledge=acknowledge)))
    return out


def mark_stale_plans(session: Session, section: TeacherCourse, dates: list[date]) -> int:
    """Flag substitute plans whose day changed after they were generated (LB-37)."""
    if not dates:
        return 0
    entries = {e.date: e for e in section.calendar}
    n = 0
    for sp in session.scalars(select(SubPlan).where(SubPlan.teacher_course_id == section.id, SubPlan.date.in_(dates))):
        e = entries.get(sp.date)
        fp = fingerprint(entry_state(e)) if e is not None else ""
        if sp.entry_fingerprint != fp and sp.status != SubPlanStatus.stale:
            sp.status = SubPlanStatus.stale
            n += 1
    return n


def reject_proposal(session: Session, proposal: Proposal) -> None:
    """Only pending (or blocked) proposals can be rejected; approved ones are undone with propose_undo (LB-58)."""
    if proposal.status not in (ProposalStatus.pending, ProposalStatus.blocked):
        raise ProposalNotPending(f"Proposal {proposal.id} is {proposal.status.value}; only pending proposals can be rejected. Use undo to revert an approved one.")
    proposal.status = ProposalStatus.rejected
    proposal.decided_at = utcnow()


# --------------------------------------------------------------------- undo
def propose_undo(session: Session, proposal: Proposal) -> Proposal:
    if proposal.status != ProposalStatus.approved:
        raise ProposalNotPending(f"Proposal {proposal.id} is {proposal.status.value}; only approved proposals can be undone.")
    section = session.get(TeacherCourse, proposal.teacher_course_id)
    entries = {e.date: e for e in section.calendar}
    rows: list[DiffItem] = []
    for raw in proposal.diff:
        r = DiffItem(**raw)
        if not r.changed:
            continue
        live = entry_state(entries.get(r.date))
        if not same_state(live, r.after):
            raise StaleProposalError(f"{r.date} changed after proposal {proposal.id} was approved; later changes depend on it, so it cannot be undone automatically.")
        after = dict(r.before)
        after["status"] = live["status"]
        rows.append(DiffItem(section_id=section.id, date=r.date, action="update", before=live, after=after, changed=True, reason=f"undo proposal {proposal.id}",
                             before_title=live.get("title"), after_title=after.get("title"), before_lesson_slug=live.get("lesson_slug"), after_lesson_slug=after.get("lesson_slug"),
                             after_kind=after.get("kind") or "flex", merged_lesson_slugs=[split_key(k)[0] for k in after.get("merged_keys") or []], is_sub_day=bool(after.get("is_sub_day"))))
    created = [o.id for o in section.owed if o.created_by_proposal_id == proposal.id and o.open]
    resolved = [o.id for o in section.owed if o.resolved_by_proposal_id == proposal.id]
    plan = WindowPlan(rows, {"add": [], "resolve": [], "reopen": resolved, "close": created}, [], [], [], [],
                      min((r.date for r in rows), default=date.today()), max((r.date for r in rows), default=date.today()))
    expl = f"Undo proposal {proposal.id} ({proposal.kind}) for {section.section_name}: restores {len(rows)} day(s) to their state before it was approved."
    return save_proposal(session, section, plan, kind="undo", explanation=expl, absence=proposal.absence, reverts=proposal.id)


# ------------------------------------------------------------------- cancel
def cancel_absence(session: Session, absence: AbsenceEvent) -> list[Proposal]:
    """Withdraw an absence. Unapproved: cancelled at once. Approved: proposals that return its days to teaching (LB-43)."""
    if absence.status == AbsenceStatus.cancelled:
        raise PlanningError("This absence is already cancelled.")
    for p in absence.proposals:
        if p.status in (ProposalStatus.pending, ProposalStatus.blocked) and p.kind == "absence_reconciliation":
            p.status = ProposalStatus.superseded
            p.decided_at = utcnow()
    approved_sections = {p.teacher_course_id for p in absence.proposals if p.status == ProposalStatus.approved and p.kind == "absence_reconciliation"}
    if not approved_sections:
        absence.status = AbsenceStatus.cancelled
        for sp in absence.sub_plans:
            sp.status = SubPlanStatus.stale
        return []
    out = []
    for section in absence.teacher.sections:
        if section.id not in approved_sections:
            continue
        unit_titles = {u.id: u.title for u in section.units}
        rows = []
        for e in section.calendar:
            if e.absence_id != absence.id or not e.is_sub_day:
                continue
            before = entry_state(e)
            after = dict(before)
            after.update({"is_sub_day": False, "absence_id": None, "replacement_activity_id": None})
            if e.lesson_id is None and before.get("kind") == "filler":
                after.update({"kind": "flex", "title": f"Flex / work day ({unit_titles.get(e.unit_id, 'catch-up')})"})
            rows.append(DiffItem(section_id=section.id, date=e.date, action="release", before=before, after=after, changed=True,
                                 reason="absence cancelled; the day returns to regular teaching", before_title=before.get("title"), after_title=after.get("title"),
                                 before_lesson_slug=before.get("lesson_slug"), after_lesson_slug=after.get("lesson_slug"), after_kind=after.get("kind") or "flex"))
        if not rows:
            continue
        plan = WindowPlan(rows, {"add": [], "resolve": []}, [], [], [], [], rows[0].date, rows[-1].date)
        expl = (f"Cancel absence {absence.id} for {section.section_name}: {len(rows)} substitute day(s) return to regular teaching. "
                "Replacement-activity days become flex days; lessons already moved later stay where they are (use the owed-lesson or rebuild actions to pull them forward).")
        out.append(save_proposal(session, section, plan, kind="cancel_absence", explanation=expl, absence=absence))
    return out


# ------------------------------------------------------- progress / slips
def reconcile_slip(session: Session, section: TeacherCourse, on: date, extra_days: int = 1, reason: str = "lesson took longer than planned") -> Proposal:
    """Instruction on ``on`` needs ``extra_days`` more class periods: insert continuation days after it and repair."""
    if extra_days < 1:
        raise PlanningError("A slip needs at least one extra day.")
    entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == on))
    if entry is None or entry.lesson is None:
        raise PlanningError(f"{section.section_name} has no lesson on {on} to extend.")
    lesson = entry.lesson
    existing = [int(e.continuation or 0) for e in section.calendar if e.lesson_id == lesson.id]
    first = max(existing + [0]) + 1
    conts = [lesson_item(lesson, seq=-1 + i * 0.01, continuation=first + i) for i in range(extra_days)]
    ys = year(session, section)
    start = next_school_day(ys, on)
    reps = replacement_options(session, section.teacher_id, section.course.subject.value)
    owed = [o for o in section.owed if o.open and o.kind == "owed"]
    plan = plan_window(session, section, start=start, through=start, inserted=conts, owed=owed, convert=_converter(session, section, reps, unit_context(section)))
    header = [f"{section.section_name}: '{lesson.title}' on {on} needs {extra_days} more day(s) ({reason})."]
    return save_proposal(session, section, plan, kind="slip_reconciliation", explanation=explain_rows(section, plan, header))


def propose_backlog_placement(session: Session, section: TeacherCourse, *, start: Optional[date] = None, include_dropped: bool = False) -> Proposal:
    """Schedule owed (and optionally dropped) lessons from ``start``, absorbing slack (LB-01)."""
    owed = [o for o in section.owed if o.open and (o.kind == "owed" or include_dropped)]
    if not owed:
        raise PlanningError(f"{section.section_name} has no owed lessons.")
    ys = year(session, section)
    start = start or next_school_day(ys, date.today() - timedelta(days=1))
    reps = replacement_options(session, section.teacher_id, section.course.subject.value)
    plan = plan_window(session, section, start=start, through=start, owed=owed, convert=_converter(session, section, reps, unit_context(section)))
    header = [f"{section.section_name}: schedule {len(owed)} owed lesson(s) from {start}."]
    return save_proposal(session, section, plan, kind="backlog_placement", explanation=explain_rows(section, plan, header))


def mark_progress(session: Session, section: TeacherCourse, on: date, status: str, note: str = "") -> CalendarEntry:
    entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == on))
    if entry is None:
        raise PlanningError(f"No calendar entry for {section.section_name} on {on}")
    try:
        entry.status = EntryStatus(status)
    except ValueError as exc:
        raise PlanningError(f"Unknown status {status!r}; use planned, completed or skipped.") from exc
    if note:
        entry.notes = (entry.notes + "; " if entry.notes else "") + note
    return entry
