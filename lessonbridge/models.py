"""SQLAlchemy ORM model.

The entity list follows the brainstorming document: teachers, schools, courses,
teacher_courses, documents, document_versions, teacher_documents,
school_calendar_events, curriculum_units, lessons, lesson_dependencies,
instructional_calendar, teacher_preferences, teacher_rules,
replacement_activities, absence_events, absence_decisions and sub_plans.

A few supporting tables are added for the behaviours the document asks for:
source registrations (versioned public sources), proposals (calendar diffs that
wait for approval), calendar changes (audit trail), generation attempts (the
quality gate log), leave plans and teacher touches (the effort metric).
"""
from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    text,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------- enums
class Subject(str, enum.Enum):
    english = "english"
    civics = "civics"


class DocumentKind(str, enum.Enum):
    public = "public"
    teacher = "teacher"
    generated = "generated"


class DocumentType(str, enum.Enum):
    academic_calendar = "academic_calendar"
    grading_periods = "grading_periods"
    standards = "standards"
    curriculum_framework = "curriculum_framework"
    course_description = "course_description"
    pacing_guide = "pacing_guide"
    assessment_calendar = "assessment_calendar"
    syllabus = "syllabus"
    teacher_pacing_guide = "teacher_pacing_guide"
    lesson_calendar = "lesson_calendar"
    classroom_procedures = "classroom_procedures"
    other = "other"


class CalendarEventType(str, enum.Enum):
    first_day = "first_day"
    last_day = "last_day"
    holiday = "holiday"
    teacher_workday = "teacher_workday"
    early_release = "early_release"
    quarter_start = "quarter_start"
    quarter_end = "quarter_end"
    grading_deadline = "grading_deadline"
    testing_window = "testing_window"
    no_school = "no_school"
    other = "other"


class LessonType(str, enum.Enum):
    direct_instruction = "direct_instruction"
    guided_practice = "guided_practice"
    independent_practice = "independent_practice"
    discussion = "discussion"
    writing_workshop = "writing_workshop"
    review = "review"
    assessment = "assessment"
    project = "project"
    reading = "reading"
    enrichment = "enrichment"


class Priority(str, enum.Enum):
    required = "required"
    recommended = "recommended"
    optional = "optional"


class Delivery(str, enum.Enum):
    regular_teacher = "regular_teacher"
    long_term_sub = "long_term_sub"
    any_sub = "any_sub"
    independent = "independent"


class EntryKind(str, enum.Enum):
    lesson = "lesson"
    review = "review"
    assessment = "assessment"
    filler = "filler"
    placeholder = "placeholder"
    flex = "flex"


class EntryStatus(str, enum.Enum):
    planned = "planned"
    completed = "completed"
    skipped = "skipped"


class DetailLevel(str, enum.Enum):
    unit = "unit"
    lesson = "lesson"
    detailed = "detailed"


class RuleScope(str, enum.Enum):
    lesson = "lesson"
    unit = "unit"
    course = "course"
    teacher = "teacher"
    default = "default"


class AbsenceType(str, enum.Enum):
    single_day = "single_day"
    short = "short"
    extended_leave = "extended_leave"


class SubstituteType(str, enum.Enum):
    any_sub = "any_sub"
    long_term_sub = "long_term_sub"
    none = "none"


class AbsenceStatus(str, enum.Enum):
    planned = "planned"
    active = "active"
    completed = "completed"
    cancelled = "cancelled"


class Decision(str, enum.Enum):
    KEEP = "KEEP"
    MODIFY = "MODIFY"
    REPLACE = "REPLACE"
    POSTPONE = "POSTPONE"
    REORDER = "REORDER"


class ProposalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    superseded = "superseded"  # another approval changed the calendar this proposal was computed against
    stale = "stale"  # approval found the live calendar no longer matches the proposal's before-state
    blocked = "blocked"  # failed the invariant gate; cannot be approved
    reverted = "reverted"  # approved, then undone by an approved undo proposal


