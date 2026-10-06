"""Deterministic substitute-plan generator.

Used when Claude is not configured, as the fallback when generation fails
validation repeatedly, and in tests. It builds plans from lesson metadata,
teacher rules and the replacement library, so output is grounded in real
materials and passes the quality gate for every lesson type, substitute type
and period length (LB-38).
"""
from __future__ import annotations

from typing import Any

from ..schemas import SubPlanContent

STAPLES = ("paper", "pencils", "independent reading books", "seating chart")


def _fit(fixed: list[tuple[str, int]], work_index: int, total: int) -> list[tuple[str, int]]:
    """Give the work step whatever time remains; shrink admin steps (never a rule-mandated step) on short periods."""
    steps = [list(s) for s in fixed]
    others = sum(m for i, (_, m) in enumerate(steps) if i != work_index)
    remaining = total - others
    if remaining < 5:
        # Short period: admin steps (attendance, collection, cleanup) give way first, down to 1 minute each.
        for i, (text, m) in enumerate(steps):
            if i == work_index or "study" in text.lower():
                continue
            while m > 1 and remaining < 5:
                m -= 1
                remaining += 1
            steps[i][1] = m
    steps[work_index][1] = max(1, remaining)
    return [(t, m) for t, m in steps if m > 0]


def _number(steps: list[tuple[str, int]]) -> list[str]:
    return [f"{i + 1}. {text} ({m} min)" for i, (text, m) in enumerate(steps)]


def generate_template_plan(ctx: dict[str, Any]) -> SubPlanContent:
    minutes = int(ctx["class_minutes"])
    sub = ctx.get("substitute_type", "any_sub")
    rules = ctx.get("rules", [])
    rule_texts = [r["text"] for r in rules]
    materials = list(dict.fromkeys(ctx.get("available_materials", [])))
    handout = [m for m in materials if m not in STAPLES]
    outputs = ctx.get("expected_outputs") or ["completed class work"]
    deliverable = outputs[0]
    title = ctx.get("lesson_title", "Class work")
    objective = ctx.get("objective") or f"Students complete {title}."
    mats_phrase = ", ".join(m.replace("_", " ") for m in handout[:3]) or "the materials on the desk"
    study = next((int(r["structured"].get("minutes", 0)) for r in rules if (r.get("structured") or {}).get("action") == "study_period"), 0)
    early = next((r["text"] for r in rules if (r.get("structured") or {}).get("action") == "early_finisher"), "Early finishers read their independent reading book silently.")
    admin = 5 if minutes >= 35 else 2
    attendance = "Take attendance using the seating chart and write the objective on the board: " + objective
    compressed = ctx.get("compressed") or []

    if ctx.get("is_assessment"):
        fixed = [(attendance, admin)]
        if study:
            fixed.append((f"Pre-quiz study period: students study silently using their notes or the review sheet for {study} minutes.", study))
        fixed.append((f"Distribute {mats_phrase}. Students complete the assessment silently and independently. Answer questions about directions only.", 0))
        fixed.append(("Collect every assessment, count them against the class list, and place them in the tray labeled for the teacher. Students who finish early read silently.", admin))
        steps = _fit(fixed, len(fixed) - 2, minutes)
    elif ctx.get("kind") == "flex":
        unit = ctx.get("unit") or "this unit"
        fixed = [
            (attendance, admin),
            (f"Write on the board what students still owe from earlier in {unit}; students list their own unfinished items and start on them using {mats_phrase}.", admin),
            ("Catch-up block: students finish unfinished work independently; circulate and check off completed items on the class list.", 0),
            (f"Extension block: students who are finished complete the extension practice ({mats_phrase}); partners may quiz each other on unit vocabulary.", max(1, (minutes - 3 * admin) // 3)),
            ("Collect unfinished work and extension practice separately; leave both in the teacher's tray with a note of who finished what.", admin),
        ]
        steps = _fit(fixed, 2, minutes)
    elif ctx.get("kind") == "filler":
        desc = ctx.get("activity_description") or f"Students complete {title}."
        fixed = [
            (attendance, admin),
            (f"Distribute {mats_phrase}. Read the directions aloud: {desc}", admin),
            ("Students work independently and silently; circulate to keep students on task. Answer questions about directions only.", 0),
            ("Stop work. Students write their name on the work; collect it and place it in the teacher's tray.", admin),
            ("Clean up: materials returned to the back table, chairs pushed in, dismiss when the bell rings.", min(admin, 3)),
        ]
        steps = _fit(fixed, 2, minutes)
    elif sub == "long_term_sub" and ctx.get("lesson_type") in ("direct_instruction", "guided_practice", "discussion", "writing_workshop", "project"):
        core = f" Focus on: {'; '.join(compressed)}." if compressed else ""
        fixed = [
            (attendance, admin),
            (f"Warm-up: students write two sentences connecting the previous lesson ({ctx.get('prior_state', 'previous lesson')}) to today's objective.", admin),
            (f"Instruction: using {mats_phrase}, present the lesson content. Key points: {objective}{core}", max(1, minutes // 3)),
            ("Guided practice: students work through the practice task in pairs while you circulate; stop the class halfway to check one example together.", 0),
            (f"Exit ticket / deliverable: each student submits {deliverable}.", admin),
            ("Collect the deliverable and note how far the class got for the next plan.", admin),
        ]
        steps = _fit(fixed, 3, minutes)
    else:
        core = f" Today covers only: {'; '.join(compressed)}." if compressed else ""
        fixed = [
            (attendance, admin),
            (f"Distribute {mats_phrase}. Read the directions aloud and model only the first item with the class; answer questions about directions only.{core}", admin),
            (f"Students complete the task independently: {objective}", 0),
            ("Partner check: students compare answers with an elbow partner and star anything they disagree on for the teacher to review.", max(1, min(10, minutes // 5))),
            (f"Collect {deliverable}; students who are not finished write 'unfinished' at the top. Place work in the teacher's tray.", admin),
        ]
        steps = _fit(fixed, 2, minutes)
    instructions = _number(steps)
    notes_next = ctx.get("next_day_note") or "Leave a note on how far the class got and any students who need follow-up."
    return SubPlanContent(
        date=ctx["date"], period=str(ctx.get("period", "")), course=ctx.get("course", ""), objective=objective,
        materials=materials or ["paper", "pencils"], instructions=instructions, student_deliverable=deliverable,
        what_to_collect=[deliverable] + list(outputs[1:2]), early_finisher_activity=early, classroom_rules=rule_texts,
        notes_for_next_day=notes_next, total_minutes=minutes, substitute_type=sub, standards=list(ctx.get("standards", [])),
    )
