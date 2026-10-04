"""LessonBridge command-line interface."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from . import db
from .absence import leave as leave_mod
from .absence.service import apply_proposal, create_absence, mark_progress, plan_absence, reconcile_slip, reject_proposal
from .config import settings
from .curriculum.calendar import entries_between
from .generation.llm import LLMClient
from .generation.subplan import generate_sub_plans
from .ingestion.index import search as fts_search
from .models import AbsenceEvent, Proposal, ProposalStatus, SchoolCalendarEvent, SubPlan, Teacher, TeacherCourse, TeacherRule
from .profile.onboarding import OnboardingInput, confirm_calendar, count_touches, ensure_public_context, record_touch, run_onboarding
from .profile.rules import add_rule
from .render import markdown as md
from .schemas import DiffItem

app = typer.Typer(help="LessonBridge: instructional continuity for teachers.", no_args_is_help=True)
calendar_app = typer.Typer(help="Instructional calendar")
absence_app = typer.Typer(help="Absences, proposals and substitute plans")
leave_app = typer.Typer(help="Extended-leave planning")
rules_app = typer.Typer(help="Classroom rules")
progress_app = typer.Typer(help="Record what actually happened")
app.add_typer(calendar_app, name="calendar")
app.add_typer(absence_app, name="absence")
app.add_typer(leave_app, name="leave")
app.add_typer(rules_app, name="rules")
app.add_typer(progress_app, name="progress")
console = Console()


def _teacher(session, teacher_id: Optional[int]) -> Teacher:
    if teacher_id:
        t = session.get(Teacher, teacher_id)
    else:
        t = session.scalar(select(Teacher).order_by(Teacher.id.desc()))
    if t is None:
        raise typer.BadParameter("No teacher found; run `lessonbridge onboard` first.")
    return t


def _section(session, teacher: Teacher, section: Optional[str]) -> TeacherCourse:
    if section is None:
        return teacher.sections[0]
    if section.isdigit():
        for s in teacher.sections:
            if s.id == int(s.id) and str(s.id) == section:
                return s
    for s in teacher.sections:
        if section.lower() in s.section_name.lower() or section == s.period:
            return s
    raise typer.BadParameter(f"Unknown section {section!r}. Known: {[s.section_name for s in teacher.sections]}")


def _out(path: Optional[Path], text: str, default_name: str) -> None:
    if path is None:
        console.print(text)
        return
    target = path / default_name if path.suffix == "" else path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    console.print(f"[green]wrote[/green] {target}")


@app.callback()
def _init(data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Where the database and documents live (default ./data)")):
    if data_dir:
        settings.data_dir = data_dir
    db.init_engine(settings)


@app.command()
def init(force: bool = typer.Option(False, help="Re-fetch public sources even if not due")):
    """Create the database and load LCPS public context (calendar, standards, curriculum)."""
    from .ingestion.pipeline import register_public_sources
    from .providers import get_provider

    with db.session_scope() as s:
        provider = get_provider("lcps")
        results = register_public_sources(s, provider, force=force, cfg=settings)
        from .ingestion.pipeline import sync_calendar_events

        n = sync_calendar_events(s, provider, "2026-2027")
        console.print(f"Public sources touched: {len(results)}; calendar events added: {n}")
        console.print(f"Network access: {'on' if settings.allow_network else 'off'}; generator: {'Claude (' + settings.model + ')' if LLMClient(settings).available() else 'template (no Claude credentials found)'}")


@app.command()
def onboard(profile: Path = typer.Argument(..., help="YAML onboarding profile (see examples/teacher_profile.yaml)"), today: Optional[str] = typer.Option(None, help="Override today's date (YYYY-MM-DD) for planning horizons")):
    """Build a teacher's instructional context from public sources plus their documents."""
    inp = OnboardingInput.from_yaml(profile)
    with db.session_scope() as s:
        res = run_onboarding(s, inp, cfg=settings, llm=LLMClient(settings), today=date.fromisoformat(today) if today else None)
        console.rule("[bold]Onboarding complete")
        console.print(f"Teacher id: {res.teacher_id}   teacher touches: {res.touches}")
        console.print(f"Public sources loaded: {', '.join(res.public_sources) or 'already current'}")
        console.print(f"Calendar dates needing confirmation: {len(res.flagged_events)} (run `lessonbridge calendar confirm`)")
        for subj, src in res.curriculum_sources.items():
            console.print(f"  {subj}: curriculum from {src}")
        for name, rep in res.build_reports.items():
            console.print(f"  {name}: {rep.entries_created} course-days planned, {rep.entries_created - rep.flex_days} lessons placed, {rep.flex_days} flex / to-be-detailed days" + (f", {len(rep.quarter_overruns)} quarter overruns" if rep.quarter_overruns else ""))
            for o in rep.quarter_overruns[:3]:
                console.print(f"    [yellow]overrun[/yellow] {o}")
        for c in res.curriculum_conflicts:
            console.print(f"  [yellow]conflict[/yellow] {c}")
        for n in res.curriculum_notes:
            console.print(f"  note: {n}")
        if res.extracted_rules:
            console.print(f"Rules learned from documents: {len(res.extracted_rules)}")
            for r in res.extracted_rules[:8]:
                console.print(f"  - {r}")
        for w in res.warnings:
            console.print(f"[red]{w}[/red]")


