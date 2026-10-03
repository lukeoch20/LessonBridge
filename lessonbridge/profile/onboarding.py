"""First-run onboarding: build the teacher's instructional context with minimal effort.

Order of work mirrors the design document: public-first information gathering,
then the few teacher-specific inputs (school, grade, subjects, schedule,
syllabus, optional pacing guide), then review of what was found, then review
of the draft calendar.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, settings as default_settings
from ..curriculum.calendar import BuildReport, build_section_calendar
from ..curriculum.interpret import extract_teacher_context, load_curriculum, merge_curriculum
from ..ingestion.pipeline import ingest_file, register_public_sources, sync_calendar_events
from ..models import (
    Course,
    DocumentType,
    ReplacementActivity,
    School,
    SchoolCalendarEvent,
    Subject,
    Teacher,
    TeacherCourse,
    TeacherTouch,
)
from ..providers import get_provider
from ..providers.defaults import default_replacement_activities
from .rules import add_rule, seed_default_rules, set_preference

COURSE_NAMES = {"english": "English 7", "civics": "Civics and Economics"}
STANDARDS_PREFIX = {"english": "7.", "civics": "CE."}


class SectionInput(BaseModel):
    subject: str
    section_name: str
    period: str = "1"
    meeting_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    minutes_per_meeting: int = 50
    room: Optional[str] = None
    plan_group: Optional[str] = None


class DocumentInput(BaseModel):
    path: str
    doc_type: str = "syllabus"  # syllabus | teacher_pacing_guide | lesson_calendar | classroom_procedures | other
    subject: Optional[str] = None
    title: Optional[str] = None


class OnboardingInput(BaseModel):
    teacher_name: str
    email: Optional[str] = None
    school: str  # slug or name
    grade: int = 7
    subjects: list[str] = Field(default_factory=lambda: ["english", "civics"])
    school_year: str = "2026-2027"
    sections: list[SectionInput] = Field(default_factory=list)
    planning_periods: list[str] = Field(default_factory=list)
    documents: list[DocumentInput] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    preferences: dict[str, str] = Field(default_factory=dict)
    district: str = "lcps"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "OnboardingInput":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        base = Path(path).parent
        for d in data.get("documents", []):
            p = Path(d["path"])
            if not p.is_absolute():
                d["path"] = str((base / p).resolve())
        return cls(**data)


@dataclass
class OnboardingResult:
    teacher_id: int
    public_sources: list[str]
    calendar_events: int
    flagged_events: list[str]
    curriculum_sources: dict[str, str]
    curriculum_conflicts: list[str]
    curriculum_notes: list[str]
    build_reports: dict[str, BuildReport]
    extracted_rules: list[str]
    touches: int
    warnings: list[str] = field(default_factory=list)


def record_touch(session: Session, teacher_id: int, workflow: str, touch_type: str, label: str) -> None:
    session.add(TeacherTouch(teacher_id=teacher_id, workflow=workflow, touch_type=touch_type, label=label))


def count_touches(session: Session, teacher_id: int, workflow: str | None = None) -> int:
    q = select(func.count(TeacherTouch.id)).where(TeacherTouch.teacher_id == teacher_id)
    if workflow:
        q = q.where(TeacherTouch.workflow == workflow)
    return int(session.scalar(q) or 0)


def ensure_public_context(session: Session, district: str, school_year: str, cfg: Settings | None = None) -> tuple[list[str], int]:
    provider = get_provider(district)
    results = register_public_sources(session, provider, cfg=cfg or default_settings)
    added = sync_calendar_events(session, provider, school_year)
    return [r.document.title for r in results], added


def _ensure_school(session: Session, district: str, school: str) -> School:
    provider = get_provider(district)
    known = {s["slug"]: s["name"] for s in provider.get_schools()}
    slug = school if school in known else next((k for k, v in known.items() if v.lower() == school.lower()), None)
    if slug is None:
        import re

        slug = re.sub(r"[^a-z0-9]+", "-", school.lower()).strip("-")
        name = school
    else:
        name = known[slug]
    row = session.scalar(select(School).where(School.slug == slug))
    if row is None:
        row = School(slug=slug, name=name, district_slug=provider.slug, district_name=provider.name, state=provider.state)
        session.add(row)
        session.flush()
    return row


def _ensure_course(session: Session, subject: str, grade: int) -> Course:
    row = session.scalar(select(Course).where(Course.subject == Subject(subject), Course.grade == grade))
    if row is None:
        row = Course(subject=Subject(subject), grade=grade, name=COURSE_NAMES.get(subject, subject.title()), standards_prefix=STANDARDS_PREFIX.get(subject, ""))
        session.add(row)
        session.flush()
    return row


def seed_replacement_library(session: Session) -> int:
    existing = {(r.subject.value, r.slug) for r in session.scalars(select(ReplacementActivity).where(ReplacementActivity.teacher_id.is_(None)))}
    n = 0
    for a in default_replacement_activities():
        if (a["subject"], a["slug"]) in existing:
            continue
        session.add(ReplacementActivity(teacher_id=None, subject=Subject(a["subject"]), slug=a["slug"], title=a["title"], description=a["description"], duration_minutes=a["duration_minutes"], delivery=a["delivery"], category=a["category"], materials=a["materials"], student_output=a["student_output"], tags=[a["category"]]))
        n += 1
    return n


def run_onboarding(session: Session, inp: OnboardingInput, *, cfg: Settings | None = None, llm=None, today: date | None = None) -> OnboardingResult:
    cfg = cfg or default_settings
    warnings: list[str] = []

    # 1. Public-first.
    sources, added_events = ensure_public_context(session, inp.district, inp.school_year, cfg)
    seed_replacement_library(session)

    # 2. Teacher basics (each is a touch).
    school = _ensure_school(session, inp.district, inp.school)
    teacher = Teacher(name=inp.teacher_name, email=inp.email, school_id=school.id, grade=inp.grade, school_year=inp.school_year, planning_periods=inp.planning_periods)
    session.add(teacher)
    session.flush()
    record_touch(session, teacher.id, "onboarding", "field", "select school")
    record_touch(session, teacher.id, "onboarding", "confirmation", "confirm grade")
    record_touch(session, teacher.id, "onboarding", "field", "select subjects")
    seed_default_rules(session, teacher.id)

    sections_in = inp.sections or [SectionInput(subject=s, section_name=f"{COURSE_NAMES.get(s, s)} - Period {i+1}", period=str(i + 1)) for i, s in enumerate(inp.subjects)]
    sections: list[TeacherCourse] = []
    for s in sections_in:
        course = _ensure_course(session, s.subject, inp.grade)
        sec = TeacherCourse(teacher_id=teacher.id, course_id=course.id, section_name=s.section_name, period=s.period, meeting_days=s.meeting_days, minutes_per_meeting=s.minutes_per_meeting, room=s.room, plan_group=s.plan_group or s.subject)
        session.add(sec)
        sections.append(sec)
    session.flush()
    record_touch(session, teacher.id, "onboarding", "field", "enter teaching schedule")
    for r in inp.rules:
        add_rule(session, teacher.id, r, source="onboarding")
        record_touch(session, teacher.id, "onboarding", "field", "classroom rule")
    for k, v in inp.preferences.items():
        set_preference(session, teacher.id, k, v)

    # 3. Teacher documents -> curriculum per subject.
    extracted_by_subject: dict[str, list] = {}
    extracted_rules: list[str] = []
    for d in inp.documents:
        p = Path(d.path)
        if not p.exists():
            warnings.append(f"Document not found: {p}")
            continue
        doc_type = DocumentType(d.doc_type)
        res = ingest_file(session, p, teacher_id=teacher.id, doc_type=doc_type, title=d.title, subject=d.subject, cfg=cfg)
        record_touch(session, teacher.id, "onboarding", "field", f"upload {doc_type.value}")
        subjects = [d.subject] if d.subject else inp.subjects
        for subj in subjects:
            ctx, method = extract_teacher_context(res.version.parsed_text, subj, llm=llm)
            extracted_by_subject.setdefault(subj, []).append((ctx, method, doc_type))
            if doc_type in (DocumentType.syllabus, DocumentType.classroom_procedures):
                for rule in ctx.rules:
                    if rule.text not in extracted_rules:
                        add_rule(session, teacher.id, rule.text, category=rule.category, structured=rule.structured or None, source=f"{doc_type.value}:{method}")
                        extracted_rules.append(rule.text)
                for k, v in ctx.preferences.items():
                    set_preference(session, teacher.id, k, v, source=f"{doc_type.value}:{method}", confidence=0.6)

    curriculum_sources: dict[str, str] = {}
    conflicts: list[str] = []
    notes: list[str] = []
    build_reports: dict[str, BuildReport] = {}
    session.refresh(teacher)
    for subj in inp.subjects:
        ctxs = extracted_by_subject.get(subj, [])
        # Pacing guide beats syllabus for sequence; use whichever has units, pacing guide first.
        chosen = None
        for ctx, method, doc_type in sorted(ctxs, key=lambda t: 0 if t[2] == DocumentType.teacher_pacing_guide else 1):
            if ctx.units:
                chosen = (ctx, method, doc_type)
                break
        merge = merge_curriculum(subj, chosen[0] if chosen else None)
        curriculum_sources[subj] = f"{chosen[2].value} ({chosen[1]})" if chosen else "district default (inference)"
        conflicts.extend(merge.conflicts)
        notes.extend(merge.notes)
        for sec in sections:
            if sec.course.subject.value != subj:
                continue
            load_curriculum(session, sec, merge.spec)
            build_reports[sec.section_name] = build_section_calendar(session, sec, today=today)
    record_touch(session, teacher.id, "onboarding", "click", "review public information")
    record_touch(session, teacher.id, "onboarding", "click", "review draft calendar")

    flagged = [f"{e.date}: {e.title}" for e in session.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.school_year == inp.school_year, SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False)).order_by(SchoolCalendarEvent.date))]
    teacher.onboarding_state = {"completed": True, "curriculum_sources": curriculum_sources, "flagged_events": len(flagged)}
    return OnboardingResult(
        teacher_id=teacher.id, public_sources=sources, calendar_events=added_events, flagged_events=flagged,
        curriculum_sources=curriculum_sources, curriculum_conflicts=conflicts, curriculum_notes=notes,
        build_reports=build_reports, extracted_rules=extracted_rules, touches=count_touches(session, teacher.id, "onboarding"), warnings=warnings,
    )


def confirm_calendar(session: Session, teacher_id: int, school_year: str, *, event_ids: list[int] | None = None) -> int:
    q = select(SchoolCalendarEvent).where(SchoolCalendarEvent.school_year == school_year, SchoolCalendarEvent.confirmed.is_(False))
    if event_ids:
        q = q.where(SchoolCalendarEvent.id.in_(event_ids))
    n = 0
    for ev in session.scalars(q):
        ev.confirmed = True
        ev.needs_confirmation = False
        n += 1
    record_touch(session, teacher_id, "onboarding", "confirmation", f"confirm {n} calendar dates")
    return n
