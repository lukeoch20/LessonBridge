"""Daily-plan quality gate. Every generated plan passes these checks or is regenerated with feedback."""
from __future__ import annotations

import re
from typing import Any

from ..schemas import SubPlanContent, ValidationResult

# Items any classroom has; listing them is never "inventing" a material.
CLASSROOM_STAPLES = {"paper", "pencils", "pencil", "pens", "pen", "whiteboard", "board", "seating chart", "attendance sheet", "timer", "clock",
                     "notebook", "notebooks", "independent reading book", "independent reading books", "chromebooks", "chromebook", "textbook", "notes", "class list"}
# Resources that must be on the available list if a step uses them (LB-40).
RESOURCE_WORDS = ("video", "youtube", "kahoot", "quizlet", "blooket", "gimkit", "website", "web site", "link", "film", "movie", "podcast", "game",
                  "poster", "newspaper", "magazine", "slideshow", "powerpoint", "worksheet", "handout", "song", "app", "online", "google form", "padlet", "nearpod")
NEW_CONTENT_PHRASES = ("teach ", "teach the", "lecture", "introduce new", "direct instruction on", "explain the concept", "present the lesson", "present new")
PAREN_MINUTES_RE = re.compile(r"\((\d+)\s*min(?:ute)?s?\)")
MINUTES_RE = re.compile(r"\((\d+)\s*min(?:ute)?s?\)|\b(\d+)\s*min(?:ute)?s?\b", re.I)
_GENERIC = {"completed", "complete", "work", "class", "sheet", "student", "students", "final", "practice", "notes", "page", "pages"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower().replace("_", " ")).strip()


def _words(s: str) -> set[str]:
    return {w for w in _norm(s).split() if len(w) > 3}


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


def _material_ok(item: str, available: set[str]) -> bool:
    n = _norm(item)
    if not n:
        return True
    if n in available or n in CLASSROOM_STAPLES or n.rstrip("s") in CLASSROOM_STAPLES:
        return True
    # A listed material may be named with an extra qualifier ("federalism slides (printed)").
    return any(a and (n == a or n.startswith(a + " ") or a.startswith(n + " ")) and len(min(n, a, key=len)) > 6 for a in available)


def validate_plan(plan: SubPlanContent, ctx: dict[str, Any]) -> ValidationResult:
    """``ctx`` is the same context dict the generator received."""
    failures: list[str] = []
    warnings: list[str] = []
    available = {_norm(m) for m in ctx.get("available_materials", [])}

    # 1. Uses only available materials: whole-item comparison, not substring (LB-40).
    for m in plan.materials:
        if not _material_ok(m, available):
            failures.append(f"Material not available: '{m}'")
    # 1b. Resources mentioned only in the steps must be available too.
    listed_text = " ".join(available)
    for step in plan.instructions:
        low = _norm(step)
        for word in RESOURCE_WORDS:
            if re.search(rf"\b{re.escape(word)}s?\b", low) and word not in listed_text:
                failures.append(f"Step uses a resource that is not available ({word}): '{step[:60]}'")
                break

    # 2. Fits class duration and steps are timed and sum correctly.
    minutes = int(ctx["class_minutes"])
    if plan.total_minutes != minutes:
        failures.append(f"total_minutes is {plan.total_minutes}; class is {minutes} minutes")
    timed = [step_minutes(s) for s in plan.instructions]
    if not plan.instructions:
        failures.append("No instruction steps")
    elif any(t <= 0 for t in timed):
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

    # 5. Does not invent assignments: the deliverable must be an expected output as a whole phrase (LB-40).
    expected = [_norm(o) for o in ctx.get("expected_outputs", []) if _norm(o)]
    if expected:
        d = _norm(plan.student_deliverable)
        distinctive = lambda e: _words(e) - _GENERIC
        ok = any(e == d or e in d or (d in e and len(d) > 6) or (distinctive(e) and distinctive(e) <= _words(d)) for e in expected)
        if not ok:
            failures.append(f"Student deliverable '{plan.student_deliverable}' is not one of the expected outputs {ctx.get('expected_outputs')}")

    # 6. Includes applicable classroom rules verbatim.
    present = {_norm(r) for r in plan.classroom_rules}
    for r in ctx.get("rules", []):
        if _norm(r["text"]) not in present:
            failures.append(f"Missing classroom rule: '{r['text'][:70]}'")

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

    if len(plan.instructions) > 10:
        warnings.append("More than ten steps; consider consolidating")
    if not plan.what_to_collect:
        warnings.append("Nothing listed to collect")
    return ValidationResult(passed=not failures, failures=failures, warnings=warnings)