@app.command()
def status(teacher_id: Optional[int] = None):
    """Show the teacher profile, sections, pending proposals and effort metric."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        console.print(f"[bold]{t.name}[/bold] — {t.school.name} ({t.school.district_name}), grade {t.grade}, {t.school_year}")
        table = Table("id", "section", "period", "days", "min", "units", "course-days")
        for sec in t.sections:
            table.add_row(str(sec.id), sec.section_name, sec.period, "".join("MTWRF"[d - 1] for d in sec.meeting_days), str(sec.minutes_per_meeting), str(len(sec.units)), str(len(sec.calendar)))
        console.print(table)
        pend = list(s.scalars(select(Proposal).where(Proposal.teacher_id == t.id, Proposal.status == ProposalStatus.pending)))
        console.print(f"Pending proposals: {len(pend)}   rules: {len(t.rules)}   absences: {len(t.absences)}   teacher touches (all workflows): {count_touches(s, t.id)}")
        flagged = s.scalar(select(SchoolCalendarEvent).where(SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False)))
        if flagged:
            console.print("[yellow]School calendar dates are still provisional; run `lessonbridge calendar confirm`.[/yellow]")


# ----------------------------------------------------------------- calendar
@calendar_app.command("show")
def calendar_show(section: Optional[str] = typer.Option(None, help="Section name fragment or period"), start: Optional[str] = None, end: Optional[str] = None, teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Print the instructional calendar for a section."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        a = date.fromisoformat(start) if start else date.today()
        b = date.fromisoformat(end) if end else a + timedelta(days=28)
        entries = entries_between(s, sec.id, a, b)
        _out(out, md.render_calendar(entries, title=f"{sec.section_name}: {a} to {b}"), f"calendar_{sec.id}.md")


@calendar_app.command("events")
def calendar_events(school_year: str = "2026-2027"):
    """List school calendar events and whether they are confirmed."""
    with db.session_scope() as s:
        table = Table("id", "date", "type", "title", "status")
        for e in s.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.school_year == school_year).order_by(SchoolCalendarEvent.date)):
            table.add_row(str(e.id), f"{e.date}" + (f" → {e.end_date}" if e.end_date else ""), e.event_type.value, e.title, "confirmed" if e.confirmed else ("[yellow]provisional[/yellow]" if e.needs_confirmation else "ok"))
        console.print(table)


@calendar_app.command("confirm")
def calendar_confirm(ids: Optional[str] = typer.Option(None, help="Comma-separated event ids; default all"), teacher_id: Optional[int] = None, school_year: str = "2026-2027"):
    """Confirm provisional school-calendar dates (one teacher touch for the whole batch)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        n = confirm_calendar(s, t.id, school_year, event_ids=[int(x) for x in ids.split(",")] if ids else None)
        console.print(f"Confirmed {n} dates.")


@calendar_app.command("rebuild")
def calendar_rebuild(section: Optional[str] = None, start: Optional[str] = None, teacher_id: Optional[int] = None):
    """Rebuild the planned calendar for a section from its curriculum sequence."""
    from .curriculum.calendar import build_section_calendar

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        rep = build_section_calendar(s, sec, start=date.fromisoformat(start) if start else None)
        console.print(f"{sec.section_name}: {rep.entries_created} course-days, {rep.flex_days} flex; conflicts: {rep.conflicts}; overruns: {len(rep.quarter_overruns)}")


# ------------------------------------------------------------------- rules
@rules_app.command("add")
def rules_add(text: str, teacher_id: Optional[int] = None, scope: str = "teacher"):
    """Add a classroom rule (free text; LessonBridge infers the structured form)."""
    from .models import RuleScope

    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        r = add_rule(s, t.id, text, scope=RuleScope(scope))
        record_touch(s, t.id, "rules", "field", "add rule")
        console.print(f"Rule {r.id} [{r.category}] {r.structured}")


