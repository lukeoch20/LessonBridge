from datetime import date

from lessonbridge import db
from lessonbridge.generation.template import generate_template_plan
from lessonbridge.generation.validate import validate_plan
from lessonbridge.models import RuleScope, TeacherRule
from lessonbridge.profile.rules import add_rule, resolve_rules, rule_minutes, structure_rule
from lessonbridge.schemas import SubPlanContent


def test_structure_rule_parses_study_period():
    cat, st = structure_rule("On quiz days, students receive 15 minutes to study before the quiz.")
    assert cat == "assessment_routine" and st == {"trigger": "assessment", "action": "study_period", "minutes": 15}
    cat, st = structure_rule("Substitutes may not grade student work.")
    assert st["action"] == "no_sub_grading"


def test_rule_scoping_and_supersession(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        from lessonbridge.models import Teacher

        t = s.get(Teacher, tid)
        sec = t.sections[0]
        general = resolve_rules(s, tid, section=sec, is_assessment=False)
        quiz_day = resolve_rules(s, tid, section=sec, is_assessment=True)
        assert rule_minutes(general, "study_period") == 0
        assert rule_minutes(quiz_day, "study_period") == 15
        # Teacher's early-finisher rule superseded the default one.
        texts = [r.text for r in general]
        assert any("Early finishers read independently; there is no free time" in x for x in texts)
        assert not any(x.startswith("Early finishers read independently or complete unfinished") for x in texts)
        # A lesson-scoped rule wins over a teacher-wide rule in the same category/action.
        lesson = sec.units[0].lessons[0]
        add_rule(s, tid, "Early finishers start the extension packet.", scope=RuleScope.lesson, scope_id=lesson.id, structured={"trigger": "always", "action": "early_finisher"})
        rs = resolve_rules(s, tid, section=sec, lesson=lesson)
        ef = [r for r in rs if r.structured.get("action") == "early_finisher"]
        assert len(ef) == 1 and ef[0].scope == "lesson"
        # Duplicate text is not stored twice.
        n_before = len(list(s.scalars(__import__("sqlalchemy").select(TeacherRule).where(TeacherRule.teacher_id == tid))))
        add_rule(s, tid, "Early finishers start the extension packet.", scope=RuleScope.lesson, scope_id=lesson.id)
        n_after = len(list(s.scalars(__import__("sqlalchemy").select(TeacherRule).where(TeacherRule.teacher_id == tid))))
        assert n_before == n_after


def quiz_ctx():
    return {
        "date": "2026-10-29", "period": "3", "course": "Civics", "class_minutes": 50, "substitute_type": "any_sub", "kind": "assessment",
        "lesson_title": "Federalism Quiz", "lesson_type": "assessment", "objective": "Demonstrate mastery of federalism concepts.", "is_assessment": True,
        "available_materials": ["federalism_quiz_form", "paper", "pencils"], "expected_outputs": ["completed federalism quiz"],
        "rules": [{"text": "On quiz days, students receive 15 minutes to study before the quiz.", "category": "assessment_routine", "structured": {"trigger": "assessment", "action": "study_period", "minutes": 15}},
                  {"text": "Collect all work.", "category": "collection", "structured": {"trigger": "always", "action": "collect_work"}}],
        "is_meeting_day": True, "standards": ["CE.7"],
    }


def test_template_plan_passes_quality_gate_on_quiz_day():
    ctx = quiz_ctx()
    plan = generate_template_plan(ctx)
    result = validate_plan(plan, ctx)
    assert result.passed, result.failures
    assert any("15 min" in step and "study" in step.lower() for step in plan.instructions)
    assert plan.total_minutes == 50


def test_quality_gate_catches_missing_study_period_and_invented_material():
    ctx = quiz_ctx()
    bad = SubPlanContent(
        date=date(2026, 10, 29), period="3", course="Civics", objective="Demonstrate mastery of federalism concepts.",
        materials=["federalism_quiz_form", "Kahoot link"], instructions=["1. Attendance (5 min)", "2. Quiz (45 min)"],
        student_deliverable="completed federalism quiz", what_to_collect=["quizzes"], early_finisher_activity="read",
        classroom_rules=["Collect all work."], notes_for_next_day="", total_minutes=50,
    )
    result = validate_plan(bad, ctx)
    assert not result.passed
    joined = " ".join(result.failures)
    assert "study period" in joined and "Kahoot" in joined and "Missing classroom rule" in joined


def test_quality_gate_catches_minute_mismatch_and_new_content_for_day_sub():
    ctx = quiz_ctx()
    ctx.update({"is_assessment": False, "kind": "lesson", "rules": []})
    bad = SubPlanContent(date=date(2026, 10, 29), period="3", course="Civics", objective="Demonstrate mastery of federalism concepts.", materials=["paper"],
                         instructions=["1. Teach the concept of federalism (20 min)", "2. Practice (20 min)"], student_deliverable="completed federalism quiz",
                         what_to_collect=[], early_finisher_activity="read", classroom_rules=[], notes_for_next_day="", total_minutes=50)
    result = validate_plan(bad, ctx)
    assert any("sum to 40" in f for f in result.failures)
    assert any("should not teach new content" in f for f in result.failures)
