"""Claude client wrapper.

All model calls go through here so prompts, model choice, structured output
and refusal handling live in one place. When no credentials resolve, the rest
of the system falls back to deterministic template generation; every SDK error
is converted to ``GenerationError`` so callers can fall back per plan.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings, settings as default_settings
from ..schemas import ExtractedTeacherContext, LessonSpec, RuleSpec, SubPlanContent, UnitSpec
from . import prompts

T = TypeVar("T", bound=BaseModel)
log = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    pass


def _strict_schema(model: Type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> the strict subset structured outputs accept.

    Every object lists all its properties as required and forbids extras.
    Metadata keys ("title", "default", "format") are removed from schema nodes
    only, never from a ``properties`` mapping, where "title" can be a real
    field name (LB-16).
    """
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for v in node:
                walk(v)
            return
        if not isinstance(node, dict):
            return
        for key in ("default", "title", "format"):
            node.pop(key, None)
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node["properties"].keys())
        for k, v in node.items():
            if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
                for sub in v.values():
                    walk(sub)
            else:
                walk(v)

    walk(schema)
    return schema


def credentials_available() -> bool:
    """True when the SDK can resolve credentials: an API key or token, or a stored `ant auth login` profile."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    cfg_dir = Path(os.environ.get("ANTHROPIC_CONFIG_DIR", Path.home() / ".config" / "anthropic"))
    if cfg_dir.is_dir():
        for p in cfg_dir.rglob("*"):
            if p.is_file() and p.stat().st_size > 0 and p.suffix in (".json", ".toml", ".yaml", ".yml", "") and "cache" not in p.parts:
                return True
    return False


class LLMClient:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self._client = None
        self._checked = False
        self._available = False
        self.unavailable_reason: str = ""

    def available(self) -> bool:
        if self.cfg.generator == "template":
            self.unavailable_reason = "template generator selected"
            return False
        if not self._checked:
            self._checked = True
            if not credentials_available():
                self.unavailable_reason = "no Claude credentials found (set ANTHROPIC_API_KEY or run `ant auth login`)"
                if self.cfg.generator == "claude":
                    log.warning("LESSONBRIDGE_GENERATOR=claude but %s; using the template generator.", self.unavailable_reason)
                return False
            try:
                import anthropic

                self._client = anthropic.Anthropic()
                self._available = True
            except Exception as exc:  # noqa: BLE001
                self.unavailable_reason = f"could not create the Claude client: {exc}"
                self._available = False
        return self._available

    def _disable(self, reason: str) -> None:
        self._available = False
        self.unavailable_reason = reason

    # ----------------------------------------------------------- core call
    def structured(self, *, system: str, user: str | list[dict], schema: Type[T], max_tokens: int = 8000, effort: str | None = None) -> T:
        if not self.available():
            raise GenerationError(f"Claude is not available: {self.unavailable_reason}")
        import anthropic

        content = user if isinstance(user, list) else [{"type": "text", "text": user}]
        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort or self.cfg.effort, "format": {"type": "json_schema", "schema": _strict_schema(schema)}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        def call(kw: dict) -> Any:
            with self._client.beta.messages.stream(**kw) as stream:
                return stream.get_final_message()

        try:
            try:
                message = call(kwargs)
            except anthropic.BadRequestError as exc:
                # Some proxies / platforms reject the fallback parameter; retry once without it (guarded, LB-62).
                if "fallback" not in str(exc).lower():
                    raise
                kwargs.pop("fallbacks", None)
                kwargs.pop("betas", None)
                message = call(kwargs)
        except anthropic.AuthenticationError as exc:
            self._disable("Claude rejected the credentials")
            raise GenerationError("Claude rejected the credentials; using the template generator.") from exc
        except anthropic.PermissionDeniedError as exc:
            self._disable("the credentials lack permission for this model")
            raise GenerationError("The credentials lack permission for this model.") from exc
        except anthropic.RateLimitError as exc:
            raise GenerationError("Rate limited by the Claude API; try again shortly.") from exc
        except anthropic.BadRequestError as exc:
            raise GenerationError(f"Bad request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise GenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise GenerationError("Could not reach the Claude API.") from exc
        except TypeError as exc:
            # The SDK raises TypeError when no authentication method resolves (LB-39).
            self._disable(f"no usable credentials ({exc})")
            raise GenerationError(f"No usable Claude credentials: {exc}") from exc
        except anthropic.AnthropicError as exc:
            raise GenerationError(f"Claude SDK error: {exc}") from exc
        if message.stop_reason == "refusal":
            detail = getattr(message, "stop_details", None)
            raise GenerationError(f"Claude declined this request ({getattr(detail, 'category', None) or 'refusal'}).")
        if message.stop_reason == "max_tokens":
            raise GenerationError("Response was cut off by max_tokens; increase the limit.")
        usage = getattr(message, "usage", None)
        if usage is not None:
            log.debug("usage: input=%s cache_read=%s cache_write=%s", getattr(usage, "input_tokens", None), getattr(usage, "cache_read_input_tokens", None), getattr(usage, "cache_creation_input_tokens", None))
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return schema.model_validate_json(text)
        except Exception as exc:
            raise GenerationError(f"Could not parse model output as {schema.__name__}: {exc}") from exc

    # -------------------------------------------------------- task helpers
    def generate_sub_plan(self, context: dict[str, Any], feedback: list[str] | None = None) -> SubPlanContent:
        ctx_text = prompts.SUB_PLAN_USER.format(context=json.dumps(context, indent=2, default=str, sort_keys=True))
        # The per-day context is the cache breakpoint, so feedback retries for the same day reuse it (LB-63).
        blocks: list[dict] = [{"type": "text", "text": ctx_text, "cache_control": {"type": "ephemeral"}}]
        if feedback:
            blocks.append({"type": "text", "text": "A previous attempt failed validation for these reasons; fix every one of them:\n- " + "\n- ".join(feedback)})
        return self.structured(system=prompts.SUB_PLAN_SYSTEM, user=blocks, schema=_SubPlanOut).to_content()

    def extract_teacher_context(self, text: str, subject: str) -> ExtractedTeacherContext:
        out = self.structured(system=prompts.CONTEXT_EXTRACT_SYSTEM.format(subject=subject), user=text[:120_000], schema=_ExtractOut, max_tokens=16000)
        return out.to_context()

    def draft_lesson(self, context: dict[str, Any]) -> LessonSpec:
        out = self.structured(system=prompts.DRAFT_LESSON_SYSTEM, user=json.dumps(context, indent=2, default=str, sort_keys=True), schema=_DraftOut, max_tokens=4000)
        return out.to_spec()


# ------------------------------------------------- strict output models
class _SubPlanOut(BaseModel):
    objective: str
    materials: list[str]
    instructions: list[str]
    student_deliverable: str
    what_to_collect: list[str]
    early_finisher_activity: str
    classroom_rules: list[str]
    notes_for_next_day: str
    total_minutes: int

    def to_content(self) -> SubPlanContent:
        # date/period/course are filled by the caller from the context.
        return SubPlanContent(date="1970-01-01", period="", course="", **self.model_dump())


class _UnitOut(BaseModel):
    slug: str
    title: str
    quarter: int
    planned_days: int
    standards: list[str]
    summary: str


class _RuleOut(BaseModel):
    text: str
    category: str


class _ExtractOut(BaseModel):
    units: list[_UnitOut]
    rules: list[_RuleOut]
    preferences: list[str]
    materials: list[str]
    assessments: list[str]
    conflicts: list[str]

    def to_context(self) -> ExtractedTeacherContext:
        return ExtractedTeacherContext(
            units=[UnitSpec(slug=u.slug, title=u.title, quarter=u.quarter if 1 <= u.quarter <= 4 else None, planned_days=u.planned_days if u.planned_days > 0 else None,
                            standards=u.standards, summary=u.summary, source="syllabus") for u in self.units],
            rules=[RuleSpec(text=r.text, category=r.category or "procedure") for r in self.rules],
            preferences={f"pref_{i}": p for i, p in enumerate(self.preferences)},
            materials=self.materials, assessments=self.assessments, conflicts=self.conflicts, confidence=0.8,
        )


class _DraftOut(BaseModel):
    title: str
    objective: str
    lesson_type: str
    materials: list[str]
    student_output: list[str]

    def to_spec(self) -> LessonSpec:
        lt = self.lesson_type if self.lesson_type in ("guided_practice", "independent_practice", "review", "discussion", "writing_workshop", "reading") else "guided_practice"
        return LessonSpec(slug="drafted", title=self.title, objective=self.objective, lesson_type=lt, materials=self.materials, student_output=self.student_output)
