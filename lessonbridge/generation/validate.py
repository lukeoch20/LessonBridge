"""Daily-plan quality gate. Every generated plan passes these checks or is regenerated with feedback."""
from __future__ import annotations

import re
from typing import Any

from ..schemas import SubPlanContent, ValidationResult

CLASSROOM_STAPLES = {"paper", "pencils", "pencil", "pens", "whiteboard", "board", "seating chart", "attendance sheet", "timer", "clock", "notebook", "notebooks", "independent reading book", "independent reading books", "chromebooks", "chromebook", "textbook"}
NEW_CONTENT_PHRASES = ("teach ", "lecture", "introduce new", "direct instruction on", "explain the concept", "model how to")
MINUTES_RE = re.compile(r"\((\d+)\s*min(?:ute)?s?\)|\b(\d+)\s*min(?:ute)?s?\b", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _words(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) > 3}


PAREN_MINUTES_RE = re.compile(r"\((\d+)\s*min(?:ute)?s?\)")


def step_minutes(step: str) -> int:
    """Minutes allotted to a step: the trailing '(N min)' marker wins over durations mentioned in the text."""
    paren = PAREN_MINUTES_RE.findall(step)
    if paren:
        return int(paren[-1])
    matches = MINUTES_RE.findall(step)
    if not matches:
        return 0
    g1, g2 = matches[-1]
    return int(g1 or g2)


def validate_plan(plan: SubPlanContent, ctx: dict[str, Any]) -> ValidationResult:
    """``ctx`` is the same context dict the generator received."""
    failures: list[str] = []
    warnings: list[str] = []

    # 1. Uses only available materials.
    available = {_norm(m) for m in ctx.get("available_materials", [])} | {_norm(s) for s in CLASSROOM_STAPLES}
    for m in plan.materials:
        nm = _norm(m)
        if not nm:
            continue
        if nm in available or any(nm in a or a in nm for a in available if a):
            continue
        failures.append(f"Material not available: '{m}'")

    # 2. Fits class duration and steps are timed and sum correctly.
    minutes = int(ctx["class_minutes"])
    if plan.total_minutes != minutes:
        failures.append(f"total_minutes is {plan.total_minutes}; class is {minutes} minutes")
    timed = [step_minutes(s) for s in plan.instructions]
    if not plan.instructions:
        failures.append("No instruction steps")
    elif any(t == 0 for t in timed):
        failures.append("Every instruction step must state its minutes, e.g. '(10 min)'")
    elif sum(timed) != minutes:
        failures.append(f"Instruction steps sum to {sum(timed)} minutes, not {minutes}")

    # 3. Covers the objective.
    objective_words = _words(ctx.get("objective", ""))
    plan_words = _words(plan.objective) | _words(" ".join(plan.instructions))
    if objective_words and len(objective_words & plan_words) < max(1, len(objective_words) // 4):
        failures.append("Plan does not address the lesson objective")

    # 4. Appropriate for substitute type.
    if ctx.get("substitute_type") == "any_sub":
        bad = [s for s in plan.instructions if any(p in s.lower() for p in NEW_CONTENT_PHRASES)]
        if bad:
            failures.append("A day-to-day substitute should not teach new content: " + bad[0][:80])

    # 5. Does not invent assignments: deliverable must match an expected output.
    expected = [_norm(o) for o in ctx.get("expected_outputs", [])]
    if expected:
        d = _norm(plan.student_deliverable)
        if not any(e in d or d in e or len(_words(e) & _words(d)) >= 1 for e in expected if e):
            failures.append(f"Student deliverable '{plan.student_deliverable}' is not one of the expected outputs {ctx.get('expected_outputs')}")

    # 6. Includes applicable classroom rules verbatim.
    rule_texts = [r["text"] for r in ctx.get("rules", [])]
    present = {_norm(r) for r in plan.classroom_rules}
    for r in rule_texts:
        if _norm(r) not in present:
            failures.append(f"Missing classroom rule: '{r[:70]}'")

    # 7. Timed routines from structured rules (e.g. pre-quiz study period).
    for r in ctx.get("rules", []):
        st = r.get("structured") or {}
        if st.get("action") == "study_period" and ctx.get("is_assessment"):
            need = int(st.get("minutes", 0))
            ok = any(("study" in s.lower() or "review" in s.lower()) and step_minutes(s) >= need for s in plan.instructions)
            if not ok:
                failures.append(f"Quiz day requires a {need}-minute study period as its own step")

    # 8. Respects schedule.
    if not ctx.get("is_meeting_day", True):
        failures.append("Section does not meet on this date")

    # Warnings (do not fail).
    if len(plan.instructions) > 10:
        warnings.append("More than ten steps; consider consolidating")
    if not plan.what_to_collect:
        warnings.append("Nothing listed to collect")
    return ValidationResult(passed=not failures, failures=failures, warnings=warnings)
