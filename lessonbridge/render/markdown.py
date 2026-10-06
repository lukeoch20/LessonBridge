"""Markdown rendering for substitute plans, proposals, briefs and packets."""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from ..schemas import DiffItem, ReturnBrief, SubPlanContent, ValidationResult, WeeklyFramework


def render_sub_plan(plan: SubPlanContent, *, teacher_name: str = "", validation: Optional[ValidationResult] = None, drafted: bool = False) -> str:
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
    if drafted:
        lines += ["", "> This lesson was drafted by LessonBridge to fill a 'lesson to be detailed' day; the teacher should review it."]
    if validation and not validation.passed:
        lines += ["", "> **Validation warnings (plan needs teacher review):**", *[f"> - {f}" for f in validation.failures]]
    return "\n".join(lines) + "\n"


ACTION_LABELS = {
    "keep": "unchanged", "shift": "moved", "carry": "carried to next quarter", "merge": "compressed", "compress": "compressed",
    "replace": "substitute activity", "substitute": "substitute day", "release": "back to regular teaching", "update": "updated", "insert": "added",
}


def render_diff(diff: Iterable[DiffItem], *, title: str = "PROPOSED CALENDAR CHANGES") -> str:
    """Every row approval will write is listed; only rows it leaves untouched are collapsed (LB-04)."""
    items = list(diff)
    lines = [f"## {title}", "", "| Date | Now | After approval | Change | Why |", "|---|---|---|---|---|"]
    unchanged = [it for it in items if not it.changed]
    for it in items:
        if not it.changed:
            continue
        after = (it.after_title or "—") + (" **[SUB]**" if it.is_sub_day and "[SUB]" not in (it.after_title or "") else "")
        before = (it.before_title or "—") + (" [SUB]" if it.before and it.before.get("is_sub_day") and "[SUB]" not in (it.before_title or "") else "")
        lines.append(f"| {it.date:%a %b %d} | {before} | {after} | {ACTION_LABELS.get(it.action, it.action)} | {it.reason} |")
    if unchanged:
        lines.append(f"| … | | *{len(unchanged)} other day(s) in the window are not changed* | | |")
    return "\n".join(lines) + "\n"


def render_backlog(backlog: dict | None, owed_titles: dict[int, str] | None = None) -> str:
    if not backlog:
        return ""
    out = []
    adds = backlog.get("add", [])
    if adds:
        out += ["**Goes to the owed-lesson list (not deleted):**", *[f"- {a['title']} ({a['kind']}: {a.get('reason', '')})" for a in adds], ""]
    if backlog.get("resolve"):
        names = [owed_titles.get(i, f"#{i}") for i in backlog["resolve"]] if owed_titles else [f"#{i}" for i in backlog["resolve"]]
        out += ["**Scheduled from the owed-lesson list:** " + ", ".join(names), ""]
    if backlog.get("reopen"):
        out += [f"**Returned to the owed-lesson list:** {len(backlog['reopen'])} lesson(s)", ""]
    return "\n".join(out)


def render_proposal(explanation: str, diff: Iterable[DiffItem], constraints: dict | None = None, *, heading: str, backlog: dict | None = None, owed_titles: dict[int, str] | None = None) -> str:
    out = [f"# {heading}", "", explanation, "", render_diff(diff)]
    bl = render_backlog(backlog, owed_titles)
    if bl:
        out.append(bl)
    if constraints:
        if constraints.get("violations"):
            out += ["**Blocked by the safety check (cannot be approved):**", *[f"- {v}" for v in constraints["violations"]], ""]
        if constraints.get("decisions_required"):
            out += ["**Needs your explicit decision before approval:**", *[f"- {v}" for v in constraints["decisions_required"]], ""]
        if constraints.get("hard"):
            out += ["**Hard constraints respected:**", *[f"- {h}" for h in constraints["hard"]], ""]
        if constraints.get("soft"):
            out += ["**Soft-constraint notes:**", *[f"- {x}" for x in constraints["soft"]], ""]
        if constraints.get("unresolved"):
            out += ["**Unresolved:**", *[f"- {u}" for u in constraints["unresolved"]], ""]
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
    lines = ["# LONG-TERM SUBSTITUTE HANDOFF", "", f"Leave begins {packet.get('start')}; the teacher returns {packet.get('return', '')}. Detailed daily plans cover the first {packet.get('detailed_days')} school days; weekly frameworks follow.", ""]
    if pre_leave:
        lines += ["## Hard deadlines during leave", *[f"- {d}" for d in pre_leave.get("hard_deadlines", [])], ""]
    rules = packet.get("classroom_rules", [])
    lines += ["## Classroom rules (all of them, and when each applies)", *[f"- {r['text']} — *{r['applies']}*" if isinstance(r, dict) else f"- {r}" for r in rules], ""]
    lines += ["## Flexibility boundaries", *[f"- {b}" for b in packet.get("flexibility_boundaries", [])], ""]
    for s in packet.get("sections", []):
        lines += [f"## {s['section']} (Period {s['period']}, {s['minutes']} min)", ""]
        for title, u in s.get("unit_context", {}).items():
            lines += [f"**Unit context — {title}:** {u.get('summary', '')} Standards: {', '.join(u.get('standards', []))}"]
        if s.get("section_rules"):
            lines += ["", "**Rules for this class:**", *[f"- {r['text']} — *{r['applies']}*" for r in s["section_rules"]]]
        lines += ["", "**First days:**", *[f"- {d['date']}: {d['title']}" + (" *(drafted by LessonBridge — review)*" if d.get("drafted") else "") + (" (detailed plan attached)" if d.get('sub_plan_id') else "") for d in s.get("days", [])], ""]
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
    def bullets(rows: list[str], cap: int = 12) -> list[str]:
        if not rows:
            return ["  - none"]
        out = [f"  - {a}" for a in rows[:cap]]
        if len(rows) > cap:
            out.append(f"  - … {len(rows) - cap} more")
        return out

    w = analysis["leave_window"]
    lines = ["# PRE-LEAVE ANALYSIS", "", f"Leave window: {w['start']} to {w['end']}; return {w.get('return', '')}", "", "## Hard deadlines", *[f"- {d}" for d in analysis.get("hard_deadlines", [])], ""]
    for s in analysis.get("sections", []):
        lines += [f"## {s['section']}", f"- Course-days during leave: {s['course_days_during_leave']}", f"- Units: {', '.join(s['units']) or '—'}"]
        lines += ["- Assessments during leave:", *bullets(s["assessments"])]
        lines += ["- Dependencies that cross the leave start:", *bullets(s["sensitive_dependencies"])]
        lines += ["- Units that straddle the leave start:", *bullets(s.get("straddling_units", []))]
        lines += ["- Multi-day work due during leave:", *bullets(s.get("multi_step_work", []))]
        lines += ["- Advantageous to complete before leave:", *bullets(s["complete_before_leave"])]
        lines += [f"- Days not yet detailed ({len(s.get('undetailed_days', []))}); LessonBridge drafts the first ten leave days when it builds the handoff:", *bullets(s.get("undetailed_days", []), 8), ""]
    return "\n".join(lines)
