"""Claude client wrapper.

All model calls go through here so prompts, model choice, structured output
and refusal handling live in one place. When no credentials are available the
rest of the system falls back to deterministic template generation.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from ..config import Settings, settings as default_settings
from ..schemas import ExtractedTeacherContext, RuleSpec, SubPlanContent, UnitSpec
from . import prompts

T = TypeVar("T", bound=BaseModel)


class GenerationError(RuntimeError):
    pass


def _strict_schema(model: Type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> the strict subset structured outputs accept (every property required, no extras)."""
    schema = model.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for key in ("default", "title", "format"):
                node.pop(key, None)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    return schema


class LLMClient:
    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or default_settings
        self._client = None
        self._checked = False
        self._available = False

    def available(self) -> bool:
        if self.cfg.generator == "template":
            return False
        if not self._checked:
            self._checked = True
            has_cred = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"))
            if not has_cred:
                # Profiles from `ant auth login` live on disk; let the SDK decide.
                from pathlib import Path

                has_cred = (Path.home() / ".config" / "anthropic").exists()
            if has_cred or self.cfg.generator == "claude":
                try:
                    import anthropic

                    self._client = anthropic.Anthropic()
                    self._available = True
                except Exception:
                    self._available = False
        return self._available

    # ----------------------------------------------------------- core call
    def structured(self, *, system: str, user: str, schema: Type[T], max_tokens: int = 8000, effort: str | None = None) -> T:
        if not self.available():
            raise GenerationError("Claude is not configured (no credentials); use the template generator.")
        import anthropic

        kwargs: dict[str, Any] = dict(
            model=self.cfg.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort or self.cfg.effort, "format": {"type": "json_schema", "schema": _strict_schema(schema)}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        try:
            with self._client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            # Older proxies / platforms may reject the fallback parameter; retry without it once.
            if "fallback" in str(exc).lower():
                kwargs.pop("fallbacks", None)
                kwargs.pop("betas", None)
                with self._client.beta.messages.stream(**kwargs) as stream:
                    message = stream.get_final_message()
            else:
                raise GenerationError(f"Bad request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise GenerationError("Rate limited by the Claude API; try again shortly.") from exc
        except anthropic.APIStatusError as exc:
            raise GenerationError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise GenerationError("Could not reach the Claude API.") from exc
        if message.stop_reason == "refusal":
            detail = getattr(message, "stop_details", None)
            raise GenerationError(f"Claude declined this request ({getattr(detail, 'category', None) or 'refusal'}).")
        if message.stop_reason == "max_tokens":
            raise GenerationError("Response was cut off by max_tokens; increase the limit.")
        text = next((b.text for b in message.content if b.type == "text"), "")
        try:
            return schema.model_validate_json(text)
        except Exception as exc:
            raise GenerationError(f"Could not parse model output as {schema.__name__}: {exc}") from exc

    # -------------------------------------------------------- task helpers
    def generate_sub_plan(self, context: dict[str, Any], feedback: list[str] | None = None) -> SubPlanContent:
        user = prompts.SUB_PLAN_USER.format(context=json.dumps(context, indent=2, default=str, sort_keys=True))
        if feedback:
            user += "\n\nA previous attempt failed validation for these reasons; fix every one of them:\n- " + "\n- ".join(feedback)
        return self.structured(system=prompts.SUB_PLAN_SYSTEM, user=user, schema=_SubPlanOut).to_content()

    def extract_teacher_context(self, text: str, subject: str) -> ExtractedTeacherContext:
        out = self.structured(system=prompts.CONTEXT_EXTRACT_SYSTEM.format(subject=subject), user=text[:120_000], schema=_ExtractOut, max_tokens=16000)
        return out.to_context()

    def explain_proposal(self, explanation: str, diff_lines: list[str]) -> str:
        out = self.structured(system=prompts.EXPLAIN_SYSTEM, user="Mechanical explanation:\n" + explanation + "\n\nDiff:\n" + "\n".join(diff_lines), schema=_TextOut, max_tokens=1000, effort="low")
        return out.text


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
            units=[UnitSpec(slug=u.slug, title=u.title, quarter=u.quarter or None, planned_days=u.planned_days or 10, standards=u.standards, summary=u.summary, source="syllabus") for u in self.units],
            rules=[RuleSpec(text=r.text, category=r.category or "procedure") for r in self.rules],
            preferences={f"pref_{i}": p for i, p in enumerate(self.preferences)},
            materials=self.materials, assessments=self.assessments, conflicts=self.conflicts, confidence=0.8,
        )


class _TextOut(BaseModel):
    text: str
