"""LLM wrapper: schema strictness and the retry-with-feedback loop, using a fake client."""
import json
from datetime import date

from lessonbridge.config import Settings
from lessonbridge.generation.llm import LLMClient, _strict_schema, _SubPlanOut
from lessonbridge.generation import subplan as subplan_mod


def test_strict_schema_requires_every_property():
    schema = _strict_schema(_SubPlanOut)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "default" not in json.dumps(schema)


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.stop_reason = stop
        self.stop_details = None
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeAnthropic:
    """Returns a bad plan first (missing study period), then a good one."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        # Mirror the rules/materials the context asked for, like a model that read the prompt would.
        user = kwargs["messages"][0]["content"]
        ctx = json.loads(user[user.index("{") : user.rindex("}") + 1])
        out = dict(self.outputs.pop(0))
        out["classroom_rules"] = [r["text"] for r in ctx["rules"]]
        out["materials"] = ctx["available_materials"][:2]
        out["student_deliverable"] = ctx["expected_outputs"][0]
        out["objective"] = ctx["objective"]
        out["total_minutes"] = ctx["class_minutes"]
        return _Stream(_Msg(json.dumps(out)))


def good_plan(minutes=50, study=True):
    steps = ["1. Attendance using the seating chart (5 min)"]
    if study:
        steps.append("2. Silent study with notes before the quiz (15 min)")
    steps.append(f"3. Students complete the federalism quiz silently (25 min)" if study else "2. Students complete the federalism quiz silently (40 min)")
    steps.append("4. Collect quizzes and place in the tray (5 min)")
    return {"objective": "Demonstrate mastery of federalism concepts.", "materials": ["federalism_quiz_form", "pencils"], "instructions": steps,
            "student_deliverable": "completed federalism quiz", "what_to_collect": ["quizzes"], "early_finisher_activity": "read silently",
            "classroom_rules": ["On quiz days, students receive 15 minutes to study before the quiz.", "Collect all work."], "notes_for_next_day": "none", "total_minutes": minutes}


def test_generate_retries_with_feedback_until_quality_gate_passes(onboarded, monkeypatch):
    from lessonbridge import db
    from lessonbridge.absence.service import create_absence, plan_absence, apply_proposal
    from lessonbridge.curriculum.calendar import entries_between
    from lessonbridge.models import Teacher

    cfg = Settings(data_dir=onboarded[0].data_dir, allow_network=False, generator="claude", model="claude-opus-5-5")
    fake = FakeAnthropic([good_plan(study=False), good_plan(study=True)])
    llm = LLMClient(cfg)
    llm._client, llm._available, llm._checked = fake, True, True
    with db.session_scope() as s:
        t = s.get(Teacher, onboarded[1])
        civ = t.sections[2]
        quiz = next(e for e in entries_between(s, civ.id, date(2026, 9, 1), date(2026, 12, 1)) if e.lesson and e.lesson.is_assessment)
        ab = create_absence(s, t.id, quiz.date, quiz.date, substitute_type="any_sub")
        for p in plan_absence(s, ab):
            apply_proposal(s, p)
        entry = entries_between(s, civ.id, quiz.date, quiz.date)[0]
        assert entry.lesson and entry.lesson.is_assessment  # one-day absence keeps the quiz
        plan = subplan_mod.generate_for_entry(s, ab, civ, entry, llm=llm, cfg=cfg)
        assert plan.generator == "claude" and plan.attempts == 2 and plan.status.value == "accepted"
        assert "study period" in " ".join(fake.calls[1]["messages"][0]["content"].split("\n"))  # feedback was sent back
        assert fake.calls[0]["model"] == "claude-opus-5-5" and fake.calls[0]["fallbacks"] == "default"
        assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"
