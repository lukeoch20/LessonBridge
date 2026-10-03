"""Pydantic schemas shared by the engines, the LLM layer, the CLI and the web app.

These are the *typed* shapes that flow between modules. ORM rows are converted
to and from these so the planning engines never depend on a live session.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ public context
class CalendarEventSpec(BaseModel):
    date: date
    end_date: Optional[date] = None
    event_type: str
    title: str
    instructional: bool = False
    quarter: Optional[int] = None
    needs_confirmation: bool = False
    notes: Optional[str] = None


class GradingPeriodSpec(BaseModel):
    quarter: int
    start: date
    end: date
    grades_due: Optional[date] = None
    needs_confirmation: bool = False


class StandardSpec(BaseModel):
    code: str
    title: str
    description: str = ""
    strand: str = ""


class SourcePayload(BaseModel):
    """What a district provider returns for one source."""

    source_key: str
    doc_type: str
    title: str
    url: Optional[str] = None
    content_type: str = "application/json"
    text: str = ""
    structured: dict[str, Any] = Field(default_factory=dict)
    origin: Literal["snapshot", "live"] = "snapshot"
    refresh_interval_days: int = 30
    subject: Optional[str] = None
    raw_bytes: Optional[bytes] = None
    notes: str = ""


# -------------------------------------------------------------------- curriculum
class LessonSpec(BaseModel):
    slug: str
    title: str
    objective: str = ""
    lesson_type: str = "direct_instruction"
    duration_minutes: int = 50
    minimum_viable_minutes: int = 30
    priority: Literal["required", "recommended", "optional"] = "required"
    delivery_requirement: list[str] = Field(default_factory=lambda: ["regular_teacher", "long_term_sub"])
    prerequisites: list[str] = Field(default_factory=list)  # slugs that must come before this lesson
    materials: list[str] = Field(default_factory=list)
    student_output: list[str] = Field(default_factory=list)
    standards: list[str] = Field(default_factory=list)
    required_components: list[str] = Field(default_factory=list)
    optional_components: list[str] = Field(default_factory=list)
    can_move: bool = True
    quarter_boundary_allowed: bool = False
    hard_date: Optional[date] = None
    notes: str = ""


class UnitSpec(BaseModel):
    slug: str
    title: str
    summary: str = ""
    standards: list[str] = Field(default_factory=list)
    quarter: Optional[int] = None
    planned_days: int = 10
    lessons: list[LessonSpec] = Field(default_factory=list)
    source: str = "generated"


class CurriculumSpec(BaseModel):
    subject: str
    grade: int = 7
    units: list[UnitSpec] = Field(default_factory=list)
    source_notes: list[str] = Field(default_factory=list)


class RuleSpec(BaseModel):
    text: str
    category: str = "procedure"
    scope: Literal["lesson", "unit", "course", "teacher", "default"] = "teacher"
    scope_ref: Optional[str] = None  # slug of the lesson/unit or subject of the course
    structured: dict[str, Any] = Field(default_factory=dict)


class ExtractedTeacherContext(BaseModel):
    """What the curriculum interpreter pulls out of a syllabus / pacing guide."""

    units: list[UnitSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    preferences: dict[str, str] = Field(default_factory=dict)
    materials: list[str] = Field(default_factory=list)
    assessments: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    confidence: float = 0.5


# ---------------------------------------------------------------------- planning
class CalendarDay(BaseModel):
    date: date
    section_id: int
    title: str
    kind: str = "lesson"
    lesson_slug: Optional[str] = None
    unit_slug: Optional[str] = None
    status: str = "planned"
    detail_level: str = "lesson"
    is_sub_day: bool = False
    notes: str = ""
    merged_lesson_slugs: list[str] = Field(default_factory=list)


class DecisionResult(BaseModel):
    date: date
    section_id: int
    lesson_slug: Optional[str]
    lesson_title: str
    decision: Literal["KEEP", "MODIFY", "REPLACE", "POSTPONE", "REORDER"]
    rationale: str
    replacement_slug: Optional[str] = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class DiffItem(BaseModel):
    """One line of a proposed change to the instructional calendar."""

    section_id: int
    date: date
    action: Literal["shift", "merge", "compress", "replace", "defer", "drop", "preserve", "insert", "keep"]
    before_title: Optional[str] = None
    after_title: Optional[str] = None
    before_lesson_slug: Optional[str] = None
    after_lesson_slug: Optional[str] = None
    after_kind: str = "lesson"
    merged_lesson_slugs: list[str] = Field(default_factory=list)
    is_sub_day: bool = False
    reason: str = ""
    new_date: Optional[date] = None  # for shift/defer: where the displaced lesson landed


class ProposalSpec(BaseModel):
    section_id: int
    explanation: str
    diff: list[DiffItem]
    hard_constraints: list[str] = Field(default_factory=list)
    soft_constraint_notes: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


# -------------------------------------------------------------- generated artefacts
class SubPlanContent(BaseModel):
    """The standardized substitute plan structure from the design document."""

    date: date
    period: str
    course: str
    objective: str
    materials: list[str]
    instructions: list[str] = Field(description="Ordered, timed steps the substitute follows")
    student_deliverable: str
    what_to_collect: list[str]
    early_finisher_activity: str
    classroom_rules: list[str]
    notes_for_next_day: str
    total_minutes: int
    substitute_type: str = "any_sub"
    standards: list[str] = Field(default_factory=list)


class WeeklyFramework(BaseModel):
    week_start: date
    week_end: date
    course: str
    section_id: int
    unit_titles: list[str]
    objectives: list[str]
    required_assessments: list[str]
    deadlines: list[str]
    standards: list[str]
    suggested_sequence: list[str]
    materials: list[str]
    constraints: list[str]
    flexibility_notes: str = ""


class ReturnBriefSection(BaseModel):
    course: str
    completed: list[str]
    moved: list[str]
    skipped: list[str]
    assessment_status: list[str]
    outstanding_work: list[str]
    recommended_reentry_point: str
    first_week_back: list[str]


class ReturnBrief(BaseModel):
    absence_id: int
    start_date: date
    end_date: date
    sections: list[ReturnBriefSection]
    calendar_changes: list[str]
    follow_up: list[str]


class ValidationResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
