"""LessonBridge command-line interface."""
from __future__ import annotations

import enum
import functools
import ipaddress
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from sqlalchemy import func, select

from . import db
from .absence import leave as leave_mod
from .absence.service import (
    PlanningError,
    apply_all_for_absence,
    apply_proposal,
    cancel_absence,
    create_absence,
    default_substitute_type,
    mark_progress,
    plan_absence,
    propose_backlog_placement,
    propose_undo,
    reconcile_slip,
    reject_proposal,
)
from .config import settings
from .curriculum.calendar import entries_between, propose_rebuild
from .generation.llm import LLMClient
from .generation.subplan import generate_plans_for_absence
from .ingestion.index import search as fts_search
from .models import AbsenceEvent, DocumentKind, DocumentType, Proposal, ProposalStatus, RuleScope, SchoolCalendarEvent, SubPlan, Teacher, TeacherCourse, TeacherRule
from .profile.onboarding import OnboardingError, OnboardingInput, confirm_calendar, count_touches, record_touch, run_onboarding
from .profile.rules import RuleTargetError, add_rule, describe_rule, resolve_target
from .render import markdown as md
from .schemas import DiffItem

app = typer.Typer(help="LessonBridge: instructional continuity for teachers.", no_args_is_help=True)
calendar_app = typer.Typer(help="Instructional calendar")
absence_app = typer.Typer(help="Absences, proposals and substitute plans")
leave_app = typer.Typer(help="Extended-leave planning")
rules_app = typer.Typer(help="Classroom rules")
progress_app = typer.Typer(help="Record what actually happened")
backlog_app = typer.Typer(help="Owed lessons (never deleted, waiting for a day)")
documents_app = typer.Typer(help="Teacher documents after onboarding")
app.add_typer(calendar_app, name="calendar")
app.add_typer(absence_app, name="absence")
app.add_typer(leave_app, name="leave")
app.add_typer(rules_app, name="rules")
app.add_typer(progress_app, name="progress")
app.add_typer(backlog_app, name="backlog")
app.add_typer(documents_app, name="documents")
console = Console(highlight=False)


def say(text: str, style: str = "") -> None:
    """Print user/data text safely: Rich markup in it is shown literally (LB-57)."""
    console.print(escape(str(text)), style=style or None)


def handle(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (PlanningError, OnboardingError, RuleTargetError, ValueError) as exc:
            say(f"Error: {exc}", "red")
            raise typer.Exit(1)

    return wrapper


def _teacher(session, teacher_id: Optional[int]) -> Teacher:
    """The teacher to act on; never a silent guess when several exist (LB-44)."""
    if teacher_id:
        t = session.get(Teacher, teacher_id)
        if t is None:
            raise PlanningError(f"No teacher with id {teacher_id}.")
        return t
    teachers = list(session.scalars(select(Teacher).order_by(Teacher.id)))
    if not teachers:
        raise PlanningError("No teacher found; run `lessonbridge onboard` first.")
    if len(teachers) > 1:
        raise PlanningError("Several teachers exist (" + ", ".join(f"{t.id}: {t.name}" for t in teachers) + "); pass --teacher-id.")
    return teachers[0]


def _section(session, teacher: Teacher, section: Optional[str]) -> TeacherCourse:
    """Match by 'id:N', then exact period, then exact name, then a unique name fragment (LB-44)."""
    secs = list(teacher.sections)
    if section is None:
        if len(secs) == 1:
            return secs[0]
        raise PlanningError("Pass --section (a period such as 3, or a name). Sections: " + "; ".join(f"P{s.period} {s.section_name}" for s in secs))
    key = section.strip()
    if key.lower().startswith("id:"):
        sid = key[3:].strip()
        for s in secs:
            if str(s.id) == sid:
                return s
        raise PlanningError(f"No section with id {sid} for this teacher.")
    for matcher in (lambda s: s.period == key.lstrip("Pp"), lambda s: s.section_name.lower() == key.lower()):
        hits = [s for s in secs if matcher(s)]
        if len(hits) == 1:
            return hits[0]
    frag = [s for s in secs if key.lower() in s.section_name.lower()]
    if len(frag) == 1:
        return frag[0]
    raise PlanningError(f"Section {section!r} is ambiguous or unknown. Sections: " + "; ".join(f"P{s.period} {s.section_name}" for s in secs))


def _out(path: Optional[Path], text: str, default_name: str) -> None:
    if path is None:
        say(text)
        return
    target = path / default_name if path.suffix == "" else path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    say(f"wrote {target}", "green")


def _render(session, p: Proposal) -> str:
    sec = session.get(TeacherCourse, p.teacher_course_id)
    owed = {o.id: o.title for o in sec.owed}
    return md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} ({p.kind}, {p.status.value}) — {sec.section_name}", backlog=p.backlog, owed_titles=owed)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlanningError(f"{value!r} is not a date (use YYYY-MM-DD).") from exc