class SubPlanStatus(str, enum.Enum):
    draft = "draft"
    failed_validation = "failed_validation"
    accepted = "accepted"
    stale = "stale"  # the calendar day changed after the plan was generated


# --------------------------------------------------------------------------- core
class School(Base):
    __tablename__ = "schools"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    district_slug: Mapped[str] = mapped_column(String(64), default="lcps")
    district_name: Mapped[str] = mapped_column(String(200), default="Loudoun County Public Schools")
    state: Mapped[str] = mapped_column(String(2), default="VA")
    level: Mapped[str] = mapped_column(String(32), default="middle")

    teachers: Mapped[list["Teacher"]] = relationship(back_populates="school")


class Teacher(Base):
    __tablename__ = "teachers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    school_id: Mapped[int] = mapped_column(ForeignKey("schools.id"))
    grade: Mapped[int] = mapped_column(Integer, default=7)
    school_year: Mapped[str] = mapped_column(String(9), default="2026-2027")
    planning_periods: Mapped[list[Any]] = mapped_column(JSON, default=list)
    onboarding_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    school: Mapped[School] = relationship(back_populates="teachers")
    sections: Mapped[list["TeacherCourse"]] = relationship(back_populates="teacher", cascade="all, delete-orphan")
    rules: Mapped[list["TeacherRule"]] = relationship(back_populates="teacher", cascade="all, delete-orphan")
    preferences: Mapped[list["TeacherPreference"]] = relationship(back_populates="teacher", cascade="all, delete-orphan")
    absences: Mapped[list["AbsenceEvent"]] = relationship(back_populates="teacher", cascade="all, delete-orphan")
    touches: Mapped[list["TeacherTouch"]] = relationship(cascade="all, delete-orphan")
    proposals: Mapped[list["Proposal"]] = relationship(cascade="all, delete-orphan", overlaps="absence,proposals")
    calendar_changes: Mapped[list["CalendarChange"]] = relationship(cascade="all, delete-orphan")
    document_links: Mapped[list["TeacherDocument"]] = relationship(cascade="all, delete-orphan")


class Course(Base):
    __tablename__ = "courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[Subject] = mapped_column(Enum(Subject))
    grade: Mapped[int] = mapped_column(Integer, default=7)
    name: Mapped[str] = mapped_column(String(200))
    standards_prefix: Mapped[str] = mapped_column(String(16), default="")
    __table_args__ = (UniqueConstraint("subject", "grade", name="uq_course_subject_grade"),)


class TeacherCourse(Base):
    """A section the teacher actually teaches: course + period + meeting pattern."""

    __tablename__ = "teacher_courses"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"))
    section_name: Mapped[str] = mapped_column(String(100))
    period: Mapped[str] = mapped_column(String(16), default="1")
    # ISO weekday numbers the section meets (1=Mon .. 5=Fri).
    meeting_days: Mapped[list[Any]] = mapped_column(JSON, default=lambda: [1, 2, 3, 4, 5])
    minutes_per_meeting: Mapped[int] = mapped_column(Integer, default=50)
    room: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Sections of the same course that share one plan (e.g. three English blocks).
    plan_group: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Length of this section's period on early-release days (LB-53); None = not yet confirmed.
    early_release_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    teacher: Mapped[Teacher] = relationship(back_populates="sections")
    course: Mapped[Course] = relationship()
    units: Mapped[list["CurriculumUnit"]] = relationship(back_populates="section", cascade="all, delete-orphan", order_by="CurriculumUnit.sequence")
    calendar: Mapped[list["CalendarEntry"]] = relationship(back_populates="section", cascade="all, delete-orphan", order_by="CalendarEntry.date")
    owed: Mapped[list["OwedLesson"]] = relationship(back_populates="section", cascade="all, delete-orphan")

    @property
    def active_units(self) -> list["CurriculumUnit"]:
        return [u for u in self.units if u.active]

    def minutes_on(self, is_early_release: bool) -> int:
        if is_early_release:
            return self.early_release_minutes or max(20, int(round(self.minutes_per_meeting * 0.6 / 5.0) * 5))
        return self.minutes_per_meeting


