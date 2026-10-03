"""Database adapter for the planning core: build items/slots from the calendar,
run decisions + repair, store proposals, apply approved proposals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..curriculum.calendar import SchoolDay, entries_between, load_year_structure, school_days
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
    Proposal,
    ProposalStatus,
    ReplacementActivity,
    SubstituteType,
    TeacherCourse,
)
from ..profile.rules import get_preferences, resolve_rules, rule_minutes
from ..schemas import DiffItem, ProposalSpec
from .decisions import DayDecision, ReplacementOption, decide_days
from .engine import Change, Item, Slot, repair


# ------------------------------------------------------------------ builders
def item_from_entry(entry: CalendarEntry, seq: int) -> Item:
    lesson = entry.lesson
    if lesson is None:
        kind = "placeholder" if entry.kind == EntryKind.placeholder else "flex"
        return Item(key=f"{kind}-{entry.id}", title=entry.title, kind=kind, sequence=seq, unit=entry.unit.slug if entry.unit else "", priority="optional", min_minutes=0, duration=0)
    kind = "assessment" if lesson.is_assessment else ("review" if lesson.lesson_type.value == "review" else "lesson")
    return Item(
        key=lesson.slug,
        title=entry.title if not entry.merged_lesson_ids else lesson.title,
        kind=kind,
        lesson_id=lesson.id,
        lesson_type=lesson.lesson_type.value,
        priority=lesson.priority.value,
        duration=lesson.duration_minutes,
        min_minutes=lesson.minimum_viable_minutes,
        can_move=lesson.can_move,
        quarter_boundary_allowed=lesson.quarter_boundary_allowed,
        hard_date=lesson.hard_date,
        prereqs={d.depends_on.slug for d in lesson.dependencies},
        delivery=set(lesson.delivery_requirement or []),
        unit=lesson.unit.slug,
        merged=[],
        sequence=seq,
    )


def replacement_options(session: Session, teacher_id: int, subject: str) -> list[ReplacementOption]:
    rows = session.scalars(select(ReplacementActivity).where(ReplacementActivity.subject == subject, (ReplacementActivity.teacher_id == teacher_id) | (ReplacementActivity.teacher_id.is_(None))))
    opts = []
    for r in rows:
        opts.append(ReplacementOption(r.slug, r.title, r.description, r.duration_minutes, r.category, list(r.materials or []), r.student_output, list(r.tags or []), r.id))
    # Teacher-owned activities first.
    return sorted(opts, key=lambda o: 0 if o.activity_id and any(o.activity_id == r.id and r.teacher_id == teacher_id for r in rows) else 1)


@dataclass
class SectionWindow:
    section: TeacherCourse
    days: list[SchoolDay]
    entries: dict[date, CalendarEntry]
    boundary: date
    boundary_label: str


def window_for(session: Session, section: TeacherCourse, start: date, *, extend_to: Optional[date] = None) -> SectionWindow:
    """Slots from ``start`` to the next quarter end (inclusive), or further if asked."""
    ys = load_year_structure(session, section.teacher.school.district_slug, section.teacher.school_year)
    boundary = ys.quarter_end_on_or_after(start) or ys.last_day
    label = "quarter end"
    if extend_to and extend_to > boundary:
        boundary, label = extend_to, "planning horizon"
    days = school_days(ys, start, boundary, section.meeting_days)
    entries = {e.date: e for e in entries_between(session, section.id, start, boundary)}
    return SectionWindow(section, days, entries, boundary, label)


# ---------------------------------------------------------------- planning
def plan_absence_for_section(session: Session, absence: AbsenceEvent, section: TeacherCourse) -> tuple[list[DayDecision], ProposalSpec, list[Change]]:
    teacher = absence.teacher
    prefs = get_preferences(session, teacher.id)
    win = window_for(session, section, absence.start_date, extend_to=absence.end_date)
    sub_days = [d.date for d in win.days if absence.start_date <= d.date <= absence.end_date]
    if not sub_days:
        return [], ProposalSpec(section_id=section.id, explanation="This section does not meet during the absence.", diff=[]), []

    ordered_entries = [win.entries[d.date] for d in win.days if d.date in win.entries and win.entries[d.date].status == EntryStatus.planned]
    items = [item_from_entry(e, i) for i, e in enumerate(ordered_entries)]
    by_date = {e.date: it for e, it in zip(ordered_entries, items)}
    absence_days = len(sub_days)
    reps = replacement_options(session, teacher.id, section.course.subject.value)
    decisions = decide_days(sub_days, by_date, items, substitute_type=absence.substitute_type.value, absence_days=absence_days, replacements=reps, prefs=prefs)

    # Translate decisions into pinned sub-day items.
    plan_items: list[Item] = []
    consumed_keys: set[str] = set()
    for dec in decisions:
        if dec.decision in ("KEEP", "MODIFY") and dec.item is not None:
            it = dec.item
            it.pinned, it.is_sub_day = dec.date, True
            it.payload = {"decision": dec.decision, "rationale": dec.rationale}
            consumed_keys.add(it.key)
            plan_items.append(it)
        elif dec.decision == "REORDER" and dec.reorder_with is not None:
            it = dec.reorder_with
            it.pinned, it.is_sub_day = dec.date, True
            it.payload = {"decision": "REORDER", "rationale": dec.rationale}
            consumed_keys.add(it.key)
            plan_items.append(it)
        else:  # REPLACE / POSTPONE -> replacement payload on the sub day
            rep = dec.replacement
            title = f"{rep.title if rep else 'Independent work'} [SUB]"
            plan_items.append(Item(key=f"sub-{section.id}-{dec.date.isoformat()}", title=title, kind="filler", pinned=dec.date, is_sub_day=True, sequence=-1,
                                   payload={"decision": dec.decision, "rationale": dec.rationale, "replacement_slug": rep.slug if rep else None, "replacement_id": rep.activity_id if rep else None,
                                            "displaced": dec.item.title if dec.item else None}))
    for it in items:
        if it.key not in consumed_keys:
            plan_items.append(it)

    study = rule_minutes(resolve_rules(session, teacher.id, section=section, is_assessment=True), "study_period")
    slots = [Slot(d.date, d.quarter, d.early_release, section.minutes_per_meeting, existing_key=(by_date[d.date].key if d.date in by_date else None), existing_title=(win.entries[d.date].title if d.date in win.entries else None), existing_kind=(win.entries[d.date].kind.value if d.date in win.entries else "lesson")) for d in win.days if d.date in win.entries]
    result = repair(slots, plan_items, minutes=section.minutes_per_meeting, study_minutes=study, boundary_label=win.boundary_label, soft_prefs=prefs)

    diff = [
        DiffItem(section_id=section.id, date=c.date, action=c.action, before_title=c.before_title, after_title=c.after_title, before_lesson_slug=c.before_key, after_lesson_slug=c.after_key,
                 after_kind=c.after_kind, merged_lesson_slugs=c.merged, is_sub_day=c.is_sub_day, reason=c.reason, new_date=c.new_date)
        for c in result.changes
    ]
    explanation = explain(section, decisions, result, win)
    spec = ProposalSpec(section_id=section.id, explanation=explanation, diff=diff, hard_constraints=result.hard_constraints, soft_constraint_notes=result.soft_notes, unresolved=result.unresolved)
    return decisions, spec, result.changes


def explain(section: TeacherCourse, decisions: list[DayDecision], result, win: SectionWindow) -> str:
    lines = [f"{section.section_name}: {len(decisions)} substitute day(s); window runs to {win.boundary_label} on {win.boundary}."]
    for d in decisions:
        what = d.item.title if d.item else "flex day"
        lines.append(f"- {d.date:%a %b %d}: {d.decision} — {what}. {d.rationale}")
    merges = [c for c in result.changes if c.action in ("merge", "compress")]
    shifts = [c for c in result.changes if c.action == "shift" and c.after_title]
    if merges:
        lines.append("Compressed: " + "; ".join(f"{c.after_title} on {c.date:%a %b %d}" for c in merges))
    if shifts:
        lines.append(f"Shifted {len(shifts)} day(s) to keep the sequence in order; a simple one-day shift would have pushed content past the {win.boundary_label}." if merges else f"Shifted {len(shifts)} day(s); slack in the calendar absorbed the lost time.")
    if result.dropped:
        lines.append("Dropped optional/recommended content: " + ", ".join(i.title for i in result.dropped))
    if result.deferred:
        lines.append(f"Deferred past the {win.boundary_label}: " + ", ".join(i.title for i in result.deferred))
    if result.unresolved:
        lines.append("Needs your decision: " + " ".join(result.unresolved))
    if result.soft_notes:
        lines.append("Notes: " + " ".join(result.soft_notes))
    return "\n".join(lines)


def create_absence(session: Session, teacher_id: int, start: date, end: date, *, substitute_type: str | None = None, reason: str = "", notes: str = "") -> AbsenceEvent:
    days = (end - start).days + 1
    if days <= 1:
        atype = AbsenceType.single_day
    elif days <= 10:
        atype = AbsenceType.short
    else:
        atype = AbsenceType.extended_leave
    sub = SubstituteType(substitute_type) if substitute_type else (SubstituteType.long_term_sub if atype == AbsenceType.extended_leave else SubstituteType.any_sub)
    ab = AbsenceEvent(teacher_id=teacher_id, start_date=start, end_date=end, absence_type=atype, substitute_type=sub, reason=reason, notes=notes)
    session.add(ab)
    session.flush()
    return ab


def plan_absence(session: Session, absence: AbsenceEvent) -> list[Proposal]:
    """Run decisions + reconciliation for every section; returns pending proposals (one per section)."""
    for old in absence.proposals:
        if old.status == ProposalStatus.pending:
            old.status = ProposalStatus.superseded
    for d in list(absence.decisions):
        session.delete(d)
    proposals: list[Proposal] = []
    for section in absence.teacher.sections:
        decisions, spec, _ = plan_absence_for_section(session, absence, section)
        for dec in decisions:
            entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == dec.date))
            session.add(AbsenceDecision(absence_id=absence.id, calendar_entry_id=entry.id if entry else None, lesson_id=dec.item.lesson_id if dec.item else None, date=dec.date,
                                        decision=Decision(dec.decision), rationale=dec.rationale, replacement_activity_id=dec.replacement.activity_id if dec.replacement else None, inputs=dec.inputs))
        if not spec.diff:
            continue
        prop = Proposal(teacher_id=absence.teacher_id, absence_id=absence.id, teacher_course_id=section.id, kind="absence_reconciliation", explanation=spec.explanation,
                        diff=[d.model_dump(mode="json") for d in spec.diff], constraints={"hard": spec.hard_constraints, "soft": spec.soft_constraint_notes, "unresolved": spec.unresolved})
        session.add(prop)
        proposals.append(prop)
    session.flush()
    return proposals


# ------------------------------------------------------------------- apply
def apply_proposal(session: Session, proposal: Proposal) -> int:
    """Make a pending proposal authoritative. Returns number of calendar rows changed."""
    if proposal.status != ProposalStatus.pending:
        raise ValueError(f"Proposal {proposal.id} is {proposal.status.value}, not pending")
    section = session.get(TeacherCourse, proposal.teacher_course_id)
    lessons_by_slug = {l.slug: l for u in section.units for l in u.lessons}
    changed = 0
    from ..models import utcnow

    for raw in proposal.diff:
        item = DiffItem(**raw)
        if item.action in ("drop", "defer"):
            continue
        entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == item.date))
        before = {"title": entry.title if entry else None, "lesson": entry.lesson.slug if entry and entry.lesson else None, "kind": entry.kind.value if entry else None}
        if entry is None:
            entry = CalendarEntry(teacher_course_id=section.id, date=item.date, title=item.after_title or "Flex / work day")
            session.add(entry)
        lesson = lessons_by_slug.get(item.after_lesson_slug or "")
        new_title = item.after_title or "Flex / work day"
        new_kind = EntryKind(item.after_kind) if item.after_kind in EntryKind.__members__ else EntryKind.lesson
        merged_ids = [lessons_by_slug[s].id for s in item.merged_lesson_slugs if s in lessons_by_slug]
        same = entry.title == new_title and (entry.lesson_id == (lesson.id if lesson else None)) and entry.is_sub_day == item.is_sub_day and entry.merged_lesson_ids == merged_ids
        if same and item.action in ("keep", "preserve"):
            continue
        entry.title = new_title
        entry.lesson_id = lesson.id if lesson else None
        entry.unit_id = lesson.unit_id if lesson else entry.unit_id
        entry.kind = new_kind
        entry.is_sub_day = item.is_sub_day
        entry.merged_lesson_ids = merged_ids
        entry.origin = "reconciliation"
        if item.is_sub_day and item.action == "replace":
            entry.notes = (item.reason or "").strip()
        session.add(CalendarChange(teacher_id=proposal.teacher_id, proposal_id=proposal.id, teacher_course_id=section.id, change_type=item.action, date=item.date, before=before,
                                   after={"title": new_title, "lesson": item.after_lesson_slug, "kind": new_kind.value}, reason=item.reason))
        changed += 1
    # Deferred lessons: mark the lesson so the next calendar build / window knows.
    for raw in proposal.diff:
        item = DiffItem(**raw)
        if item.action in ("drop", "defer"):
            session.add(CalendarChange(teacher_id=proposal.teacher_id, proposal_id=proposal.id, teacher_course_id=section.id, change_type=item.action, date=item.date, before={"lesson": item.before_lesson_slug, "title": item.before_title}, after={}, reason=item.reason))
    proposal.status = ProposalStatus.approved
    proposal.decided_at = utcnow()
    if proposal.absence and proposal.absence.status == AbsenceStatus.planned:
        proposal.absence.status = AbsenceStatus.active if proposal.absence.start_date <= date.today() else AbsenceStatus.planned
    session.flush()
    return changed


def reject_proposal(session: Session, proposal: Proposal) -> None:
    from ..models import utcnow

    proposal.status = ProposalStatus.rejected
    proposal.decided_at = utcnow()


# ------------------------------------------------------- progress / slippage
def reconcile_slip(session: Session, section: TeacherCourse, on: date, extra_days: int = 1, reason: str = "lesson took longer than planned") -> Proposal:
    """Instruction on ``on`` needs ``extra_days`` more class periods: repair the schedule from the next day."""
    win = window_for(session, section, on)
    ordered = [win.entries[d.date] for d in win.days if d.date in win.entries and win.entries[d.date].status == EntryStatus.planned]
    if not ordered:
        raise ValueError("No planned entries from that date")
    items = [item_from_entry(e, i) for i, e in enumerate(ordered)]
    head = items[0]
    head.pinned = on
    cont = [Item(key=f"{head.key}-cont{i+1}", title=f"{head.title} (continued)", kind=head.kind if head.kind != "assessment" else "lesson", lesson_id=head.lesson_id, lesson_type=head.lesson_type, priority="required", duration=head.duration, min_minutes=head.min_minutes, prereqs={head.key}, unit=head.unit, sequence=0, delivery=set(head.delivery)) for i in range(extra_days)]
    rest = items[1:]
    for r in rest:
        r.sequence += extra_days
    for i, c in enumerate(cont, start=1):
        c.sequence = i
    plan_items = [head, *cont, *rest]
    study = rule_minutes(resolve_rules(session, section.teacher_id, section=section, is_assessment=True), "study_period")
    slots = [Slot(d.date, d.quarter, d.early_release, section.minutes_per_meeting, existing_key=(items[[e.date for e in ordered].index(d.date)].key if d.date in win.entries else None), existing_title=(win.entries[d.date].title if d.date in win.entries else None)) for d in win.days if d.date in win.entries]
    result = repair(slots, plan_items, minutes=section.minutes_per_meeting, study_minutes=study, boundary_label=win.boundary_label, soft_prefs=get_preferences(session, section.teacher_id))
    diff = [DiffItem(section_id=section.id, date=c.date, action=c.action, before_title=c.before_title, after_title=c.after_title, before_lesson_slug=c.before_key, after_lesson_slug=(c.after_key.split("-cont")[0] if c.after_key and "-cont" in c.after_key else c.after_key), after_kind=c.after_kind, merged_lesson_slugs=c.merged, is_sub_day=c.is_sub_day, reason=c.reason) for c in result.changes]
    explanation = f"{section.section_name}: '{head.title}' on {on} needs {extra_days} more day(s) ({reason}). " + ("Compressed: " + "; ".join(c.after_title for c in result.changes if c.action in ('merge', 'compress')) + ". " if any(c.action in ('merge', 'compress') for c in result.changes) else "") + (" ".join(result.unresolved) if result.unresolved else "")
    prop = Proposal(teacher_id=section.teacher_id, teacher_course_id=section.id, kind="slip_reconciliation", explanation=explanation, diff=[d.model_dump(mode="json") for d in diff], constraints={"hard": result.hard_constraints, "soft": result.soft_notes, "unresolved": result.unresolved})
    session.add(prop)
    session.flush()
    return prop


def mark_progress(session: Session, section: TeacherCourse, on: date, status: str, note: str = "") -> CalendarEntry:
    entry = session.scalar(select(CalendarEntry).where(CalendarEntry.teacher_course_id == section.id, CalendarEntry.date == on))
    if entry is None:
        raise ValueError(f"No calendar entry for {section.section_name} on {on}")
    entry.status = EntryStatus(status)
    if note:
        entry.notes = (entry.notes + "; " if entry.notes else "") + note
    return entry