@app.callback()
def _init(data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Where the database and documents live (default ./data)")):
    if data_dir:
        settings.data_dir = data_dir
    db.init_engine(settings)


@app.command()
@handle
def init(force: bool = typer.Option(False, help="Re-fetch public sources even if not due")):
    """Create the database and load LCPS public context (calendar, standards, curriculum)."""
    from .ingestion.pipeline import register_public_sources, sync_calendar_events
    from .providers import get_provider

    with db.session_scope() as s:
        provider = get_provider("lcps")
        results = register_public_sources(s, provider, force=force, cfg=settings)
        n = sync_calendar_events(s, provider, "2026-2027")
        say(f"Public sources touched: {len(results)}; calendar events added: {n}")
        llm = LLMClient(settings)
        gen = f"Claude ({settings.model})" if llm.available() else f"template ({llm.unavailable_reason})"
        say(f"Network access: {'on' if settings.allow_network else 'off'}; generator: {gen}")


@app.command()
@handle
def onboard(profile: Path = typer.Argument(..., help="YAML onboarding profile (see examples/teacher_profile.yaml)"), today: Optional[str] = typer.Option(None, help="Override today's date (YYYY-MM-DD)")):
    """Build (or update) a teacher's instructional context from public sources plus their documents."""
    inp = OnboardingInput.from_yaml(profile)
    with db.session_scope() as s:
        res = run_onboarding(s, inp, cfg=settings, llm=LLMClient(settings), today=_date(today) if today else None)
        console.rule("Onboarding complete" if not res.updated_existing else "Profile updated")
        say(f"Teacher id: {res.teacher_id}   onboarding touches: {res.touches}")
        say(f"Public sources loaded: {', '.join(res.public_sources) or 'already current'}")
        say(f"Calendar dates needing confirmation: {len(res.flagged_events)} (run `lessonbridge calendar confirm`)")
        for subj, src in res.curriculum_sources.items():
            say(f"  {subj}: curriculum from {src}")
        for name, rep in res.build_reports.items():
            if rep.entries_created:
                say(f"  {name}: {rep.entries_created} course-days, {rep.entries_created - rep.flex_days} lesson days, {rep.flex_days} flex / to-be-detailed days")
            for p in rep.problems[:4]:
                say(f"    note: {p}", "yellow")
        for c in res.curriculum_conflicts:
            say(f"  conflict: {c}", "yellow")
        for n in res.curriculum_notes:
            say(f"  note: {n}")
        if res.extracted_rules:
            say(f"Rules learned from documents: {len(res.extracted_rules)}")
            for r in res.extracted_rules[:10]:
                say(f"  - {r}")
        for w in res.warnings:
            say(w, "yellow")


@app.command()
@handle
def teachers():
    """List teacher profiles."""
    with db.session_scope() as s:
        for t in s.scalars(select(Teacher).order_by(Teacher.id)):
            say(f"{t.id}: {t.name} — {t.school.name}, {len(t.sections)} sections")


@app.command()
@handle
def status(teacher_id: Optional[int] = typer.Option(None)):
    """Teacher profile, sections, pending proposals, owed lessons and the effort metric."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        say(f"{t.name} — {t.school.name} ({t.school.district_name}), grade {t.grade}, {t.school_year}")
        table = Table("id", "section", "period", "days", "min", "units", "course-days", "owed")
        for sec in t.sections:
            table.add_row(str(sec.id), escape(sec.section_name), sec.period, "".join("MTWRF"[d - 1] for d in sec.meeting_days), str(sec.minutes_per_meeting),
                          str(len(sec.active_units)), str(len(sec.calendar)), str(sum(1 for o in sec.owed if o.open)))
        console.print(table)
        pend = s.scalar(select(func.count(Proposal.id)).where(Proposal.teacher_id == t.id, Proposal.status == ProposalStatus.pending))
        stale = s.scalar(select(func.count(SubPlan.id)).join(AbsenceEvent).where(AbsenceEvent.teacher_id == t.id, SubPlan.status == "stale"))
        say(f"Pending proposals: {pend}   active rules: {sum(1 for r in t.rules if r.active)}   absences: {len(t.absences)}   stale plans: {stale}   teacher touches: {count_touches(s, t.id)}")
        if s.scalar(select(SchoolCalendarEvent).where(SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False))):
            say("School calendar dates are still provisional; run `lessonbridge calendar confirm`.", "yellow")


# ----------------------------------------------------------------- calendar
@calendar_app.command("show")
@handle
def calendar_show(section: Optional[str] = typer.Option(None, help="Period (e.g. 3), exact name, or id:N"), start: Optional[str] = None, end: Optional[str] = None,
                  teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Print the instructional calendar for a section."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        a = _date(start) if start else date.today()
        b = _date(end) if end else a + timedelta(days=28)
        _out(out, md.render_calendar(entries_between(s, sec.id, a, b), title=f"{sec.section_name}: {a} to {b}"), f"calendar_{sec.id}.md")


@calendar_app.command("events")
@handle
def calendar_events(school_year: str = "2026-2027"):
    """List school calendar events and whether they are confirmed."""
    with db.session_scope() as s:
        table = Table("id", "date", "type", "title", "status")
        for e in s.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.school_year == school_year).order_by(SchoolCalendarEvent.date)):
            table.add_row(str(e.id), f"{e.date}" + (f" → {e.end_date}" if e.end_date else ""), e.event_type.value, escape(e.title), "confirmed" if e.confirmed else ("provisional" if e.needs_confirmation else "ok"))
        console.print(table)


@calendar_app.command("confirm")
@handle
def calendar_confirm(ids: Optional[str] = typer.Option(None, help="Comma-separated event ids; default all"), teacher_id: Optional[int] = None, school_year: str = "2026-2027"):
    """Confirm provisional school-calendar dates (one teacher touch for the whole batch)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        say(f"Confirmed {confirm_calendar(s, t.id, school_year, event_ids=[int(x) for x in ids.split(',')] if ids else None)} dates.")


@calendar_app.command("rebuild")
@handle
def calendar_rebuild(section: str = typer.Option(..., help="Period, exact name, or id:N"), start: Optional[str] = None, teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Propose rebuilding planned days from a date; completed, skipped and substitute days stay. Approve to apply."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        prop, _ = propose_rebuild(s, sec, start=_date(start) if start else None)
        record_touch(s, t.id, "calendar", "click", "request rebuild")
        _out(out, _render(s, prop), f"proposal_{prop.id}.md")
        say(f"Proposal {prop.id} is {prop.status.value}. Approve with `lessonbridge absence approve {prop.id}`.")


@calendar_app.command("edit")
@handle
def calendar_edit(section: str, on: str, title: Optional[str] = None, notes: Optional[str] = None, lesson: Optional[str] = typer.Option(None, help="Put this lesson slug on a flex / to-be-detailed day"), teacher_id: Optional[int] = None):
    """Edit one calendar day (title, notes, or place an unscheduled lesson)."""
    from .curriculum.editing import edit_entry

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        e = edit_entry(s, sec, _date(on), title=title, notes=notes, lesson_slug=lesson)
        record_touch(s, t.id, "calendar", "correction", "edit day")
        say(f"{sec.section_name} {e.date}: {e.title}")


@calendar_app.command("fill")
@handle
def calendar_fill(section: str, on: str, title: str = typer.Option(...), objective: str = typer.Option(...), lesson_type: str = "guided_practice",
                  materials: str = typer.Option("", help="Comma-separated"), outputs: str = typer.Option("", help="Comma-separated"), teacher_id: Optional[int] = None):
    """Fill a 'lesson to be detailed' (or flex) day with a lesson you describe."""
    from .curriculum.editing import fill_placeholder

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        l = fill_placeholder(s, sec, _date(on), title=title, objective=objective, lesson_type=lesson_type,
                             materials=[m.strip() for m in materials.split(",") if m.strip()], outputs=[o.strip() for o in outputs.split(",") if o.strip()])
        record_touch(s, t.id, "calendar", "field", "fill placeholder")
        say(f"{sec.section_name} {on}: {l.title} ({l.slug})")


# ------------------------------------------------------------------- rules
class ScopeOption(str, enum.Enum):
    teacher = "teacher"
    course = "course"
    unit = "unit"
    lesson = "lesson"


@rules_app.command("add")
@handle
def rules_add(text: str, teacher_id: Optional[int] = None, scope: ScopeOption = typer.Option(ScopeOption.teacher, help="teacher | course | unit | lesson"),
              target: Optional[str] = typer.Option(None, help="Subject for course scope, unit slug, or lesson slug")):
    """Add a classroom rule (plain language; LessonBridge shows how it understood it)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sc = RuleScope(scope.value)
        r = add_rule(s, t.id, text, scope=sc, scope_id=resolve_target(s, t.id, sc, target))
        record_touch(s, t.id, "rules", "field", "add rule")
        say(f"Rule {r.id} [{r.category}, {r.scope.value}] understood as: {describe_rule(r.structured)}")


@rules_app.command("list")
@handle
def rules_list(teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        table = Table("id", "scope", "category", "text", "understood as", "source", "active")
        for r in s.scalars(select(TeacherRule).where(TeacherRule.teacher_id == t.id).order_by(TeacherRule.id)):
            table.add_row(str(r.id), r.scope.value, escape(r.category), escape(r.text), escape(describe_rule(r.structured or {})), escape(r.source), "yes" if r.active else "superseded")
        console.print(table)


# ---------------------------------------------------------------- absences
class SubOption(str, enum.Enum):
    any_sub = "any_sub"
    long_term_sub = "long_term_sub"


@absence_app.command("plan")
@handle
def absence_plan(start: str, end: Optional[str] = None, substitute: Optional[SubOption] = typer.Option(None, help="any_sub | long_term_sub"), reason: str = "",
                 teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Report an absence: decide each day and reconcile the calendar into proposals (applied only when approved)."""
    a = _date(start)
    b = _date(end) if end else a
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sub = substitute.value if substitute else None
        if sub is None and default_substitute_type(s, t.id, a, b) == "long_term_sub":
            # Never assume a long-term substitute silently (LB-24).
            if sys.stdin.isatty():
                sub = "long_term_sub" if typer.confirm("This absence is longer than ten school days. Will a long-term substitute teach new material?", default=True) else "any_sub"
            else:
                raise PlanningError("This absence is longer than ten school days; pass --substitute long_term_sub or --substitute any_sub.")
        ab = create_absence(s, t.id, a, b, substitute_type=sub, reason=reason)
        record_touch(s, t.id, "absence", "field", "report absence")
        props = plan_absence(s, ab)
        say(f"Absence {ab.id}: {ab.absence_type.value}, substitute: {ab.substitute_type.value}, {a} → {b}")
        _out(out, "\n\n".join(_render(s, p) for p in props), f"absence_{ab.id}_proposals.md")
        say(f"{len(props)} proposal(s). Approve all at once with `lessonbridge absence approve --all {ab.id}`.")


@absence_app.command("show")
@handle
def absence_show(proposal_id: int, out: Optional[Path] = None):
    """Print one proposal's full diff."""
    with db.session_scope() as s:
        p = s.get(Proposal, proposal_id)
        if p is None:
            raise PlanningError(f"No proposal {proposal_id}.")
        _out(out, _render(s, p), f"proposal_{p.id}.md")


@absence_app.command("approve")
@handle
def absence_approve(proposal_id: Optional[int] = typer.Argument(None), all_for: Optional[int] = typer.Option(None, "--all", help="Approve every pending proposal for this absence id"),
                    acknowledge: bool = typer.Option(False, "--acknowledge", help="Accept the decisions a proposal lists (e.g. an assessment crossing a quarter)"), teacher_id: Optional[int] = None):
    """Approve proposal(s); the calendar changes only now. Outdated proposals are refused."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        if all_for is not None:
            ab = s.get(AbsenceEvent, all_for)
            if ab is None:
                raise PlanningError(f"No absence {all_for}.")
            done = apply_all_for_absence(s, ab, acknowledge=acknowledge)
        else:
            if proposal_id is None:
                raise PlanningError("Pass a proposal id or --all ABSENCE_ID.")
            p = s.get(Proposal, proposal_id)
            if p is None:
                raise PlanningError(f"No proposal {proposal_id}.")
            done = [(p, apply_proposal(s, p, acknowledge=acknowledge))]
        for p, n in done:
            say(f"Proposal {p.id} approved: {n} calendar day(s) changed.")
        record_touch(s, t.id, "absence", "decision", "approve")


@absence_app.command("reject")
@handle
def absence_reject(proposal_id: int, teacher_id: Optional[int] = None):
    """Reject a pending proposal. (An approved one is reverted with `absence undo`.)"""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        p = s.get(Proposal, proposal_id)
        if p is None:
            raise PlanningError(f"No proposal {proposal_id}.")
        reject_proposal(s, p)
        record_touch(s, t.id, "absence", "decision", "reject proposal")
        say("Rejected.")


@absence_app.command("undo")
@handle
def absence_undo(proposal_id: int, teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Propose reverting an approved proposal (approve the result to apply it)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        p = s.get(Proposal, proposal_id)
        if p is None:
            raise PlanningError(f"No proposal {proposal_id}.")
        u = propose_undo(s, p)
        record_touch(s, t.id, "absence", "click", "request undo")
        _out(out, _render(s, u), f"proposal_{u.id}.md")


@absence_app.command("cancel")
@handle
def absence_cancel(absence_id: int, teacher_id: Optional[int] = None):
    """Withdraw an absence. If it was approved, proposals return its days to regular teaching."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        ab = s.get(AbsenceEvent, absence_id)
        if ab is None:
            raise PlanningError(f"No absence {absence_id}.")
        props = cancel_absence(s, ab)
        record_touch(s, t.id, "absence", "decision", "cancel absence")
        say(f"Absence {ab.id} cancelled." if not props else f"{len(props)} proposal(s) return its days to teaching; approve with `lessonbridge absence approve --all {ab.id}`.")


@absence_app.command("set-activity")
@handle
def absence_set_activity(proposal_id: int, on: str, activity: str, teacher_id: Optional[int] = None):
    """Choose a different replacement activity for one day of a pending proposal."""
    from .curriculum.editing import set_proposal_replacement

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        p = s.get(Proposal, proposal_id)
        if p is None:
            raise PlanningError(f"No proposal {proposal_id}.")
        set_proposal_replacement(s, p, _date(on), activity)
        record_touch(s, t.id, "absence", "correction", "change activity")
        say("Updated.")


@absence_app.command("subplans")
@handle
def absence_subplans(absence_id: int, out: Optional[Path] = typer.Option(None, help="Directory for one Markdown file per course-day"), days: Optional[int] = None,
                     only_stale: bool = typer.Option(False, help="Regenerate only plans whose day changed")):
    """Generate substitute plans, one per course-day, each validated before it is stored."""
    ids = generate_plans_for_absence(absence_id, days_limit=days, llm=LLMClient(settings), cfg=settings, only_stale=only_stale)
    with db.session_scope() as s:
        plans = [s.get(SubPlan, i) for i in ids]
        ab = s.get(AbsenceEvent, absence_id)
        record_touch(s, ab.teacher_id, "absence", "click", "generate plans")
        ok = sum(1 for p in plans if p.status.value == "accepted")
        say(f"Generated {len(plans)} plan(s) ({ok} passed validation) using {sorted({p.generator for p in plans})}.")
        for p in plans:
            if out:
                _out(out, p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
            else:
                say(p.rendered_markdown)


@absence_app.command("brief")
@handle
def absence_brief(absence_id: int, out: Optional[Path] = None):
    """Return-to-school brief: completed, moved, skipped; where to resume."""
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        if ab is None:
            raise PlanningError(f"No absence {absence_id}.")
        _out(out, md.render_return_brief(leave_mod.return_brief(s, ab)), f"return_brief_{ab.id}.md")


@absence_app.command("list")
@handle
def absence_list(teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        table = Table("id", "type", "sub", "start", "end", "status", "proposals", "plans", "stale")
        for a in t.absences:
            table.add_row(str(a.id), a.absence_type.value, a.substitute_type.value, str(a.start_date), str(a.end_date), a.status.value,
                          ", ".join(f"{p.id}:{p.status.value}" for p in a.proposals), str(len(a.sub_plans)), str(sum(1 for p in a.sub_plans if p.status.value == "stale")))
        console.print(table)


# ------------------------------------------------------------------ backlog
@backlog_app.command("list")
@handle
def backlog_list(teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        for sec in t.sections:
            for o in sec.owed:
                if o.open:
                    say(f"P{sec.period} {o.title} [{o.kind}] — {o.reason}")


@backlog_app.command("schedule")
@handle
def backlog_schedule(section: str, start: Optional[str] = None, include_dropped: bool = False, teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Propose days for a section's owed lessons."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        p = propose_backlog_placement(s, sec, start=_date(start) if start else None, include_dropped=include_dropped)
        record_touch(s, t.id, "calendar", "click", "schedule owed lessons")
        _out(out, _render(s, p), f"proposal_{p.id}.md")


# ---------------------------------------------------------------- documents
@documents_app.command("add")
@handle
def documents_add(path: Path, doc_type: str = typer.Option("syllabus", "--type", help="syllabus | teacher_pacing_guide | lesson_calendar | classroom_procedures | other"),
                  subject: Optional[str] = None, new_document: bool = typer.Option(False, help="Store as a new document even if one with this name exists"), teacher_id: Optional[int] = None):
    """Add a document after onboarding: rules are read at once, a new curriculum arrives as rebuild proposals."""
    from .curriculum.editing import interpret_upload
    from .ingestion.pipeline import ingest_bytes

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        dt = DocumentType(doc_type)
        res = ingest_bytes(s, data=path.read_bytes(), name=path.name, kind=DocumentKind.teacher, doc_type=dt, title=path.stem.replace("_", " ").title(),
                           scope=f"teacher:{t.id}", subject=subject, as_new_version=not new_document, cfg=settings)
        record_touch(s, t.id, "documents", "field", f"upload {dt.value}")
        out = interpret_upload(s, t, res.version.parsed_text, dt, subject=subject, llm=LLMClient(settings), label=path.name)
        say(f"Stored '{res.document.title}' (version {res.version.version_no}). Rules read: {len(out['rules'])}. Proposals: {out['proposals'] or 'none'}.")
        for n in out["notes"]:
            say(f"  {n}")


# -------------------------------------------------------------------- leave
@leave_app.command("plan")
@handle
def leave_plan(absence_id: int, out: Path = typer.Option(Path("out"), help="Directory for the packet"), generate_plans: bool = typer.Option(True, help="Generate the first 10 days of detailed plans")):
    """Extended leave: pre-leave analysis, drafted first days, handoff packet with 10 detailed days, weekly frameworks."""
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        if ab is None:
            raise PlanningError(f"No absence {absence_id}.")
        pre = leave_mod.pre_leave_analysis(s, ab)
        drafted = leave_mod.draft_placeholders(s, ab, llm=LLMClient(settings))
        _out(out, md.render_pre_leave(pre), f"leave_{ab.id}_pre_leave.md")
        record_touch(s, ab.teacher_id, "leave", "click", "build leave packet")
    ids = generate_plans_for_absence(absence_id, days_limit=10, llm=LLMClient(settings), cfg=settings) if generate_plans else []
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        plans = [s.get(SubPlan, i) for i in ids]
        packet = leave_mod.build_handoff(s, ab, {p.calendar_entry_id: p.id for p in plans})
        _out(out, md.render_handoff(packet, ab.leave_plan.pre_leave), f"leave_{ab.id}_handoff.md")
        for p in plans:
            _out(out / "daily_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
        weeks = leave_mod.weekly_frameworks(s, ab)
        _out(out, md.render_weekly(weeks), f"leave_{ab.id}_weekly_frameworks.md")
        say(f"Leave packet written: {len(drafted)} drafted day(s), {len(plans)} detailed plans, {len(weeks)} weekly frameworks.")


# ----------------------------------------------------------------- progress
class StatusOption(str, enum.Enum):
    completed = "completed"
    skipped = "skipped"
    planned = "planned"


@progress_app.command("mark")
@handle
def progress_mark(section: str, on: str, status: StatusOption, note: str = "", teacher_id: Optional[int] = None):
    """Record what happened on a course-day (used by the return brief)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        e = mark_progress(s, sec, _date(on), status.value, note)
        record_touch(s, t.id, "progress", "field", "mark progress")
        say(f"{sec.section_name} {e.date}: {e.title} → {e.status.value}")


@progress_app.command("slip")
@handle
def progress_slip(section: str, on: str, days: int = 1, reason: str = "lesson took longer than planned", teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """A lesson ran long: propose continuation days that absorb the nearest slack instead of shifting everything."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        p = reconcile_slip(s, sec, _date(on), days, reason)
        record_touch(s, t.id, "progress", "field", "report slip")
        _out(out, _render(s, p), f"proposal_{p.id}.md")


# --------------------------------------------------------------------- misc
@app.command()
@handle
def search(query: str, limit: int = 8):
    """Search the indexed public and teacher documents."""
    with db.session_scope() as s:
        for h in fts_search(s, query, limit=limit):
            say(f"{h.title} ({h.doc_type}, {h.scope})\n  {h.snippet}")


def _is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@app.command()
@handle
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the web interface (local only by default; any other address requires the printed launch token)."""
    import uvicorn

    from .web.app import create_app

    token = None if _is_loopback(host) else secrets.token_urlsafe(24)
    if token:
        say(f"Listening on {host}. Open http://{host}:{port}/?token={token} — every other request without this browser's token is refused.", "yellow")
    uvicorn.run(create_app(token=token, extra_hosts=[] if _is_loopback(host) else [host]), host=host, port=port)


@app.command()
@handle
def demo(out: Path = typer.Option(Path("out/demo"), help="Output folder (must be empty or new)"), force: bool = typer.Option(False, help="If the folder is not empty, write into a new run-* subfolder inside it"),
         today: str = "2026-10-05"):
    """End-to-end walkthrough in its own database: onboard, a one-day absence, the maternity leave, and a slip. Never deletes anything (LB-19)."""
    if out.exists() and any(out.iterdir()):
        if not force:
            raise PlanningError(f"{out} is not empty; choose another --out or pass --force to write into a new subfolder (nothing is deleted).")
        out = out / f"run-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    settings.data_dir = out / "data"
    db.init_engine(settings)
    profile = Path(__file__).resolve().parent.parent / "examples" / "teacher_profile.yaml"
    onboard(profile, today=today)
    with db.session_scope() as s:
        t = _teacher(s, None)
        ab = create_absence(s, t.id, date(2026, 10, 26), date(2026, 10, 26), reason="sick day")
        props = plan_absence(s, ab)
        _out(out, "\n\n".join(_render(s, p) for p in props), "1_one_day_absence_proposals.md")
        apply_all_for_absence(s, ab, acknowledge=True)
        one_day = ab.id
    for pid in generate_plans_for_absence(one_day, llm=LLMClient(settings), cfg=settings):
        with db.session_scope() as s:
            p = s.get(SubPlan, pid)
            _out(out / "one_day_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
    with db.session_scope() as s:
        t = _teacher(s, None)
        for sec in t.sections:
            mark_progress(s, sec, date(2026, 10, 26), "completed", "sub day")
        _out(out, md.render_return_brief(leave_mod.return_brief(s, s.get(AbsenceEvent, one_day))), "2_one_day_return_brief.md")
        leave = create_absence(s, t.id, date(2026, 11, 9), date(2027, 1, 29), substitute_type="long_term_sub", reason="maternity leave")
        plan_absence(s, leave)
        apply_all_for_absence(s, leave, acknowledge=True)
        pre = leave_mod.pre_leave_analysis(s, leave)
        leave_mod.draft_placeholders(s, leave, llm=LLMClient(settings))
        _out(out, md.render_pre_leave(pre), "3_leave_pre_leave_analysis.md")
        leave_id = leave.id
    ids = generate_plans_for_absence(leave_id, days_limit=10, llm=LLMClient(settings), cfg=settings)
    with db.session_scope() as s:
        leave = s.get(AbsenceEvent, leave_id)
        plans = [s.get(SubPlan, i) for i in ids]
        packet = leave_mod.build_handoff(s, leave, {p.calendar_entry_id: p.id for p in plans})
        _out(out, md.render_handoff(packet, leave.leave_plan.pre_leave), "4_leave_handoff.md")
        for p in plans:
            _out(out / "leave_daily_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
        _out(out, md.render_weekly(leave_mod.weekly_frameworks(s, leave)), "5_leave_weekly_frameworks.md")
        t = _teacher(s, None)
        civ = next(sec for sec in t.sections if sec.course.subject.value == "civics")
        slip_entry = next(e for e in entries_between(s, civ.id, date(2026, 11, 30), date(2027, 1, 15)) if e.lesson is not None and not e.lesson.is_assessment)
        slip = reconcile_slip(s, civ, slip_entry.date, 1)
        _out(out, _render(s, slip), "6_slip_proposal.md")
        console.rule("Demo complete")
        say(f"Teacher touches (all workflows): {count_touches(s, t.id)}. Outputs and the demo database are in {out}/")


if __name__ == "__main__":
    app()
