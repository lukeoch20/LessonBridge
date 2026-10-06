"""Ingestion pipeline.

Source -> Discover -> Download -> Validate -> Extract -> Normalize metadata ->
Store original -> Store parsed content -> Index -> Register version.

Public sources are versioned by checksum and refreshed on an interval, never on
every open. Teacher uploads follow the same path with ``kind="teacher"``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, settings as default_settings
from ..models import (
    CalendarEventType,
    Document,
    DocumentKind,
    DocumentType,
    DocumentVersion,
    SchoolCalendarEvent,
    SourceRegistration,
    TeacherDocument,
    utcnow,
)
from ..providers.base import DistrictProvider
from ..schemas import SourcePayload
from .extract import extract_text, guess_content_type
from .index import index_version, remove_version


@dataclass
class IngestResult:
    document: Document
    version: DocumentVersion
    created_version: bool
    chunks: int


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_original(cfg: Settings, scope: str, key: str, version_no: int, data: bytes, suffix: str) -> str:
    folder = cfg.documents_dir / scope.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.v{version_no}{suffix}"
    path.write_bytes(data)
    return str(path)


def _suffix_for(content_type: str, name: str = "") -> str:
    if name and "." in Path(name).name:
        return Path(name).suffix.lower()
    for ext, ct in {".pdf": "pdf", ".docx": "wordprocessingml", ".xlsx": "spreadsheetml", ".csv": "csv", ".html": "html", ".json": "json", ".md": "markdown"}.items():
        if ct in content_type:
            return ext
    return ".txt"


def ingest_bytes(
    session: Session,
    *,
    data: bytes,
    name: str,
    kind: DocumentKind,
    doc_type: DocumentType,
    title: str,
    scope: str,
    content_type: Optional[str] = None,
    source_url: Optional[str] = None,
    provider: Optional[str] = None,
    subject: Optional[str] = None,
    structured: Optional[dict] = None,
    text_override: Optional[str] = None,
    as_new_version: bool = True,
    cfg: Settings | None = None,
) -> IngestResult:
    """Validate, extract, store, index and version one document payload."""
    cfg = cfg or default_settings
    cfg.ensure_dirs()
    if not data:
        raise ValueError("Empty document payload")
    content_type = content_type or guess_content_type(name)
    digest = checksum(data)

    # Identity includes the subject, so an English and a Civics "syllabus" stay separate documents (LB-45).
    q = select(Document).where(Document.scope == scope, Document.doc_type == doc_type, Document.title == title)
    q = q.where(Document.subject.is_(None)) if subject is None else q.where(Document.subject == subject)
    doc = session.scalar(q)
    if doc is not None and not as_new_version:
        current_cs = session.scalar(select(DocumentVersion.checksum).where(DocumentVersion.document_id == doc.id, DocumentVersion.is_current.is_(True)))
        if current_cs != digest:
            n = 2
            while session.scalar(select(Document).where(Document.scope == scope, Document.doc_type == doc_type, Document.title == f"{title} ({n})")):
                n += 1
            title, doc = f"{title} ({n})", None
    if doc is None:
        doc = Document(kind=kind, doc_type=doc_type, title=title, scope=scope, source_url=source_url, provider=provider, subject=subject)
        session.add(doc)
        session.flush()

    current = session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.is_current.is_(True)).order_by(DocumentVersion.version_no.desc()))
    if current and current.checksum == digest:
        if structured and current.structured != structured:
            current.structured = structured
        return IngestResult(doc, current, False, 0)

    version_no = (current.version_no + 1) if current else 1
    text = text_override if text_override is not None else extract_text(data, content_type, name)
    path = _store_original(cfg, scope, f"{doc_type.value}_{doc.id}", version_no, data, _suffix_for(content_type, name))
    for v in session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id)):
        v.is_current = False
    version = DocumentVersion(
        document_id=doc.id,
        version_no=version_no,
        checksum=digest,
        content_type=content_type,
        file_path=path,
        parsed_text=text,
        structured=structured or {},
        metadata_json={"original_name": name, "bytes": len(data), "source_url": source_url},
        is_current=True,
    )
    session.add(version)
    session.flush()
    session.refresh(doc)
    if current:
        remove_version(session, current.id)
    chunks = index_version(session, document_id=doc.id, version_id=version.id, doc_type=doc_type.value, scope=scope, title=title, body=text)
    return IngestResult(doc, version, True, chunks)


def ingest_file(session: Session, path: str | Path, *, teacher_id: int, doc_type: DocumentType, title: str | None = None, teacher_course_id: int | None = None, subject: str | None = None, cfg: Settings | None = None) -> IngestResult:
    """Ingest a teacher-provided file (syllabus, pacing guide, lesson calendar)."""
    p = Path(path)
    result = ingest_bytes(
        session,
        data=p.read_bytes(),
        name=p.name,
        kind=DocumentKind.teacher,
        doc_type=doc_type,
        title=title or p.stem.replace("_", " ").title(),
        scope=f"teacher:{teacher_id}",
        subject=subject,
        cfg=cfg,
    )
    link = session.scalar(select(TeacherDocument).where(TeacherDocument.teacher_id == teacher_id, TeacherDocument.document_id == result.document.id))
    if link is None:
        session.add(TeacherDocument(teacher_id=teacher_id, document_id=result.document.id, teacher_course_id=teacher_course_id, role=doc_type.value))
    return result


# ------------------------------------------------------------- public sources
def _payload_bytes(payload: SourcePayload) -> tuple[bytes, str]:
    if payload.raw_bytes:
        return payload.raw_bytes, payload.content_type
    import json

    body = {"text": payload.text, "structured": payload.structured}
    return json.dumps(body, indent=2, sort_keys=True, default=str).encode("utf-8"), "application/json"


def register_public_sources(session: Session, provider: DistrictProvider, *, force: bool = False, cfg: Settings | None = None) -> list[IngestResult]:
    """Fetch every provider source that is due and version it. Returns results for sources touched."""
    cfg = cfg or default_settings
    results: list[IngestResult] = []
    scope = f"district:{provider.slug}"
    for desc in provider.sources():
        reg = session.scalar(select(SourceRegistration).where(SourceRegistration.provider == provider.slug, SourceRegistration.source_key == desc.key))
        if reg is None:
            reg = SourceRegistration(provider=provider.slug, source_key=desc.key, url=desc.url, refresh_interval_days=desc.refresh_interval_days)
            session.add(reg)
            session.flush()
        due = force or reg.last_checked_at is None or (utcnow() - reg.last_checked_at) > timedelta(days=reg.refresh_interval_days)
        if not due:
            continue
        payload = provider.fetch(desc.key, allow_network=cfg.allow_network)
        data, ctype = _payload_bytes(payload)
        result = ingest_bytes(
            session,
            data=data,
            name=f"{desc.key}",
            kind=DocumentKind.public,
            doc_type=DocumentType(desc.doc_type),
            title=desc.title,
            scope=scope,
            content_type=ctype,
            source_url=desc.url,
            provider=provider.slug,
            subject=desc.subject,
            structured=payload.structured,
            text_override=payload.text or None,
            cfg=cfg,
        )
        reg.document_id = result.document.id
        reg.last_checked_at = utcnow()
        reg.origin = payload.origin
        reg.status = "ok"
        if result.created_version:
            reg.last_changed_at = utcnow()
            reg.current_checksum = result.version.checksum
            reg.current_version_no = result.version.version_no
        results.append(result)
    return results


def sync_calendar_events(session: Session, provider: DistrictProvider, school_year: str) -> int:
    """Materialize the provider's academic calendar into school_calendar_events (idempotent)."""
    existing = {(e.date, e.event_type, e.title): e for e in session.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.district_slug == provider.slug, SchoolCalendarEvent.school_year == school_year))}
    added = 0
    for spec in provider.get_academic_calendar(school_year):
        key = (spec.date, CalendarEventType(spec.event_type), spec.title)
        if key in existing:
            continue
        session.add(
            SchoolCalendarEvent(
                district_slug=provider.slug,
                school_year=school_year,
                date=spec.date,
                end_date=spec.end_date,
                event_type=CalendarEventType(spec.event_type),
                title=spec.title,
                instructional=spec.instructional,
                quarter=spec.quarter,
                needs_confirmation=spec.needs_confirmation,
                notes=spec.notes,
            )
        )
        added += 1
    return added
