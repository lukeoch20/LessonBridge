"""LLM wrapper: schema strictness and the retry-with-feedback loop, using a fake client."""
import json
from datetime import date

from lessonbridge.config import Settings
from lessonbridge.generation.llm import LLMClient, _strict_schema, _SubPlanOut


def test_strict_schema_requires_every_property():
    schema = _strict_schema(_SubPlanOut)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert "default" not in json.dumps(schema)


class _Msg:
    def __init__(self, text, stop="end_turn"):
        self.stop_reason = stop
        self.stop_details = None
        self.usage = None
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
        blocks = kwargs["messages"][0]["content"]
        text = blocks[0]["text"]
        ctx = json.loads(text[text.index("{") : text.rindex("}") + 1])
        out = dict(self.outputs.pop(0))
        out["classroom_rules"] = [r["text"] for r in ctx["rules"]]
        out["materials"] = ctx["available_materials"][:2]
        out["student_deliverable"] = ctx["expected_outputs"][0]
        out["objective"] = ctx["objective"]
        out["total_minutes"] = ctx["class_minutes"]
        return _Stream(_Msg(json.dumps(out)))


def good_plan(study=True):
    steps = ["1. Attendance using the seating chart (5 min)"]
    if study:
        steps.append("2. Silent study with notes before the quiz (15 min)")
    steps.append("3. Students complete the quiz silently (25 min)" if study else "2. Students complete the quiz silently (40 min)")
    steps.append("4. Collect quizzes and place in the tray (5 min)")
    return {"objective": "", "materials": [], "instructions": steps, "student_deliverable": "", "what_to_collect": ["quizzes"],
            "early_finisher_activity": "read silently", "classroom_rules": [], "notes_for_next_day": "none", "total_minutes": 50}


def quiz_context():
    return {"date": "2026-10-29", "period": "3", "course": "Civics", "class_minutes": 50, "substitute_type": "any_sub", "kind": "assessment",
            "lesson_title": "Federalism Quiz", "lesson_type": "assessment", "objective": "Demonstrate mastery: Federalism Quiz", "is_assessment": True,
            "available_materials": ["federalism-quiz_form", "paper", "pencils"], "expected_outputs": ["completed federalism quiz"],
            "rules": [{"text": "On quiz days, students receive 15 minutes to study before the quiz.", "category": "assessment_routine",
                       "structured": {"trigger": "assessment", "action": "study_period", "minutes": 15}}], "is_meeting_day": True, "standards": ["CE.7"]}


def make_llm(outputs, generator="claude"):
    cfg = Settings(allow_network=False, generator=generator, model="claude-opus-5-5")
    fake = FakeAnthropic(outputs)
    llm = LLMClient(cfg)
    llm._client, llm._available, llm._checked = fake, True, True
    return cfg, llm, fake


def test_generate_retries_with_feedback_until_quality_gate_passes():
    from lessonbridge.generation.subplan import run_job

    cfg, llm, fake = make_llm([good_plan(study=False), good_plan(study=True)])
    res = run_job(quiz_context(), llm, cfg)
    assert res.generator == "claude" and len(res.attempts) == 2 and res.validation.passed
    second = fake.calls[1]["messages"][0]["content"]
    assert "study period" in second[1]["text"]  # validator feedback sent back as its own block
    assert second[0].get("cache_control") == {"type": "ephemeral"}  # per-day context is the cache breakpoint (LB-63)
    assert fake.calls[0]["model"] == "claude-opus-5-5" and fake.calls[0]["fallbacks"] == "default"
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_every_schema_sent_to_claude_requires_exactly_its_properties():
    """Audit test recommendation 6 (LB-16)."""
    from lessonbridge.generation.llm import _DraftOut, _ExtractOut

    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for v in node.values():
                check(v)
        elif isinstance(node, list):
            for v in node:
                check(v)

    for model in (_SubPlanOut, _ExtractOut, _DraftOut):
        check(_strict_schema(model))
    unit = _strict_schema(_ExtractOut)["$defs"]["_UnitOut"]
    assert "title" in unit["properties"]


class _FailingClient:
    """Raises what the SDK raises when no credentials resolve."""

    def __init__(self):
        self.beta = self
        self.messages = self

    def stream(self, **kwargs):
        raise TypeError("Could not resolve authentication method")


def test_missing_credentials_fall_back_to_the_template_instead_of_crashing(monkeypatch):
    """LB-39 / LB-62."""
    from lessonbridge.generation import llm as llm_mod
    from lessonbridge.generation.subplan import run_job

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(llm_mod, "credentials_available", lambda: False)
    cfg = Settings(allow_network=False, generator="claude")
    client = LLMClient(cfg)
    assert not client.available() and "credentials" in client.unavailable_reason
    res = run_job(quiz_context(), client, cfg)
    assert res.generator == "template" and res.validation.passed
    cfg, llm, _ = make_llm([])
    llm._client = _FailingClient()
    res = run_job(quiz_context(), llm, cfg)
    assert res.generator == "template-fallback" and res.validation.passed and not llm.available()


def test_extraction_failure_is_reported_and_units_never_come_from_an_empty_reply():
    """LB-16: errors are surfaced; a reply without units does not replace the teacher's syllabus."""
    from lessonbridge.curriculum.interpret import extract_teacher_context
    from lessonbridge.schemas import ExtractedTeacherContext

    class Broken:
        def available(self):
            return True

        def extract_teacher_context(self, text, subject):
            raise RuntimeError("schema rejected")

    class Empty(Broken):
        def extract_teacher_context(self, text, subject):
            return ExtractedTeacherContext(units=[], rules=[])

    text = "Quarter 1\nUnit 1: Short Fiction (18 days)\n"
    ex = extract_teacher_context(text, "english", llm=Broken())
    assert ex.method == "heuristic" and "schema rejected" in ex.error and ex.context.units
    ex = extract_teacher_context(text, "english", llm=Empty())
    assert ex.context.units and ex.method == "claude+heuristic"
