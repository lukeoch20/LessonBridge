"""Web interface: review-first screens for onboarding results, the calendar, proposals, plans and rules."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import markdown as md_lib
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from .. import db
from ..absence import leave as leave_mod
from ..absence.service import apply_proposal, create_absence, mark_progress, plan_absence, reconcile_slip, reject_proposal
from ..config import settings
from ..curriculum.calendar import entries_between
from ..generation.llm import LLMClient
from ..generation.subplan import generate_sub_plans
from ..ingestion.index import search as fts_search
from ..ingestion.pipeline import ingest_bytes
from ..models import AbsenceEvent, Document, DocumentKind, DocumentType, Proposal, ProposalStatus, SchoolCalendarEvent, SubPlan, Teacher, TeacherCourse, TeacherDocument, TeacherRule
from ..profile.onboarding import OnboardingInput, confirm_calendar, count_touches, record_touch, run_onboarding
from ..profile.rules import add_rule
from ..render import markdown as md
from ..schemas import DiffItem

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _html(text: str) -> str:
    return md_lib.markdown(text, extensions=["tables"])


def create_app() -> FastAPI:
    app = FastAPI(title="LessonBridge")
    db.init_engine(settings)

    def teacher_or_none(session, teacher_id: Optional[int] = None) -> Optional[Teacher]:
        if teacher_id:
            return session.get(Teacher, teacher_id)
        return session.scalar(select(Teacher).order_by(Teacher.id.desc()))

    def render(request: Request, name: str, **ctx):
        return TEMPLATES.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db.session_scope() as s:
            t = teacher_or_none(s)
            if t is None:
                return RedirectResponse("/onboard", status_code=303)
            pending = list(s.scalars(select(Proposal).where(Proposal.teacher_id == t.id, Proposal.status == ProposalStatus.pending).order_by(Proposal.id.desc())))
            flagged = len(list(s.scalars(select(SchoolCalendarEvent).where(SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False)))))
            today = date.today()
            upcoming = {sec.id: entries_between(s, sec.id, today, today + timedelta(days=7)) for sec in t.sections}
            return render(request, "dashboard.html", teacher=t, pending=pending, flagged=flagged, touches=count_touches(s, t.id), upcoming=upcoming, llm_on=LLMClient(settings).available(), model=settings.model)

    # ------------------------------------------------------------- onboard
    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_form(request: Request):
        example = (Path(__file__).resolve().parent.parent.parent / "examples" / "teacher_profile.yaml")
        return render(request, "onboard.html", example=example.read_text() if example.exists() else "", result=None)

    @app.post("/onboard", response_class=HTMLResponse)
    def onboard_submit(request: Request, profile_yaml: str = Form(...)):
        import yaml

        data = yaml.safe_load(profile_yaml)
        inp = OnboardingInput(**data)
        # Resolve relative document paths against the examples directory.
        base = Path(__file__).resolve().parent.parent.parent / "examples"
        for d in inp.documents:
            p = Path(d.path)
            if not p.is_absolute() and (base / p).exists():
                d.path = str(base / p)
        with db.session_scope() as s:
            res = run_onboarding(s, inp, cfg=settings, llm=LLMClient(settings))
            return render(request, "onboard.html", example=profile_yaml, result=res)

    # ------------------------------------------------------------ calendar
    @app.get("/calendar/{section_id}", response_class=HTMLResponse)
    def calendar_view(request: Request, section_id: int, start: Optional[str] = None, end: Optional[str] = None):
        with db.session_scope() as s:
            sec = s.get(TeacherCourse, section_id)
            if sec is None:
                raise HTTPException(404)
            a = date.fromisoformat(start) if start else date.today() - timedelta(days=7)
            b = date.fromisoformat(end) if end else a + timedelta(days=42)
            entries = entries_between(s, sec.id, a, b)
            plans = {p.calendar_entry_id: p.id for p in s.scalars(select(SubPlan).where(SubPlan.teacher_course_id == sec.id, SubPlan.date >= a, SubPlan.date <= b))}
            return render(request, "calendar.html", section=sec, entries=entries, start=a, end=b, plans=plans, prev=(a - timedelta(days=42)).isoformat(), next=(b + timedelta(days=1)).isoformat())

    @app.post("/calendar/{section_id}/mark")
    def calendar_mark(section_id: int, on: str = Form(...), status: str = Form(...), note: str = Form("")):
        with db.session_scope() as s:
            sec = s.get(TeacherCourse, section_id)
            mark_progress(s, sec, date.fromisoformat(on), status, note)
            record_touch(s, sec.teacher_id, "progress", "field", "mark progress")
        return RedirectResponse(f"/calendar/{section_id}?start={on}", status_code=303)

    @app.post("/calendar/{section_id}/slip")
    def calendar_slip(section_id: int, on: str = Form(...), days: int = Form(1), reason: str = Form("lesson took longer than planned")):
        with db.session_scope() as s:
            sec = s.get(TeacherCourse, section_id)
            p = reconcile_slip(s, sec, date.fromisoformat(on), days, reason)
            record_touch(s, sec.teacher_id, "progress", "field", "report slip")
            pid = p.id
        return RedirectResponse(f"/proposals/{pid}", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request):
        with db.session_scope() as s:
            rows = list(s.scalars(select(SchoolCalendarEvent).order_by(SchoolCalendarEvent.date)))
            return render(request, "events.html", events=rows)

    @app.post("/events/confirm")
    def events_confirm(school_year: str = Form("2026-2027")):
        with db.session_scope() as s:
            t = teacher_or_none(s)
            confirm_calendar(s, t.id, school_year)
        return RedirectResponse("/events", status_code=303)

    # ----------------------------------------------------------- proposals
    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    def proposal_view(request: Request, proposal_id: int):
        with db.session_scope() as s:
            p = s.get(Proposal, proposal_id)
            if p is None:
                raise HTTPException(404)
            sec = s.get(TeacherCourse, p.teacher_course_id)
            body = _html(md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} — {sec.section_name}"))
            return render(request, "proposal.html", proposal=p, section=sec, body=body)

    @app.post("/proposals/{proposal_id}/approve")
    def proposal_approve(proposal_id: int):
        with db.session_scope() as s:
            p = s.get(Proposal, proposal_id)
            apply_proposal(s, p)
            record_touch(s, p.teacher_id, "absence", "decision", "approve proposal")
            target = f"/absences/{p.absence_id}" if p.absence_id else f"/calendar/{p.teacher_course_id}"
        return RedirectResponse(target, status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    def proposal_reject(proposal_id: int):
        with db.session_scope() as s:
            p = s.get(Proposal, proposal_id)
            reject_proposal(s, p)
            record_touch(s, p.teacher_id, "absence", "decision", "reject proposal")
            target = f"/absences/{p.absence_id}" if p.absence_id else "/"
        return RedirectResponse(target, status_code=303)

    # ------------------------------------------------------------ absences
    @app.get("/absences/new", response_class=HTMLResponse)
    def absence_form(request: Request):
        return render(request, "absence_new.html")

    @app.post("/absences")
    def absence_create(start: str = Form(...), end: str = Form(""), substitute: str = Form(""), reason: str = Form("")):
        with db.session_scope() as s:
            t = teacher_or_none(s)
            a = date.fromisoformat(start)
            b = date.fromisoformat(end) if end else a
            ab = create_absence(s, t.id, a, b, substitute_type=substitute or None, reason=reason)
            record_touch(s, t.id, "absence", "field", "report absence")
            plan_absence(s, ab)
            aid = ab.id
        return RedirectResponse(f"/absences/{aid}", status_code=303)

    @app.get("/absences/{absence_id}", response_class=HTMLResponse)
    def absence_view(request: Request, absence_id: int):
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, absence_id)
            if ab is None:
                raise HTTPException(404)
            sections = {sec.id: sec for sec in ab.teacher.sections}
            return render(request, "absence.html", absence=ab, sections=sections, plans=sorted(ab.sub_plans, key=lambda p: (p.date, p.section.period)), leave=ab.leave_plan)

    @app.post("/absences/{absence_id}/subplans")
    def absence_generate(absence_id: int):
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, absence_id)
            generate_sub_plans(s, ab, llm=LLMClient(settings), cfg=settings)
        return RedirectResponse(f"/absences/{absence_id}", status_code=303)

    @app.post("/absences/{absence_id}/leave")
    def absence_leave(absence_id: int):
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, absence_id)
            leave_mod.pre_leave_analysis(s, ab)
            plans = generate_sub_plans(s, ab, days_limit=10, llm=LLMClient(settings), cfg=settings)
            leave_mod.build_handoff(s, ab, {p.calendar_entry_id: p.id for p in plans})
            leave_mod.weekly_frameworks(s, ab)
        return RedirectResponse(f"/absences/{absence_id}/leave", status_code=303)

    @app.get("/absences/{absence_id}/leave", response_class=HTMLResponse)
    def absence_leave_view(request: Request, absence_id: int):
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, absence_id)
            lp = ab.leave_plan
            if lp is None:
                return RedirectResponse(f"/absences/{absence_id}", status_code=303)
            from ..schemas import WeeklyFramework

            body = _html(md.render_pre_leave(lp.pre_leave)) if lp.pre_leave else ""
            body += _html(md.render_handoff(lp.handoff, lp.pre_leave)) if lp.handoff else ""
            body += _html(md.render_weekly([WeeklyFramework(**w) for w in lp.weekly_frameworks])) if lp.weekly_frameworks else ""
            return render(request, "document.html", title=f"Leave packet — absence {ab.id}", body=body, back=f"/absences/{ab.id}")

    @app.get("/absences/{absence_id}/brief", response_class=HTMLResponse)
    def absence_brief(request: Request, absence_id: int):
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, absence_id)
            body = _html(md.render_return_brief(leave_mod.return_brief(s, ab)))
            return render(request, "document.html", title=f"Return brief — absence {ab.id}", body=body, back=f"/absences/{ab.id}")

    @app.get("/subplans/{plan_id}", response_class=HTMLResponse)
    def subplan_view(request: Request, plan_id: int):
        with db.session_scope() as s:
            p = s.get(SubPlan, plan_id)
            if p is None:
                raise HTTPException(404)
            return render(request, "document.html", title=f"Substitute plan {p.date} P{p.section.period}", body=_html(p.rendered_markdown), back=f"/absences/{p.absence_id}", meta=f"generator: {p.generator}; attempts: {p.attempts}; status: {p.status.value}")

    # -------------------------------------------------------------- rules
    @app.get("/rules", response_class=HTMLResponse)
    def rules_view(request: Request):
        with db.session_scope() as s:
            t = teacher_or_none(s)
            rules = list(s.scalars(select(TeacherRule).where(TeacherRule.teacher_id == t.id).order_by(TeacherRule.id)))
            return render(request, "rules.html", rules=rules, teacher=t)

    @app.post("/rules")
    def rules_add(text: str = Form(...)):
        with db.session_scope() as s:
            t = teacher_or_none(s)
            add_rule(s, t.id, text)
            record_touch(s, t.id, "rules", "field", "add rule")
        return RedirectResponse("/rules", status_code=303)

    # ---------------------------------------------------------- documents
    @app.get("/documents", response_class=HTMLResponse)
    def documents_view(request: Request, q: str = ""):
        with db.session_scope() as s:
            docs = list(s.scalars(select(Document).order_by(Document.kind, Document.title)))
            hits = fts_search(s, q, limit=10) if q else []
            return render(request, "documents.html", documents=docs, q=q, hits=hits)

    @app.post("/documents/upload")
    async def documents_upload(file: UploadFile, doc_type: str = Form("other"), subject: str = Form("")):
        data = await file.read()
        with db.session_scope() as s:
            t = teacher_or_none(s)
            res = ingest_bytes(s, data=data, name=file.filename or "upload", kind=DocumentKind.teacher, doc_type=DocumentType(doc_type), title=(file.filename or "upload").rsplit(".", 1)[0], scope=f"teacher:{t.id}", content_type=file.content_type, subject=subject or None, cfg=settings)
            if not s.scalar(select(TeacherDocument).where(TeacherDocument.teacher_id == t.id, TeacherDocument.document_id == res.document.id)):
                s.add(TeacherDocument(teacher_id=t.id, document_id=res.document.id, role=doc_type))
            record_touch(s, t.id, "documents", "field", f"upload {doc_type}")
        return RedirectResponse("/documents", status_code=303)

    return app


app = create_app() if __name__ == "__main__" else None