# ---------------------------------------------------------------------- documents
class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[DocumentKind] = mapped_column(Enum(DocumentKind))
    doc_type: Mapped[DocumentType] = mapped_column(Enum(DocumentType))
    title: Mapped[str] = mapped_column(String(300))
    # Scope key lets district-level documents be shared by every teacher.
    scope: Mapped[str] = mapped_column(String(64), default="district:lcps")
    source_url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subject: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    versions: Mapped[list["DocumentVersion"]] = relationship(back_populates="document", cascade="all, delete-orphan", order_by="DocumentVersion.version_no")

    @property
    def current_version(self) -> Optional["DocumentVersion"]:
        for v in reversed(self.versions):
            if v.is_current:
                return v
        return self.versions[-1] if self.versions else None


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    version_no: Mapped[int] = mapped_column(Integer, default=1)
    checksum: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(100), default="text/plain")
    file_path: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    parsed_text: Mapped[str] = mapped_column(Text, default="")
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)

    document: Mapped[Document] = relationship(back_populates="versions")


class TeacherDocument(Base):
    __tablename__ = "teacher_documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    teacher_course_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"), nullable=True)
    role: Mapped[str] = mapped_column(String(64), default="reference")

    document: Mapped[Document] = relationship()


class SourceRegistration(Base):
    """Tracks a public source so it is refreshed on a schedule, never on every open."""

    __tablename__ = "source_registrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(128))
    url: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id"), nullable=True)
    refresh_interval_days: Mapped[int] = mapped_column(Integer, default=30)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    current_checksum: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_version_no: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="never_fetched")
    origin: Mapped[str] = mapped_column(String(32), default="snapshot")  # snapshot | live
    __table_args__ = (UniqueConstraint("provider", "source_key", name="uq_source_provider_key"),)

    document: Mapped[Optional[Document]] = relationship()


class SchoolCalendarEvent(Base):
    __tablename__ = "school_calendar_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    district_slug: Mapped[str] = mapped_column(String(64), default="lcps")
    school_year: Mapped[str] = mapped_column(String(9), default="2026-2027")
    date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    event_type: Mapped[CalendarEventType] = mapped_column(Enum(CalendarEventType))
    title: Mapped[str] = mapped_column(String(200))
    instructional: Mapped[bool] = mapped_column(Boolean, default=False)
    quarter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    source_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("document_versions.id"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# --------------------------------------------------------------------- curriculum
class CurriculumUnit(Base):
    __tablename__ = "curriculum_units"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_course_id: Mapped[int] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    slug: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    standards: Mapped[list[Any]] = mapped_column(JSON, default=list)
    quarter: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    planned_days: Mapped[int] = mapped_column(Integer, default=10)
    source: Mapped[str] = mapped_column(String(32), default="generated")  # generated | syllabus | pacing_guide | teacher
    # Superseded curriculum versions stay for history (completed days reference them) but are not planned.
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))

    section: Mapped[TeacherCourse] = relationship(back_populates="units")
    lessons: Mapped[list["Lesson"]] = relationship(back_populates="unit", cascade="all, delete-orphan", order_by="Lesson.sequence")


