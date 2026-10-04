"""Markdown rendering for substitute plans, proposals, briefs and packets."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from ..schemas import DiffItem, ReturnBrief, SubPlanContent, ValidationResult, WeeklyFramework


def render_sub_plan(plan: SubPlanContent, *, teacher_name: str = "", validation: Optional[ValidationResult] = None) -> str:
    d = plan.date if isinstance(plan.date, date) else date.fromisoformat(str(plan.date))
    lines = [
        "# LESSONBRIDGE SUBSTITUTE PLAN",
        "",
        f"**Date:** {d:%A, %B %d, %Y}",
        f"**Period / Course:** Period {plan.period} — {plan.course}" + (f" (for {teacher_name})" if teacher_name else ""),
        f"**Substitute type:** {plan.substitute_type.replace('_', ' ')}",
        f"**Class length:** {plan.total_minutes} minutes" + (f" · Standards: {', '.join(plan.standards)}" if plan.standards else ""),
        "",
        "## Objective",
        plan.objective,
        "",
        "## Materials",
        *[f"- {m.replace('_', ' ')}" for m in plan.materials],
        "",
        "## Instructions",
        *plan.instructions,
        "",
        "## Student Deliverable",
        plan.student_deliverable,
        "",
        "## What to Collect",
        *[f"- {c}" for c in plan.what_to_collect],
        "",
        "## Early-Finisher Activity",
        plan.early_finisher_activity,
        "",
        "## Classroom-Specific Rules",
        *[f"- {r}" for r in plan.classroom_rules],
        "",
        "## Notes for Next Day",
        plan.notes_for_next_day,
    ]
    if validation and not validation.passed:
        lines += ["", "> **Validation warnings (plan needs teacher review):**", *[f"> - {f}" for f in validation.failures]]
    return "\n".join(lines) + "\n"


def render_diff(diff: Iterable[DiffItem], *, title: str = "PROPOSED CALENDAR CHANGES") -> str:
    items = list(diff)
    lines = [f"## {title}", "", "| Date | Original | Proposed | Action | Why |", "|---|---|---|---|---|"]
    unchanged = [it for it in items if it.action == "keep" and not it.is_sub_day]
    collapse = len(unchanged) > 3
    if collapse:
        items = [it for it in items if not (it.action == "keep" and not it.is_sub_day)]
    for it in items:
        if it.action in ("drop", "defer"):
            lines.append(f"| — | {it.before_title or ''} | *{it.action}* | {it.action} | {it.reason} |")
            continue
        after = (it.after_title or "—") + (" **[SUB]**" if it.is_sub_day and "[SUB]" not in (it.after_title or "") else "")
        lines.append(f"| {it.date:%a %b %d} | {it.before_title or '—'} | {after} | {it.action} | {it.reason} |")
    if collapse:
        lines.append(f"| … | | *{len(unchanged)} other day(s) unchanged* | keep | |")
    return "\n".join(lines) + "\n"


def render_proposal(explanation: str, diff: Iterable[DiffItem], constraints: dict | None = None, *, heading: str) -> str:
    out = [f"# {heading}", "", explanation, "", render_diff(diff)]
    if constraints:
        if constraints.get("hard"):
            out += ["**Hard constraints respected:**", *[f"- {h}" for h in constraints["hard"]], ""]
        if constraints.get("soft"):
            out += ["**Soft-constraint notes:**", *[f"- {s}" for s in constraints["soft"]], ""]
        if constraints.get("unresolved"):
            out += ["**Needs your decision:**", *[f"- {u}" for u in constraints["unresolved"]], ""]
    return "\n".join(out)


def render_return_brief(brief: ReturnBrief) -> str:
    lines = ["# RETURN BRIEF", "", f"Absence {brief.start_date} to {brief.end_date}", ""]
    for s in brief.sections:
        lines += [f"## {s.course}", ""]
        for label, rows in (("Completed", s.completed), ("Moved", s.moved), ("Skipped", s.skipped), ("Assessment status", s.assessment_status), ("Outstanding work", s.outstanding_work)):
            lines.append(f"**{label}:**" + (" none" if not rows else ""))
            lines += [f"- {r}" for r in rows]
        lines += [f"**Recommended re-entry point:** {s.recommended_reentry_point}", "", "**Suggested first week back:**", *[f"- {r}" for r in s.first_week_back], ""]
    lines += ["## Calendar Changes", *([f"- {c}" for c in brief.calendar_changes] or ["- none"]), "", "## Follow-Up", *([f"- {f}" for f in brief.follow_up] or ["- none"]), ""]
    return "\n".join(lines)


def render_weekly(frameworks: Iterable[WeeklyFramework]) -> str:
    lines = ["# WEEKLY PACING FRAMEWORKS (long-term substitute)", ""]
    for f in frameworks:
        lines += [f"## Week of {f.week_start:%b %d} — {f.course}", "", f"**Units:** {', '.join(f.unit_titles) or '—'}", "", "**Objectives:**", *[f"- {o}" for o in f.objectives], "", "**Required assessments:** " + (", ".join(f.required_assessments) or "none"), "**Deadlines:** " + (", ".join(f.deadlines) or "none"), "**Standards:** " + (", ".join(f.standards) or "—"), "", "**Suggested sequence:**", *[f"- {s}" for s in f.suggested_sequence], "", "**Materials:** " + (", ".join(m.replace('_', ' ') for m in f.materials) or "—"), "", "**Constraints:**", *[f"- {c}" for c in f.constraints], "", f"_{f.flexibility_notes}_", ""]
    return "\n".join(lines)


def render_handoff(packet: dict, pre_leave: dict | None = None) -> str:
    lines = ["# LONG-TERM SUBSTITUTE HANDOFF", "", f"Leave begins {packet.get('start')}. Detailed daily plans cover the first {packet.get('detailed_days')} instructional days; weekly frameworks follow.", ""]
    if pre_leave:
        lines += ["## Hard deadlines during leave", *[f"- {d}" for d in pre_leave.get("hard_deadlines", [])], ""]
    lines += ["## Classroom rules", *[f"- {r}" for r in packet.get("classroom_rules", [])], "", "## Flexibility boundaries", *[f"- {b}" for b in packet.get("flexibility_boundaries", [])], ""]
    for s in packet.get("sections", []):
        lines += [f"## {s['section']} (Period {s['period']}, {s['minutes']} min)", ""]
        for title, u in s.get("unit_context", {}).items():
            lines += [f"**Unit context — {title}:** {u.get('summary', '')} Standards: {', '.join(u.get('standards', []))}"]
        lines += ["", "**First days:**", *[f"- {d['date']}: {d['title']}" + (" (detailed plan attached)" if d.get('sub_plan_id') else "") for d in s.get("days", [])], ""]
        if s.get("assessment_guidance"):
            lines += ["**Assessment guidance:**", *[f"- {a}" for a in s["assessment_guidance"]], ""]
    return "\n".join(lines)


def render_calendar(entries, *, title: str) -> str:
    lines = [f"## {title}", "", "| Date | Day | Entry | Unit | Status |", "|---|---|---|---|---|"]
    for e in entries:
        flag = " **[SUB]**" if (e.is_sub_day and "[SUB]" not in e.title) else ""
        lines.append(f"| {e.date} | {e.date:%a} | {e.title}{flag} | {e.unit.title if e.unit else ''} | {e.status.value} |")
    return "\n".join(lines) + "\n"


def render_pre_leave(analysis: dict) -> str:
    def bullets(rows: list[str]) -> list[str]:
        return [f"  - {a}" for a in rows] or ["  - none"]

    lines = ["# PRE-LEAVE ANALYSIS", "", f"Leave window: {analysis['leave_window']['start']} to {analysis['leave_window']['end']}", "", "## Hard deadlines", *[f"- {d}" for d in analysis.get("hard_deadlines", [])], ""]
    for s in analysis.get("sections", []):
        lines += [f"## {s['section']}", f"- Course-days during leave: {s['course_days_during_leave']}", f"- Units: {', '.join(s['units']) or '—'}"]
        lines += ["- Assessments during leave:", *bullets(s["assessments"])]
        lines += ["- Lessons that need the regular teacher (sensitive dependencies):", *bullets(s["sensitive_dependencies"])]
        lines += ["- Advantageous to complete before leave:", *bullets(s["complete_before_leave"])]
        und = s.get("undetailed_days", [])
        lines += [f"- Days during leave not yet detailed (teacher should fill or let LessonBridge draft): {len(und)}", *([f"  - {u}" for u in und[:8]] + ([f"  - … {len(und) - 8} more"] if len(und) > 8 else [])), ""]
    return "\n".join(lines)