@rules_app.command("list")
def rules_list(teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        table = Table("id", "scope", "category", "text", "structured", "source", "active")
        for r in s.scalars(select(TeacherRule).where(TeacherRule.teacher_id == t.id).order_by(TeacherRule.id)):
            table.add_row(str(r.id), r.scope.value, r.category, r.text, json.dumps(r.structured), r.source, "yes" if r.active else "superseded")
        console.print(table)


# ---------------------------------------------------------------- absences
@absence_app.command("plan")
def absence_plan(start: str, end: Optional[str] = None, substitute: Optional[str] = typer.Option(None, help="any_sub | long_term_sub"), reason: str = "", teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """Report an absence: run the decision engine and reconcile the calendar into a proposal (not applied until approved)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        a = date.fromisoformat(start)
        b = date.fromisoformat(end) if end else a
        ab = create_absence(s, t.id, a, b, substitute_type=substitute, reason=reason)
        record_touch(s, t.id, "absence", "field", "report absence")
        props = plan_absence(s, ab)
        console.print(f"Absence {ab.id}: {ab.absence_type.value}, substitute: {ab.substitute_type.value}, {a} → {b}")
        text = []
        for p in props:
            sec = s.get(TeacherCourse, p.teacher_course_id)
            text.append(md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} — {sec.section_name}"))
        _out(out, "\n\n".join(text), f"absence_{ab.id}_proposals.md")
        console.print(f"[bold]{len(props)} proposal(s) pending.[/bold] Approve with `lessonbridge absence approve <id>` or `--all {ab.id}`.")


@absence_app.command("approve")
def absence_approve(proposal_id: Optional[int] = typer.Argument(None), all_for: Optional[int] = typer.Option(None, "--all", help="Approve every pending proposal for this absence id"), teacher_id: Optional[int] = None):
    """Approve a proposal; the calendar diff becomes authoritative."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        if all_for is not None:
            props = list(s.scalars(select(Proposal).where(Proposal.absence_id == all_for, Proposal.status == ProposalStatus.pending)))
        else:
            props = [s.get(Proposal, proposal_id)]
        for p in props:
            n = apply_proposal(s, p)
            console.print(f"Proposal {p.id} approved: {n} calendar rows changed.")
        record_touch(s, t.id, "absence", "decision", "approve proposal(s)")


@absence_app.command("reject")
def absence_reject(proposal_id: int, teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        reject_proposal(s, s.get(Proposal, proposal_id))
        record_touch(s, t.id, "absence", "decision", "reject proposal")
        console.print("Rejected.")


@absence_app.command("subplans")
def absence_subplans(absence_id: int, out: Optional[Path] = typer.Option(None, help="Directory to write one Markdown file per course-day"), days: Optional[int] = None):
    """Generate detailed substitute plans, one LLM call per course-day, each validated before it is stored."""
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        plans = generate_sub_plans(s, ab, days_limit=days, llm=LLMClient(settings), cfg=settings)
        ok = sum(1 for p in plans if p.status.value == "accepted")
        console.print(f"Generated {len(plans)} plans ({ok} passed validation) using {sorted({p.generator for p in plans})}.")
        for p in plans:
            fname = f"subplan_{p.date}_P{p.section.period}.md"
            if out:
                _out(out, p.rendered_markdown, fname)
            else:
                console.print(p.rendered_markdown)


@absence_app.command("brief")
def absence_brief(absence_id: int, out: Optional[Path] = None):
    """Return-to-school brief: what was completed, moved, skipped; where to resume."""
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        _out(out, md.render_return_brief(leave_mod.return_brief(s, ab)), f"return_brief_{ab.id}.md")


@absence_app.command("list")
def absence_list(teacher_id: Optional[int] = None):
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        table = Table("id", "type", "sub", "start", "end", "status", "proposals", "plans")
        for a in t.absences:
            table.add_row(str(a.id), a.absence_type.value, a.substitute_type.value, str(a.start_date), str(a.end_date), a.status.value, str(len(a.proposals)), str(len(a.sub_plans)))
        console.print(table)


# ------------------------------------------------------------------- leave
@leave_app.command("plan")
def leave_plan(absence_id: int, out: Path = typer.Option(Path("out"), help="Directory for the packet"), generate_plans: bool = typer.Option(True, help="Generate the first 10 days of detailed plans")):
    """Extended leave: pre-leave analysis, handoff packet with 10 detailed days, weekly frameworks for the rest."""
    with db.session_scope() as s:
        ab = s.get(AbsenceEvent, absence_id)
        pre = leave_mod.pre_leave_analysis(s, ab)
        _out(out, md.render_pre_leave(pre), f"leave_{ab.id}_pre_leave.md")
        plans = generate_sub_plans(s, ab, days_limit=10, llm=LLMClient(settings), cfg=settings) if generate_plans else []
        by_entry = {p.calendar_entry_id: p.id for p in plans}
        packet = leave_mod.build_handoff(s, ab, by_entry)
        _out(out, md.render_handoff(packet, pre), f"leave_{ab.id}_handoff.md")
        for p in plans:
            _out(out / "daily_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
        weeks = leave_mod.weekly_frameworks(s, ab)
        _out(out, md.render_weekly(weeks), f"leave_{ab.id}_weekly_frameworks.md")
        console.print(f"Leave packet written: {len(plans)} detailed plans, {len(weeks)} weekly frameworks.")


# ---------------------------------------------------------------- progress
@progress_app.command("mark")
def progress_mark(section: str, on: str, status: str = typer.Argument(..., help="completed | skipped | planned"), note: str = "", teacher_id: Optional[int] = None):
    """Record what happened on a course-day (used by the return brief)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        e = mark_progress(s, sec, date.fromisoformat(on), status, note)
        console.print(f"{sec.section_name} {e.date}: {e.title} → {e.status.value}")


@progress_app.command("slip")
def progress_slip(section: str, on: str, days: int = 1, reason: str = "lesson took longer than planned", teacher_id: Optional[int] = None, out: Optional[Path] = None):
    """A lesson ran long: propose a repaired schedule from the next day (compress/merge instead of shifting everything)."""
    with db.session_scope() as s:
        t = _teacher(s, teacher_id)
        sec = _section(s, t, section)
        p = reconcile_slip(s, sec, date.fromisoformat(on), days, reason)
        record_touch(s, t.id, "progress", "field", "report slip")
        _out(out, md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} — {sec.section_name}"), f"proposal_{p.id}.md")


# ------------------------------------------------------------------- misc
@app.command()
def search(query: str, limit: int = 8):
    """Search the indexed public and teacher documents."""
    with db.session_scope() as s:
        for h in fts_search(s, query, limit=limit):
            console.print(f"[bold]{h.title}[/bold] ({h.doc_type}, {h.scope})\n  {h.snippet}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000):
    """Run the web interface."""
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(), host=host, port=port)


@app.command()
def demo(out: Path = Path("out/demo"), today: str = "2026-10-05"):
    """End-to-end walkthrough: onboard the sample teacher, plan a one-day absence, approve it, generate plans, then plan the maternity leave."""
    import shutil

    profile = Path(__file__).resolve().parent.parent / "examples" / "teacher_profile.yaml"
    shutil.rmtree(out, ignore_errors=True)
    onboard(profile, today=today)
    with db.session_scope() as s:
        t = _teacher(s, None)
        # One-day absence in the week before the Q1 boundary.
        ab = create_absence(s, t.id, date(2026, 10, 26), date(2026, 10, 26), reason="sick day")
        props = plan_absence(s, ab)
        texts = [md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} — {s.get(TeacherCourse, p.teacher_course_id).section_name}") for p in props]
        _out(out, "\n\n".join(texts), "1_one_day_absence_proposals.md")
        for p in props:
            apply_proposal(s, p)
        plans = generate_sub_plans(s, ab, llm=LLMClient(settings), cfg=settings)
        for p in plans:
            _out(out / "one_day_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
        for sec in t.sections:
            mark_progress(s, sec, date(2026, 10, 26), "completed", "sub day")
        _out(out, md.render_return_brief(leave_mod.return_brief(s, ab)), "2_one_day_return_brief.md")
        # Maternity leave.
        leave = create_absence(s, t.id, date(2026, 11, 9), date(2027, 1, 29), reason="maternity leave")
        lprops = plan_absence(s, leave)
        for p in lprops:
            apply_proposal(s, p)
        pre = leave_mod.pre_leave_analysis(s, leave)
        _out(out, md.render_pre_leave(pre), "3_leave_pre_leave_analysis.md")
        lplans = generate_sub_plans(s, leave, days_limit=10, llm=LLMClient(settings), cfg=settings)
        packet = leave_mod.build_handoff(s, leave, {p.calendar_entry_id: p.id for p in lplans})
        _out(out, md.render_handoff(packet, pre), "4_leave_handoff.md")
        for p in lplans:
            _out(out / "leave_daily_plans", p.rendered_markdown, f"subplan_{p.date}_P{p.section.period}.md")
        _out(out, md.render_weekly(leave_mod.weekly_frameworks(s, leave)), "5_leave_weekly_frameworks.md")
        civ = t.sections[2]
        slip_entry = next(e for e in entries_between(s, civ.id, date(2026, 11, 30), date(2027, 1, 15)) if e.lesson is not None and not e.lesson.is_assessment)
        slip = reconcile_slip(s, civ, slip_entry.date, 1)
        _out(out, md.render_proposal(slip.explanation, [DiffItem(**d) for d in slip.diff], slip.constraints, heading=f"Proposal {slip.id} — slip during leave"), "6_slip_proposal.md")
        console.rule("[bold green]Demo complete")
        console.print(f"Teacher touches so far: {count_touches(s, t.id)}. Outputs in {out}/")


if __name__ == "__main__":
    app()