class Lesson(Base):
    __tablename__ = "lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("curriculum_units.id", ondelete="CASCADE"))
    sequence: Mapped[int] = mapped_column(Integer)
    slug: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text, default="")
    lesson_type: Mapped[LessonType] = mapped_column(Enum(LessonType), default=LessonType.direct_instruction)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=50)
    minimum_viable_minutes: Mapped[int] = mapped_column(Integer, default=30)
    priority: Mapped[Priority] = mapped_column(Enum(Priority), default=Priority.required)
    delivery_requirement: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["regular_teacher", "long_term_sub"])
    materials: Mapped[list[Any]] = mapped_column(JSON, default=list)
    student_output: Mapped[list[Any]] = mapped_column(JSON, default=list)
    standards: Mapped[list[Any]] = mapped_column(JSON, default=list)
    required_components: Mapped[list[Any]] = mapped_column(JSON, default=list)
    optional_components: Mapped[list[Any]] = mapped_column(JSON, default=list)
    can_move: Mapped[bool] = mapped_column(Boolean, default=True)
    quarter_boundary_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    hard_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # curriculum | syllabus | drafted (filled from a placeholder by LessonBridge, needs teacher review) | teacher
    origin: Mapped[str] = mapped_column(String(32), default="curriculum", server_default=text("'curriculum'"))

    unit: Mapped[CurriculumUnit] = relationship(back_populates="lessons")
    dependencies: Mapped[list["LessonDependency"]] = relationship(
        back_populates="lesson", cascade="all, delete-orphan", foreign_keys="LessonDependency.lesson_id"
    )

    @property
    def is_assessment(self) -> bool:
        return self.lesson_type == LessonType.assessment

    def allows(self, delivery: str) -> bool:
        return delivery in (self.delivery_requirement or [])

    @property
    def prerequisite_slugs(self) -> set[str]:
        return {d.depends_on.slug for d in self.dependencies}


class LessonDependency(Base):
    """`lesson` must come after `depends_on`."""

    __tablename__ = "lesson_dependencies"
    id: Mapped[int] = mapped_column(primary_key=True)
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    depends_on_lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id"))
    kind: Mapped[str] = mapped_column(String(32), default="before")

    lesson: Mapped[Lesson] = relationship(back_populates="dependencies", foreign_keys=[lesson_id])
    depends_on: Mapped[Lesson] = relationship(foreign_keys=[depends_on_lesson_id])


class CalendarEntry(Base):
    """One course-day on the instructional calendar (curriculum -> date mapping)."""

    __tablename__ = "instructional_calendar"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_course_id: Mapped[int] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"))
    date: Mapped[date] = mapped_column(Date)
    lesson_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lessons.id"), nullable=True)
    unit_id: Mapped[Optional[int]] = mapped_column(ForeignKey("curriculum_units.id"), nullable=True)
    kind: Mapped[EntryKind] = mapped_column(Enum(EntryKind), default=EntryKind.lesson)
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[EntryStatus] = mapped_column(Enum(EntryStatus), default=EntryStatus.planned)
    detail_level: Mapped[DetailLevel] = mapped_column(Enum(DetailLevel), default=DetailLevel.lesson)
    origin: Mapped[str] = mapped_column(String(32), default="generated")
    is_sub_day: Mapped[bool] = mapped_column(Boolean, default=False)
    # Lessons merged into this day (compression), in teaching order, before the entry's own lesson.
    merged_lesson_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # Planner identity of merged items, e.g. ["federalism-intro", "launch-annotation#c1"].
    merged_keys: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    # 0 for the lesson itself; n for its n-th "(continued)" day after a slip.
    continuation: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Which absence made this a substitute day, and which replacement activity it carries.
    absence_id: Mapped[Optional[int]] = mapped_column(ForeignKey("absence_events.id", ondelete="SET NULL"), nullable=True)
    replacement_activity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("replacement_activities.id", ondelete="SET NULL"), nullable=True)
    sub_plan_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sub_plans.id", use_alter=True, ondelete="SET NULL"), nullable=True)

    section: Mapped[TeacherCourse] = relationship(back_populates="calendar")
    lesson: Mapped[Optional[Lesson]] = relationship()
    unit: Mapped[Optional[CurriculumUnit]] = relationship()
    replacement_activity: Mapped[Optional["ReplacementActivity"]] = relationship()

    @property
    def is_slack(self) -> bool:
        return self.lesson_id is None and self.kind in (EntryKind.flex, EntryKind.placeholder) and not self.is_sub_day
    __table_args__ = (UniqueConstraint("teacher_course_id", "date", name="uq_calendar_section_date"),)


# ----------------------------------------------------------------- teacher context
class TeacherPreference(Base):
    __tablename__ = "teacher_preferences"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="onboarding")  # onboarding | correction | inferred
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("teacher_id", "key", name="uq_pref_teacher_key"),)

    teacher: Mapped[Teacher] = relationship(back_populates="preferences")


