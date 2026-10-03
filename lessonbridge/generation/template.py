"""Deterministic substitute-plan generator.

Used when Claude is not configured, as the fallback when generation fails
validation repeatedly, and in tests. It builds plans from lesson metadata,
teacher rules and the replacement library, so output is always grounded in
real materials and passes the quality gate by construction.
"""
from __future__ import annotations

from typing import Any

from ..schemas import SubPlanContent


def _steps_sum_to(steps: list[tuple[str, int]], total: int) -> list[str]:
    """Adjust the largest step so the minutes add up to ``total`` exactly."""
    steps = [list(s) for s in steps if s[1] > 0]
    diff = total - sum(m for _, m in steps)
    if diff != 0:
        idx = max(range(len(steps)), key=lambda i: steps[i][1])
        steps[idx][1] = max(5, steps[idx][1] + diff)
        diff = total - sum(m for _, m in steps)
        if diff:  # second pass if clamped
            steps[idx][1] += diff
    return [f"{i + 1}. {text} ({m} min)" for i, (text, m) in enumerate(steps)]


def generate_template_plan(ctx: dict[str, Any]) -> SubPlanContent:
    minutes = int(ctx["class_minutes"])
    sub = ctx.get("substitute_type", "any_sub")
    rules = ctx.get("rules", [])
    rule_texts = [r["text"] for r in rules]
    materials = list(dict.fromkeys(ctx.get("available_materials", [])))
    handout = [m for m in materials if m not in ("paper", "pencils", "independent reading books", "seating chart")]
    outputs = ctx.get("expected_outputs") or ["completed class work"]
    deliverable = outputs[0]
    title = ctx.get("lesson_title", "Class work")
    objective = ctx.get("objective") or f"Students complete {title}."
    mats_phrase = ", ".join(m.replace("_", " ") for m in handout[:3]) or "the materials on the desk"
    study = next((int(r["structured"].get("minutes", 0)) for r in rules if (r.get("structured") or {}).get("action") == "study_period"), 0)
    early = next((r["text"] for r in rules if (r.get("structured") or {}).get("action") == "early_finisher"), "Early finishers read their independent reading book silently.")
    attendance = "Take attendance using the seating chart and write the objective on the board: " + objective

    steps: list[tuple[str, int]]
    if ctx.get("is_assessment"):
        steps = [(attendance, 5)]
        if study:
            steps.append((f"Give students {study} minutes to study silently using their notes or the review sheet (pre-quiz study period).", study))
        steps.append((f"Distribute {mats_phrase}. Students complete the assessment silently and independently. Do not answer content questions; clarify directions only.", max(15, minutes - 5 - study - 5)))
        steps.append(("Collect every assessment, count them against the class list, and place them in the tray labeled for the teacher. Students who finish early read silently.", 5))
    elif ctx.get("kind") == "flex":
        unit = ctx.get("unit") or "this unit"
        steps = [
            (attendance, 5),
            (f"Write on the board what students still owe from earlier in {unit}; students list their own unfinished items and start on them using {mats_phrase}.", 5),
            ("Catch-up block: students finish unfinished work independently; circulate and check off completed items on the class list.", max(10, (minutes - 20) // 2)),
            (f"Extension block: students who are finished complete the extension practice ({mats_phrase}); partners may quiz each other on unit vocabulary.", max(10, minutes - 20 - (minutes - 20) // 2)),
            ("Collect unfinished work and extension practice separately; leave both in the teacher's tray with a note of who finished what.", 5),
        ]
    elif ctx.get("kind") == "filler":
        desc = ctx.get("activity_description") or f"Students complete {title}."
        steps = [
            (attendance, 5),
            (f"Distribute {mats_phrase}. Read the directions aloud: {desc}", 5),
            ("Students work independently and silently; circulate to keep students on task. Clarify directions only.", max(15, minutes - 20)),
            ("Stop work. Students write their name on the work; collect it and place it in the teacher's tray.", 5),
            ("Clean up: materials returned to the back table, chairs pushed in, dismiss by rows when the bell rings.", 5),
        ]
    elif sub == "long_term_sub" and ctx.get("lesson_type") in ("direct_instruction", "guided_practice", "discussion", "writing_workshop", "project"):
        steps = [
            (attendance, 5),
            (f"Warm-up: students write two sentences connecting yesterday's work ({ctx.get('prior_state', 'previous lesson')}) to today's objective.", 5),
            (f"Instruction: using {mats_phrase}, teach the lesson content. Key points: {objective}", 15),
            (f"Guided practice: students work through the practice task in pairs while you circulate; stop the class after half the time to check one example together.", max(10, minutes - 40)),
            (f"Exit ticket / deliverable: each student submits {deliverable}.", 5),
            ("Collect the deliverable and note how far the class got for tomorrow's plan.", 5),
        ]
    else:
        steps = [
            (attendance, 5),
            (f"Distribute {mats_phrase}. Read the directions aloud and show the first item as an example from the packet (do not teach new content).", 5),
            (f"Students complete the task independently: {objective}", max(15, minutes - 25)),
            ("Partner check: students compare answers with an elbow partner and star anything they disagree on for the teacher to review.", 10),
            (f"Collect {deliverable}; students who are not finished write 'unfinished' at the top. Place work in the teacher's tray.", 5),
        ]
    instructions = _steps_sum_to(steps, minutes)
    notes_next = ctx.get("next_day_note") or "Leave a note on how far the class got and any students who need follow-up."
    return SubPlanContent(
        date=ctx["date"], period=str(ctx.get("period", "")), course=ctx.get("course", ""), objective=objective,
        materials=materials or ["paper", "pencils"], instructions=instructions, student_deliverable=deliverable,
        what_to_collect=[deliverable] + ([o for o in outputs[1:2]]), early_finisher_activity=early, classroom_rules=rule_texts,
        notes_for_next_day=notes_next, total_minutes=minutes, substitute_type=sub, standards=list(ctx.get("standards", [])),
    )
