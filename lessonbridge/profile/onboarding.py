"""First-run onboarding: build the teacher's instructional context with minimal effort.

Order of work mirrors the design document: public-first information gathering,
then the few teacher-specific inputs (school, grade, subjects, schedule,
syllabus, optional pacing guide), then review of what was found, then review
of the draft calendar.

Re-running onboarding for the same teacher updates that teacher instead of
creating a second one (LB-44); curriculum changes for sections that already
have a calendar arrive as rebuild proposals, never as silent rewrites.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, settings as default_settings
from ..curriculum.calendar import BuildReport, build_section_calendar
from ..curriculum.interpret import extract_teacher_context, infer_subject, load_curriculum, merge_curriculum, split_by_subject
from ..ingestion.pipeline import ingest_bytes, register_public_sources, sync_calendar_events
from ..models import (
    Course,
    DocumentKind,
    DocumentType,
    ReplacementActivity,
    School,
    SchoolCalendarEvent,
    Subject,
    Teacher,
    TeacherCourse,
    TeacherDocument,
    TeacherTouch,
)
from ..providers import get_provider
from ..providers.defaults import default_replacement_activities
from .rules import add_rule, seed_default_rules, set_preference

COURSE_NAMES = {"english": "English 7", "civics": "Civics and Economics"}
STANDARDS_PREFIX = {"english": "7.", "civics": "CE."}
RULE_DOC_TYPES = {DocumentType.syllabus, DocumentType.classroom_procedures, DocumentType.teacher_pacing_guide}
CURRICULUM_DOC_TYPES = {DocumentType.syllabus, DocumentType.teacher_pacing_guide, DocumentType.lesson_calendar}


class OnboardingError(ValueError):
    pass


class SectionInput(BaseModel):
    subject: str
    section_name: str
    period: str = "1"
    meeting_days: list[int] = Field(default_factory=lambda: [1, 2, 3, 4, 5])
    minutes_per_meeting: int = Field(50, ge=10, le=240)
    early_release_minutes: Optional[int] = Field(None, ge=5, le=240)
    room: Optional[str] = None
    plan_group: Optional[str] = None

    @field_validator("period", mode="before")
    @classmethod
    def _period_str(cls, v):
        return str(v)


class DocumentInput(BaseModel):
    path: Optional[str] = None
    doc_type: str = "syllabus"  # syllabus | teacher_pacing_guide | lesson_calendar | classroom_procedures | other
    subject: Optional[str] = None
    title: Optional[str] = None
    # Uploaded documents (web) arrive as bytes instead of a path.
    name: Optional[str] = None
    data: Optional[bytes] = Field(default=None, exclude=True, repr=False)


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
    preferences: dict[str, Any] = Field(default_factory=dict)
    district: str = "lcps"

    @field_validator("preferences", mode="before")
    @classmethod
    def _prefs_as_text(cls, v):
        # YAML turns `no` into False; keep the teacher's meaning as text and parse it where it is used (LB-48).
        if not isinstance(v, dict):
            return v
        out = {}
        for k, val in v.items():
            out[str(k)] = ("yes" if val else "no") if isinstance(val, bool) else str(val)
        return out

    @field_validator("planning_periods", mode="before")
    @classmethod
    def _periods_str(cls, v):
        return [str(x) for x in v] if isinstance(v, list) else v

    @classmethod
    def from_yaml(cls, path: str | Path) -> "OnboardingInput":
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise OnboardingError(f"{path} is not valid YAML: {str(exc).splitlines()[0]}") from exc
        if not isinstance(data, dict):
            raise OnboardingError("The profile must be a YAML mapping.")
        base = Path(path).parent
        for d in data.get("documents", []) or []:
            if d.get("path"):
                p = Path(d["path"])
                if not p.is_absolute():
                    d["path"] = str((base / p).resolve())
        return cls(**data)

    @classmethod
    def from_yaml_text(cls, text: str) -> "OnboardingInput":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise OnboardingError(f"The profile is not valid YAML: {str(exc).splitlines()[0]}") from exc
        if not isinstance(data, dict):
            raise OnboardingError("The profile must be a YAML mapping.")
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
    updated_existing: bool = False
    rebuild_proposals: list[int] = field(default_factory=list)


def record_touch(session: Session, teacher_id: int, workflow: str, touch_type: str, label: str) -> None:
    """Record one explicit teacher action (field, click, correction, decision or confirmation)."""
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
        slug = re.sub(r"[^a-z0-9]+", "-", school.lower()).strip("-") or "school"
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
    try:
        subj = Subject(subject)
    except ValueError as exc:
        raise OnboardingError(f"Unsupported subject {subject!r}; the MVP supports {', '.join(s.value for s in Subject)}.") from exc
    row = session.scalar(select(Course).where(Course.subject == subj, Course.grade == grade))
    if row is None:
        row = Course(subject=subj, grade=grade, name=COURSE_NAMES.get(subject, subject.title()), standards_prefix=STANDARDS_PREFIX.get(subject, ""))
        session.add(row)
        session.flush()
    return row


def seed_replacement_library(session: Session) -> int:
    existing = {(r.subject.value, r.slug): r for r in session.scalars(select(ReplacementActivity).where(ReplacementActivity.teacher_id.is_(None)))}
    n = 0
    for a in default_replacement_activities():
        row = existing.get((a["subject"], a["slug"]))
        if row is not None:
            if set(row.tags or []) != set(a["tags"]):
                row.tags = a["tags"]  # refresh unit keywords on older databases
            continue
        session.add(ReplacementActivity(teacher_id=None, subject=Subject(a["subject"]), slug=a["slug"], title=a["title"], description=a["description"], duration_minutes=a["duration_minutes"],
                                        delivery=a["delivery"], category=a["category"], materials=a["materials"], student_output=a["student_output"], tags=a["tags"]))
        n += 1
    return n


def find_teacher(session: Session, inp: OnboardingInput, school: School) -> Optional[Teacher]:
    if inp.email:
        t = session.scalar(select(Teacher).where(Teacher.email == inp.email))
        if t is not None:
            return t
    return session.scalar(select(Teacher).where(Teacher.name == inp.teacher_name, Teacher.school_id == school.id))


def _read_document(d: DocumentInput, document_root: Optional[Path]) -> tuple[bytes, str]:
    """Bytes and file name for a document. With ``document_root`` set, paths must stay inside it (LB-18)."""
    if d.data is not None:
        return d.data, d.name or d.title or "upload"
    if not d.path:
        raise OnboardingError("A document entry has neither a path nor uploaded content.")
    p = Path(d.path)
    if document_root is not None:
        root = document_root.resolve()
        p = (root / p).resolve() if not p.is_absolute() else p.resolve()
        if root != p and root not in p.parents:
            raise OnboardingError("Documents must be uploaded (or come from the examples folder); a listed path was refused.")
    if not p.is_file():
        raise OnboardingError("A listed document could not be found." if document_root is not None else f"Document not found: {p}")
    return p.read_bytes(), p.name


def run_onboarding(session: Session, inp: OnboardingInput, *, cfg: Settings | None = None, llm=None, today: date | None = None, document_root: Optional[Path] = None) -> OnboardingResult:
    cfg = cfg or default_settings
    warnings: list[str] = []
    for s in inp.subjects:
        _ensure_course(session, s, inp.grade)  # validates subjects early

    # 1. Public-first.
    sources, added_events = ensure_public_context(session, inp.district, inp.school_year, cfg)
    seed_replacement_library(session)

    # 2. Teacher basics.
    school = _ensure_school(session, inp.district, inp.school)
    teacher = find_teacher(session, inp, school)
    updating = teacher is not None
    if teacher is None:
        teacher = Teacher(name=inp.teacher_name, email=inp.email, school_id=school.id, grade=inp.grade, school_year=inp.school_year, planning_periods=inp.planning_periods)
        session.add(teacher)
        session.flush()
        record_touch(session, teacher.id, "onboarding", "field", "select school")
        record_touch(session, teacher.id, "onboarding", "confirmation", "confirm grade")
        record_touch(session, teacher.id, "onboarding", "field", "select subjects")
    else:
        teacher.grade, teacher.school_year, teacher.planning_periods = inp.grade, inp.school_year, inp.planning_periods
        if inp.email:
            teacher.email = inp.email
        warnings.append(f"Updated the existing profile for {teacher.name} instead of creating a second teacher.")
    seed_default_rules(session, teacher.id)

    sections_in = inp.sections or [SectionInput(subject=s, section_name=f"{COURSE_NAMES.get(s, s)} - Period {i+1}", period=str(i + 1)) for i, s in enumerate(inp.subjects)]
    existing_by_period = {s.period: s for s in teacher.sections}
    new_sections: list[TeacherCourse] = []
    for s in sections_in:
        if s.subject not in inp.subjects:
            raise OnboardingError(f"Section '{s.section_name}' is for {s.subject}, which is not in the subject list.")
        course = _ensure_course(session, s.subject, inp.grade)
        sec = existing_by_period.get(s.period)
        if sec is not None and sec.course_id == course.id:
            sec.section_name, sec.meeting_days, sec.minutes_per_meeting, sec.room = s.section_name, s.meeting_days, s.minutes_per_meeting, s.room
            sec.early_release_minutes = s.early_release_minutes if s.early_release_minutes is not None else sec.early_release_minutes
            continue
        if sec is not None:
            raise OnboardingError(f"Period {s.period} already exists for another course; change it in the existing profile first.")
        sec = TeacherCourse(teacher_id=teacher.id, course_id=course.id, section_name=s.section_name, period=s.period, meeting_days=s.meeting_days,
                            minutes_per_meeting=s.minutes_per_meeting, early_release_minutes=s.early_release_minutes, room=s.room, plan_group=s.plan_group or s.subject)
        session.add(sec)
        new_sections.append(sec)
        if s.early_release_minutes is None:
            warnings.append(f"Early-release period length for {s.section_name} is not set; plans assume {sec.minutes_on(True)} minutes on early-release days. Set early_release_minutes to confirm.")
    session.flush()
    if new_sections:
        record_touch(session, teacher.id, "onboarding", "field", "enter teaching schedule")
    for r in inp.rules:
        add_rule(session, teacher.id, r, source="onboarding")
        record_touch(session, teacher.id, "onboarding", "field", "classroom rule")
    for k, v in inp.preferences.items():
        set_preference(session, teacher.id, k, v)

    # 3. Teacher documents -> rules and curriculum per subject.
    extracted_by_subject: dict[str, list] = {}
    extracted_rules: list[str] = []
    errors: list[str] = []
    for d in inp.documents:
        try:
            data, name = _read_document(d, document_root)
            doc_type = DocumentType(d.doc_type)
        except (OnboardingError, ValueError) as exc:
            warnings.append(str(exc))
            continue
        res = ingest_bytes(session, data=data, name=name, kind=DocumentKind.teacher, doc_type=doc_type, title=d.title or Path(name).stem.replace("_", " ").title(),
                           scope=f"teacher:{teacher.id}", subject=d.subject, cfg=cfg)
        if not session.scalar(select(TeacherDocument).where(TeacherDocument.teacher_id == teacher.id, TeacherDocument.document_id == res.document.id)):
            session.add(TeacherDocument(teacher_id=teacher.id, document_id=res.document.id, role=doc_type.value))
        record_touch(session, teacher.id, "onboarding", "field", f"upload {doc_type.value}")
        found = interpret_document(session, teacher, res.version.parsed_text, doc_type, subject=d.subject, subjects=inp.subjects, llm=llm, label=name)
        warnings += found["warnings"]
        errors += found["errors"]
        extracted_rules += [r for r in found["rules"] if r not in extracted_rules]
        for subj, items in found["curricula"].items():
            extracted_by_subject.setdefault(subj, []).extend(items)

    curriculum_sources: dict[str, str] = {}
    conflicts: list[str] = []
    notes: list[str] = []
    build_reports: dict[str, BuildReport] = {}
    rebuilds: list[int] = []
    session.refresh(teacher)
    for subj in inp.subjects:
        ctxs = extracted_by_subject.get(subj, [])
        chosen = next(((c, m, t) for c, m, t in sorted(ctxs, key=lambda t: 0 if t[2] == DocumentType.teacher_pacing_guide else 1) if c.units), None)
        merge = merge_curriculum(subj, chosen[0] if chosen else None)
        curriculum_sources[subj] = f"{chosen[2].value} ({chosen[1]})" if chosen else "district default (inference)"
        conflicts.extend(merge.conflicts)
        notes.extend(merge.notes)
        for sec in teacher.sections:
            if sec.course.subject.value != subj:
                continue
            if not sec.calendar:
                load_curriculum(session, sec, merge.spec)
                build_reports[sec.section_name] = build_section_calendar(session, sec, today=today)
            elif chosen is not None and _signature_of_spec(merge.spec) != _signature_of_section(sec):
                from ..curriculum.calendar import propose_rebuild
                from ..absence.service import next_school_day, year

                load_curriculum(session, sec, merge.spec)
                prop, rep = propose_rebuild(session, sec, start=next_school_day(year(session, sec), (today or date.today())))
                build_reports[sec.section_name] = rep
                rebuilds.append(prop.id)
                notes.append(f"{sec.section_name} already has a calendar; the new curriculum is waiting as proposal {prop.id} for your approval.")
    for e in errors:
        warnings.append(e)

    flagged = [f"{e.date}: {e.title}" for e in session.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.school_year == inp.school_year, SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False)).order_by(SchoolCalendarEvent.date))]
    teacher.onboarding_state = {"completed": True, "curriculum_sources": curriculum_sources, "flagged_events": len(flagged)}
    return OnboardingResult(
        teacher_id=teacher.id, public_sources=sources, calendar_events=added_events, flagged_events=flagged,
        curriculum_sources=curriculum_sources, curriculum_conflicts=conflicts, curriculum_notes=notes,
        build_reports=build_reports, extracted_rules=extracted_rules, touches=count_touches(session, teacher.id, "onboarding"), warnings=warnings,
        updated_existing=updating, rebuild_proposals=rebuilds,
    )


def _signature_of_spec(spec) -> list:
    return [(u.slug, u.quarter, u.planned_days, [l.slug for l in u.lessons]) for u in spec.units]


def _signature_of_section(sec: TeacherCourse) -> list:
    return [(u.slug, u.quarter, u.planned_days, [l.slug for l in sorted(u.lessons, key=lambda l: l.sequence)]) for u in sorted(sec.active_units, key=lambda u: u.sequence)]


def interpret_document(session: Session, teacher: Teacher, text: str, doc_type: DocumentType, *, subject: Optional[str], subjects: list[str], llm=None, label: str = "document") -> dict:
    """Extract rules (stored at once) and per-subject curricula (returned) from one document.

    Used by onboarding and by uploads after onboarding (LB-33). A document with
    no subject is split at subject headings or assigned by its vocabulary; one
    that cannot be assigned contributes rules but no units (LB-29).
    """
    warnings: list[str] = []
    errors: list[str] = []
    parts: dict[str, str] = {}
    common = ""
    if subject:
        parts[subject] = text
    else:
        split = split_by_subject(text)
        common = split.pop("", "")
        parts = {k: v for k, v in split.items() if k in subjects}
        if not parts:
            guess = infer_subject(text)
            if guess in subjects:
                parts[guess] = text
                common = ""
            elif len(subjects) == 1:
                parts[subjects[0]] = text
                common = ""
            elif doc_type in CURRICULUM_DOC_TYPES:
                warnings.append(f"Could not tell which subject '{label}' is for; its rules were added but its units were not used. Set its subject and upload it again.")
    rule_texts: list[str] = []
    curricula: dict[str, list] = {}
    rule_sources = list(parts.items()) + ([("", common)] if common.strip() else [])
    if not parts and not common.strip():
        rule_sources = [("", text)]
    for subj, part in rule_sources:
        ex = extract_teacher_context(part, subj or (subjects[0] if subjects else "english"), llm=llm, doc_type=doc_type.value)
        if ex.error:
            errors.append(f"{label}: {ex.error}")
        if doc_type in RULE_DOC_TYPES:
            for rule in ex.context.rules:
                if rule.text not in rule_texts:
                    add_rule(session, teacher.id, rule.text, category=None if rule.category == "procedure" else rule.category, structured=rule.structured or None, source=f"{doc_type.value}:{ex.method}")
                    rule_texts.append(rule.text)
            for k, v in ex.context.preferences.items():
                set_preference(session, teacher.id, k, v, source=f"{doc_type.value}:{ex.method}", confidence=0.6)
        warnings += [f"{label}: {n}" for n in ex.context.notes]
        if subj and doc_type in CURRICULUM_DOC_TYPES:
            curricula.setdefault(subj, []).append((ex.context, ex.method, doc_type))
    return {"rules": rule_texts, "curricula": curricula, "warnings": warnings, "errors": errors}


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