class TeacherRule(Base):
    """A mandatory classroom rule, scoped from a single lesson up to the whole teacher."""

    __tablename__ = "teacher_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    scope: Mapped[RuleScope] = mapped_column(Enum(RuleScope), default=RuleScope.teacher)
    scope_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(64), default="procedure")
    text: Mapped[str] = mapped_column(Text)
    # Machine-readable form, e.g. {"trigger": "assessment", "action": "study_period", "minutes": 15}
    structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(32), default="teacher")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    teacher: Mapped[Teacher] = relationship(back_populates="rules")


class ReplacementActivity(Base):
    __tablename__ = "replacement_activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"), nullable=True)
    subject: Mapped[Subject] = mapped_column(Enum(Subject))
    slug: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_minutes: Mapped[int] = mapped_column(Integer, default=45)
    delivery: Mapped[str] = mapped_column(String(32), default="any_sub")
    category: Mapped[str] = mapped_column(String(32), default="curriculum_preserving")  # curriculum_preserving | skill_maintenance | emergency_filler
    tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    materials: Mapped[list[Any]] = mapped_column(JSON, default=list)
    student_output: Mapped[str] = mapped_column(String(200), default="")
    times_used: Mapped[int] = mapped_column(Integer, default=0)


# ----------------------------------------------------------------------- absences
class AbsenceEvent(Base):
    __tablename__ = "absence_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    absence_type: Mapped[AbsenceType] = mapped_column(Enum(AbsenceType))
    substitute_type: Mapped[SubstituteType] = mapped_column(Enum(SubstituteType), default=SubstituteType.any_sub)
    status: Mapped[AbsenceStatus] = mapped_column(Enum(AbsenceStatus), default=AbsenceStatus.planned)
    reason: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    teacher: Mapped[Teacher] = relationship(back_populates="absences")
    decisions: Mapped[list["AbsenceDecision"]] = relationship(back_populates="absence", cascade="all, delete-orphan")
    sub_plans: Mapped[list["SubPlan"]] = relationship(back_populates="absence", cascade="all, delete-orphan")
    proposals: Mapped[list["Proposal"]] = relationship(back_populates="absence", cascade="all, delete-orphan")
    leave_plan: Mapped[Optional["LeavePlan"]] = relationship(back_populates="absence", uselist=False, cascade="all, delete-orphan")


class AbsenceDecision(Base):
    __tablename__ = "absence_decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    absence_id: Mapped[int] = mapped_column(ForeignKey("absence_events.id", ondelete="CASCADE"))
    calendar_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instructional_calendar.id", ondelete="SET NULL"), nullable=True)
    lesson_id: Mapped[Optional[int]] = mapped_column(ForeignKey("lessons.id", ondelete="SET NULL"), nullable=True)
    teacher_course_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"), nullable=True)
    date: Mapped[date] = mapped_column(Date)
    decision: Mapped[Decision] = mapped_column(Enum(Decision))
    rationale: Mapped[str] = mapped_column(Text, default="")
    replacement_activity_id: Mapped[Optional[int]] = mapped_column(ForeignKey("replacement_activities.id"), nullable=True)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    absence: Mapped[AbsenceEvent] = relationship(back_populates="decisions")
    replacement_activity: Mapped[Optional[ReplacementActivity]] = relationship()


class Proposal(Base):
    """A proposed calendar change set shown to the teacher as a diff before it is applied."""

    __tablename__ = "proposals"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    absence_id: Mapped[Optional[int]] = mapped_column(ForeignKey("absence_events.id", ondelete="CASCADE"), nullable=True)
    teacher_course_id: Mapped[Optional[int]] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"), nullable=True)
    # absence_reconciliation | slip_reconciliation | rebuild | backlog_placement | cancel_absence | undo | curriculum
    kind: Mapped[str] = mapped_column(String(32), default="reconciliation")
    status: Mapped[ProposalStatus] = mapped_column(Enum(ProposalStatus), default=ProposalStatus.pending)
    explanation: Mapped[str] = mapped_column(Text, default="")
    diff: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # hard, soft, unresolved, decisions_required, violations
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Lessons this proposal sends to (or takes from) the owed-lesson backlog.
    backlog: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    window_start: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    window_end: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    reverts_proposal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    absence: Mapped[Optional[AbsenceEvent]] = relationship(back_populates="proposals")


