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


# ------------------------------------------------------------- audit fixes
import pytest  # noqa: E402

from lessonbridge.profile.rules import RuleTargetError, describe_rule, resolve_target  # noqa: E402


@pytest.mark.parametrize("text,minutes", [
    ("Students get a 15-minute review before every test.", 15),
    ("Before each quiz, give students fifteen minutes to study.", 15),
    ("Allow 10 minutes of review prior to any assessment.", 10),
    ("Students have 15 minutes to review their notes before a quiz.", 15),
    ("Give 15 minutes of study time before quizzes.", 15),
])
def test_lb31_common_wordings_of_the_study_rule(text, minutes):
    cat, st = structure_rule(text)
    assert st == {"trigger": "assessment", "action": "study_period", "minutes": minutes}


def test_lb31_unparsed_timing_is_flagged_not_silently_applied():
    _, st = structure_rule("Quizzes take 20 minutes.")
    assert st.get("needs_confirmation") and st["trigger"] == "assessment"
    assert "reword" in describe_rule(st)


def test_lb15_newer_rules_are_not_dropped(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        from lessonbridge.models import Teacher

        sec = s.get(Teacher, tid).sections[0]
        add_rule(s, tid, "No hall passes during the first and last ten minutes of class.")
        add_rule(s, tid, "Late work for projects is not accepted after the unit test.")
        texts = [r.text for r in resolve_rules(s, tid, section=sec)]
        assert "No hall passes during the first and last ten minutes of class." in texts
        assert "Students may use the hall pass one at a time; sign out on the clipboard." in texts  # additive: both apply
        assert "Late work for projects is not accepted after the unit test." in texts
        add_rule(s, tid, "On quiz days, students now receive 20 minutes to study before the quiz.")
        assert rule_minutes(resolve_rules(s, tid, section=sec, is_assessment=True), "study_period") == 20  # single-valued: newest wins


def test_lb32_scoped_rules_target_real_lessons_units_and_courses(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        from lessonbridge.models import Teacher

        t = s.get(Teacher, tid)
        eng, civ = t.sections[0], t.sections[2]
        unit = civ.active_units[0]
        lesson = unit.lessons[0]
        rid = resolve_target(s, tid, RuleScope.course, "civics")
        add_rule(s, tid, "Civics notebooks stay in the classroom.", scope=RuleScope.course, scope_id=rid)
        add_rule(s, tid, "Use the unit word wall.", scope=RuleScope.unit, scope_id=resolve_target(s, tid, RuleScope.unit, unit.slug))
        add_rule(s, tid, "Pair students by seat number today.", scope=RuleScope.lesson, scope_id=resolve_target(s, tid, RuleScope.lesson, lesson.slug))
        civ_texts = [r.text for r in resolve_rules(s, tid, section=civ, lesson=lesson)]
        eng_texts = [r.text for r in resolve_rules(s, tid, section=eng)]
        assert {"Civics notebooks stay in the classroom.", "Use the unit word wall.", "Pair students by seat number today."} <= set(civ_texts)
        assert "Civics notebooks stay in the classroom." not in eng_texts  # course ids and section ids are not mixed up
        with pytest.raises(RuleTargetError):
            resolve_target(s, tid, RuleScope.lesson, "no-such-lesson")
        with pytest.raises(RuleTargetError):
            add_rule(s, tid, "Unscoped lesson rule.", scope=RuleScope.lesson)


def test_lb38_lb40_template_passes_its_gate_everywhere_and_the_gate_rejects_inventions():
    """Template x validator matrix (audit test recommendation 4)."""
    from lessonbridge.providers.defaults import civics_7, english_7

    rules = [{"text": "On quiz days, students receive 15 minutes to study before the quiz.", "category": "assessment_routine", "structured": {"trigger": "assessment", "action": "study_period", "minutes": 15}},
             {"text": "Collect all work.", "category": "collection", "structured": {"trigger": "always", "action": "collect_work"}}]
    failures = []
    for spec in (english_7(), civics_7()):
        for u in spec.units:
            for l in u.lessons:
                assess = l.lesson_type == "assessment"
                for sub in ("any_sub", "long_term_sub"):
                    for mins in (30, 45, 50, 90):
                        ctx = {"date": "2026-10-29", "period": "1", "course": "x", "class_minutes": mins, "substitute_type": sub, "kind": "assessment" if assess else "lesson",
                               "lesson_title": l.title, "lesson_type": l.lesson_type, "objective": l.objective, "is_assessment": assess, "unit": u.title,
                               "available_materials": l.materials + ["paper", "pencils", "independent reading books"], "expected_outputs": l.student_output or ["completed class work"],
                               "rules": [r for r in rules if r["structured"]["trigger"] == "always" or assess], "is_meeting_day": True, "activity_description": "Complete the packet."}
                        for kind in (ctx["kind"], "flex", "filler"):
                            c = dict(ctx, kind=kind)
                            r = validate_plan(generate_template_plan(c), c)
                            if not r.passed:
                                failures.append((l.slug, sub, mins, kind, r.failures))
    assert not failures, failures[:3]
    ctx = {"class_minutes": 50, "substitute_type": "any_sub", "objective": "Sort powers into national, state and concurrent.", "available_materials": ["powers_sort", "paper", "pencils"],
           "expected_outputs": ["powers sort"], "rules": [], "is_meeting_day": True}
    bad = SubPlanContent(date=date(2026, 10, 29), period="3", course="Civics", objective=ctx["objective"], materials=["Kahoot game projected on the whiteboard", "newspaper clippings"],
                         instructions=["1. Sort national state concurrent powers (25 min)", "2. Show the YouTube video (25 min)"], student_deliverable="sorted poster of state and national powers",
                         what_to_collect=["x"], early_finisher_activity="read", classroom_rules=[], notes_for_next_day="", total_minutes=50)
    joined = " ".join(validate_plan(bad, ctx).failures)
    assert "Kahoot" in joined and "newspaper" in joined and "deliverable" in joined
    honest = bad.model_copy(update={"materials": ["powers sort"], "student_deliverable": "powers sort"})
    assert any("youtube" in f.lower() or "video" in f.lower() for f in validate_plan(honest, ctx).failures)