class CalendarChange(Base):
    __tablename__ = "calendar_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    proposal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proposals.id"), nullable=True)
    teacher_course_id: Mapped[int] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"))
    change_type: Mapped[str] = mapped_column(String(32))
    date: Mapped[date] = mapped_column(Date)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SubPlan(Base):
    __tablename__ = "sub_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    absence_id: Mapped[int] = mapped_column(ForeignKey("absence_events.id", ondelete="CASCADE"))
    teacher_course_id: Mapped[int] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"))
    calendar_entry_id: Mapped[Optional[int]] = mapped_column(ForeignKey("instructional_calendar.id", ondelete="SET NULL"), nullable=True)
    date: Mapped[date] = mapped_column(Date)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Fingerprint of the calendar day the plan was generated for; a mismatch marks the plan stale (LB-37).
    entry_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    rendered_markdown: Mapped[str] = mapped_column(Text, default="")
    generator: Mapped[str] = mapped_column(String(32), default="template")
    model: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[SubPlanStatus] = mapped_column(Enum(SubPlanStatus), default=SubPlanStatus.draft)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    absence: Mapped[AbsenceEvent] = relationship(back_populates="sub_plans")
    section: Mapped[TeacherCourse] = relationship()
    generation_attempts: Mapped[list["GenerationAttempt"]] = relationship(back_populates="sub_plan", cascade="all, delete-orphan")


class GenerationAttempt(Base):
    __tablename__ = "generation_attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    sub_plan_id: Mapped[int] = mapped_column(ForeignKey("sub_plans.id", ondelete="CASCADE"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    failures: Mapped[list[Any]] = mapped_column(JSON, default=list)
    generator: Mapped[str] = mapped_column(String(32), default="template")
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sub_plan: Mapped[SubPlan] = relationship(back_populates="generation_attempts")


class OwedLesson(Base):
    """A lesson that is owed but has no calendar day: the backlog (LB-01).

    Created when an approved proposal cannot place a lesson (``owed``) or drops
    optional / recommended content (``dropped``). Owed lessons are offered to the
    next planning run for the section and to the explicit "schedule owed lessons"
    action; nothing is ever silently deleted.
    """

    __tablename__ = "owed_lessons"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_course_id: Mapped[int] = mapped_column(ForeignKey("teacher_courses.id", ondelete="CASCADE"))
    lesson_id: Mapped[int] = mapped_column(ForeignKey("lessons.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="owed")  # owed | dropped
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by_proposal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True)
    resolved_by_proposal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("proposals.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    section: Mapped[TeacherCourse] = relationship(back_populates="owed")
    lesson: Mapped[Lesson] = relationship()

    @property
    def open(self) -> bool:
        return self.resolved_at is None


class LeavePlan(Base):
    """Extended-leave artefacts: pre-leave analysis, handoff packet, weekly frameworks, return brief."""

    __tablename__ = "leave_plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    absence_id: Mapped[int] = mapped_column(ForeignKey("absence_events.id", ondelete="CASCADE"), unique=True)
    pre_leave: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    handoff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    weekly_frameworks: Mapped[list[Any]] = mapped_column(JSON, default=list)
    return_brief: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    absence: Mapped[AbsenceEvent] = relationship(back_populates="leave_plan")


class TeacherTouch(Base):
    """Every field, click, correction or decision we asked of the teacher. The product metric."""

    __tablename__ = "teacher_touches"
    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    workflow: Mapped[str] = mapped_column(String(64))
    touch_type: Mapped[str] = mapped_column(String(32))  # field | click | correction | decision | confirmation
    label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
