"""Web interface: review-first screens for onboarding, the calendar, proposals, plans, rules and documents.

Built for a single teacher on their own computer, and hardened accordingly:
stored text is rendered without raw HTML and under a script-free Content
Security Policy (LB-17); onboarding accepts documents only as uploads or from
the examples folder (LB-18); state-changing requests must come from this site
and the Host header must be local, and binding to any other interface requires
a launch token (LB-42); bad input returns 4xx, not 500 (LB-56).
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import ValidationError
from sqlalchemy import func, select

from .. import db
from ..absence import leave as leave_mod
from ..absence.service import (
    BlockedProposal,
    DecisionRequired,
    PlanningError,
    ProposalNotPending,
    StaleProposalError,
    apply_all_for_absence,
    apply_proposal,
    cancel_absence,
    create_absence,
    default_substitute_type,
    mark_progress,
    plan_absence,
    propose_backlog_placement,
    propose_undo,
    reconcile_slip,
    reject_proposal,
)
from ..config import settings
from ..curriculum.calendar import entries_between, propose_rebuild
from ..curriculum.editing import edit_entry, fill_placeholder, interpret_upload, set_proposal_replacement
from ..generation.llm import LLMClient
from ..generation.subplan import generate_plans_for_absence
from ..ingestion.index import search as fts_search
from ..ingestion.pipeline import ingest_bytes
from ..models import (
    AbsenceEvent,
    Document,
    DocumentKind,
    DocumentType,
    Proposal,
    ProposalStatus,
    ReplacementActivity,
    RuleScope,
    SchoolCalendarEvent,
    SubPlan,
    Teacher,
    TeacherCourse,
    TeacherDocument,
    TeacherRule,
)
from ..profile.onboarding import DocumentInput, OnboardingError, OnboardingInput, confirm_calendar, count_touches, record_touch, run_onboarding
from ..profile.rules import RuleTargetError, add_rule, describe_rule, resolve_target
from ..render import markdown as md
from ..schemas import DiffItem

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
EXAMPLES_DIR = Path(__file__).resolve().parent.parent.parent / "examples"
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}
CSP = ("default-src 'self'; script-src 'none'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")


class NotFound(Exception):
    pass


class NoTeacher(Exception):
    pass


class PickTeacher(Exception):
    pass


def safe_html(text: str) -> Markup:
    """Markdown -> HTML with raw HTML escaped and unsafe links refused."""
    return Markup(_MD.render(text or ""))


def _int(value, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a date (YYYY-MM-DD).") from exc


def _get(session, model, ident):
    row = session.get(model, _int(ident, "id"))
    if row is None:
        raise NotFound(f"No {model.__name__} {ident}.")
    return row


def _host_of(value: str) -> str:
    host = value.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def create_app(*, token: Optional[str] = None, extra_hosts: list[str] | tuple = ()) -> FastAPI:
    app = FastAPI(title="LessonBridge", docs_url=None, redoc_url=None, openapi_url=None)
    db.init_engine(settings)
    allowed_hosts = LOCAL_HOSTS | {h.lower() for h in extra_hosts}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # 1. Host allow-list (blocks DNS rebinding).
        host = _host_of(request.headers.get("host", ""))
        if host not in allowed_hosts:
            return Response("Unknown host.", status_code=400)
        # 2. Launch token when served beyond this computer.
        if token:
            supplied = request.query_params.get("token")
            if supplied == token:
                resp = RedirectResponse(request.url.path, status_code=303)
                resp.set_cookie("lb_token", token, httponly=True, samesite="strict")
                return resp
            if request.cookies.get("lb_token") != token:
                return Response("Open LessonBridge with the link printed when the server started.", status_code=403)
        # 3. Cross-site request forgery: state changes only from this site.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            if site is not None and site not in ("same-origin", "none"):
                return Response("Cross-site request refused.", status_code=403)
            if origin is not None and _host_of(urlsplit(origin).netloc) != host:
                return Response("Cross-site request refused.", status_code=403)
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx):
        return TEMPLATES.TemplateResponse(request, name, ctx, status_code=status)

    def error_page(request: Request, status: int, title: str, message: str):
        return page(request, "document.html", status, title=title, body=Markup(f"<h1>{Markup.escape(title)}</h1><p>{Markup.escape(message)}</p>"), back=request.headers.get("referer") or "/")

    @app.exception_handler(NotFound)
    async def _nf(request: Request, exc: NotFound):
        return error_page(request, 404, "Not found", str(exc))

    @app.exception_handler(NoTeacher)
    async def _nt(request: Request, exc: NoTeacher):
        return RedirectResponse("/onboard", status_code=303)

    @app.exception_handler(PickTeacher)
    async def _pt(request: Request, exc: PickTeacher):
        return RedirectResponse("/teachers", status_code=303)

    @app.exception_handler(StaleProposalError)
    async def _stale(request: Request, exc: StaleProposalError):
        return error_page(request, 409, "This proposal is out of date", str(exc))

    @app.exception_handler(ProposalNotPending)
    async def _np(request: Request, exc: ProposalNotPending):
        return error_page(request, 409, "Not pending", str(exc))

    @app.exception_handler(DecisionRequired)
    async def _dr(request: Request, exc: DecisionRequired):
        return error_page(request, 409, "Your decision is needed", str(exc) + " Tick the confirmation box on the proposal page to approve it.")

    @app.exception_handler(BlockedProposal)
    async def _bp(request: Request, exc: BlockedProposal):
        return error_page(request, 409, "Blocked by the safety check", str(exc))

    for exc_type in (PlanningError, OnboardingError, RuleTargetError, ValueError, ValidationError):
        @app.exception_handler(exc_type)
        async def _bad(request: Request, exc: Exception):
            return error_page(request, 400, "That did not work", str(exc).splitlines()[0][:500])

    def current_teacher(request: Request, session) -> Teacher:
        cookie = request.cookies.get("lb_teacher")
        if cookie and cookie.isdigit():
            t = session.get(Teacher, int(cookie))
            if t is not None:
                return t
        teachers = list(session.scalars(select(Teacher).order_by(Teacher.id).limit(2)))
        if not teachers:
            raise NoTeacher()
        if len(teachers) > 1:
            raise PickTeacher()
        return teachers[0]

    def own_section(session, teacher: Teacher, section_id) -> TeacherCourse:
        sec = _get(session, TeacherCourse, section_id)
        if sec.teacher_id != teacher.id:
            raise NotFound("No such section for this teacher.")
        return sec

    def own(session, teacher: Teacher, model, ident):
        row = _get(session, model, ident)
        if getattr(row, "teacher_id", teacher.id) != teacher.id:
            raise NotFound(f"No {model.__name__} {ident} for this teacher.")
        return row

    def render_proposal(session, p: Proposal) -> Markup:
        sec = session.get(TeacherCourse, p.teacher_course_id)
        owed = {o.id: o.title for o in sec.owed}
        return safe_html(md.render_proposal(p.explanation, [DiffItem(**d) for d in p.diff], p.constraints, heading=f"Proposal {p.id} — {sec.section_name}", backlog=p.backlog, owed_titles=owed))

    # ------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            pending = list(s.scalars(select(Proposal).where(Proposal.teacher_id == t.id, Proposal.status.in_([ProposalStatus.pending, ProposalStatus.blocked])).order_by(Proposal.id.desc())))
            flagged = s.scalar(select(func.count(SchoolCalendarEvent.id)).where(SchoolCalendarEvent.needs_confirmation.is_(True), SchoolCalendarEvent.confirmed.is_(False)))
            today = date.today()
            upcoming = {sec.id: entries_between(s, sec.id, today, today + timedelta(days=7)) for sec in t.sections}
            owed = [(sec, o) for sec in t.sections for o in sec.owed if o.open]
            stale = [p for a in t.absences for p in a.sub_plans if p.status.value == "stale"]
            llm = LLMClient(settings)
            return page(request, "dashboard.html", teacher=t, pending=pending, flagged=flagged, touches=count_touches(s, t.id), upcoming=upcoming, owed=owed,
                        stale=stale, llm_on=llm.available(), llm_reason=llm.unavailable_reason, model=settings.model,
                        sections={sec.id: sec for sec in t.sections})

    @app.get("/teachers", response_class=HTMLResponse)
    def teachers(request: Request):
        with db.session_scope() as s:
            return page(request, "teachers.html", teachers=list(s.scalars(select(Teacher).order_by(Teacher.id))))

    @app.post("/teachers/select")
    def teachers_select(teacher_id: str = Form(...)):
        with db.session_scope() as s:
            t = _get(s, Teacher, teacher_id)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("lb_teacher", str(t.id), httponly=True, samesite="strict")
            return resp

    # ------------------------------------------------------------- onboard
    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_form(request: Request):
        example = EXAMPLES_DIR / "teacher_profile.yaml"
        return page(request, "onboard.html", example=example.read_text() if example.exists() else "", result=None)

    @app.post("/onboard", response_class=HTMLResponse)
    async def onboard_submit(request: Request):
        form = await request.form()
        profile_yaml = str(form.get("profile_yaml") or "")
        inp = OnboardingInput.from_yaml_text(profile_yaml)
        for i in range(1, 4):
            f = form.get(f"doc{i}")
            if f is None or not getattr(f, "filename", ""):
                continue
            data = await f.read()
            if not data:
                continue
            inp.documents.append(DocumentInput(name=f.filename, data=data, doc_type=str(form.get(f"doc{i}_type") or "syllabus"), subject=(str(form.get(f"doc{i}_subject") or "") or None)))
        with db.session_scope() as s:
            res = run_onboarding(s, inp, cfg=settings, llm=LLMClient(settings), document_root=EXAMPLES_DIR)
            resp = page(request, "onboard.html", example=profile_yaml, result=res)
            resp.set_cookie("lb_teacher", str(res.teacher_id), httponly=True, samesite="strict")
            return resp

    # ------------------------------------------------------------ calendar
    @app.get("/calendar/{section_id}", response_class=HTMLResponse)
    def calendar_view(request: Request, section_id: str, start: Optional[str] = None, end: Optional[str] = None):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            a = _date(start) if start else date.today() - timedelta(days=7)
            b = _date(end) if end else a + timedelta(days=42)
            entries = entries_between(s, sec.id, a, b)
            plans = {p.calendar_entry_id: p for p in s.scalars(select(SubPlan).where(SubPlan.teacher_course_id == sec.id, SubPlan.date >= a, SubPlan.date <= b))}
            return page(request, "calendar.html", section=sec, entries=entries, start=a, end=b, plans=plans, owed=[o for o in sec.owed if o.open],
                        prev=(a - timedelta(days=42)).isoformat(), next=(b + timedelta(days=1)).isoformat())

    @app.post("/calendar/{section_id}/mark")
    def calendar_mark(request: Request, section_id: str, on: str = Form(...), status: str = Form(...), note: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            mark_progress(s, sec, _date(on), status, note)
            record_touch(s, t.id, "progress", "field", "mark progress")
        return RedirectResponse(f"/calendar/{sec.id}?start={on}", status_code=303)

    @app.post("/calendar/{section_id}/slip")
    def calendar_slip(request: Request, section_id: str, on: str = Form(...), days: int = Form(1), reason: str = Form("lesson took longer than planned")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            p = reconcile_slip(s, sec, _date(on), days, reason[:200])
            record_touch(s, t.id, "progress", "field", "report slip")
            pid = p.id
        return RedirectResponse(f"/proposals/{pid}", status_code=303)

    @app.post("/calendar/{section_id}/edit")
    def calendar_edit(request: Request, section_id: str, on: str = Form(...), title: str = Form(""), notes: str = Form(""), lesson: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            edit_entry(s, sec, _date(on), title=title or None, notes=notes if notes else None, lesson_slug=lesson or None)
            record_touch(s, t.id, "calendar", "correction", "edit day")
        return RedirectResponse(f"/calendar/{sec.id}?start={on}", status_code=303)

    @app.post("/calendar/{section_id}/fill")
    def calendar_fill(request: Request, section_id: str, on: str = Form(...), title: str = Form(...), objective: str = Form(...), lesson_type: str = Form("guided_practice"),
                      materials: str = Form(""), outputs: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            fill_placeholder(s, sec, _date(on), title=title, objective=objective, lesson_type=lesson_type,
                             materials=[m.strip() for m in materials.split(",") if m.strip()], outputs=[o.strip() for o in outputs.split(",") if o.strip()])
            record_touch(s, t.id, "calendar", "field", "fill placeholder")
        return RedirectResponse(f"/calendar/{sec.id}?start={on}", status_code=303)

    @app.post("/calendar/{section_id}/rebuild")
    def calendar_rebuild(request: Request, section_id: str, start: str = Form(...)):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            p, _ = propose_rebuild(s, sec, start=_date(start))
            record_touch(s, t.id, "calendar", "click", "request rebuild")
            pid = p.id
        return RedirectResponse(f"/proposals/{pid}", status_code=303)

    @app.post("/calendar/{section_id}/owed")
    def schedule_owed(request: Request, section_id: str, include_dropped: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sec = own_section(s, t, section_id)
            p = propose_backlog_placement(s, sec, include_dropped=bool(include_dropped))
            record_touch(s, t.id, "calendar", "click", "schedule owed lessons")
            pid = p.id
        return RedirectResponse(f"/proposals/{pid}", status_code=303)

    @app.get("/events", response_class=HTMLResponse)
    def events(request: Request):
        with db.session_scope() as s:
            current_teacher(request, s)
            return page(request, "events.html", events=list(s.scalars(select(SchoolCalendarEvent).order_by(SchoolCalendarEvent.date))))

    @app.post("/events/confirm")
    def events_confirm(request: Request, school_year: str = Form("2026-2027")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            confirm_calendar(s, t.id, school_year)
        return RedirectResponse("/events", status_code=303)

    # ----------------------------------------------------------- proposals
    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    def proposal_view(request: Request, proposal_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            p = own(s, t, Proposal, proposal_id)
            sec = s.get(TeacherCourse, p.teacher_course_id)
            fillers = [DiffItem(**r) for r in p.diff if (r.get("after") or {}).get("kind") == "filler" and r.get("changed")]
            acts = list(s.scalars(select(ReplacementActivity).where(ReplacementActivity.subject == sec.course.subject).order_by(ReplacementActivity.title)))
            return page(request, "proposal.html", proposal=p, section=sec, body=render_proposal(s, p), fillers=fillers, activities=acts)

    @app.post("/proposals/{proposal_id}/approve")
    def proposal_approve(request: Request, proposal_id: str, acknowledge: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            p = own(s, t, Proposal, proposal_id)
            try:
                apply_proposal(s, p, acknowledge=bool(acknowledge))
            except StaleProposalError:
                s.commit()  # keep the "stale" status so the page shows it
                raise
            record_touch(s, t.id, "absence", "decision", "approve proposal")
            target = f"/absences/{p.absence_id}" if p.absence_id else f"/calendar/{p.teacher_course_id}"
        return RedirectResponse(target, status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    def proposal_reject(request: Request, proposal_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            p = own(s, t, Proposal, proposal_id)
            reject_proposal(s, p)
            record_touch(s, t.id, "absence", "decision", "reject proposal")
            target = f"/absences/{p.absence_id}" if p.absence_id else "/"
        return RedirectResponse(target, status_code=303)

    @app.post("/proposals/{proposal_id}/undo")
    def proposal_undo(request: Request, proposal_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            u = propose_undo(s, own(s, t, Proposal, proposal_id))
            record_touch(s, t.id, "absence", "click", "request undo")
            uid = u.id
        return RedirectResponse(f"/proposals/{uid}", status_code=303)

    @app.post("/proposals/{proposal_id}/activity")
    def proposal_activity(request: Request, proposal_id: str, on: str = Form(...), activity: str = Form(...)):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            set_proposal_replacement(s, own(s, t, Proposal, proposal_id), _date(on), activity)
            record_touch(s, t.id, "absence", "correction", "change activity")
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    # ------------------------------------------------------------ absences
    @app.get("/absences/new", response_class=HTMLResponse)
    def absence_form(request: Request):
        with db.session_scope() as s:
            current_teacher(request, s)
        return page(request, "absence_new.html", ask_long_term=None)

    @app.post("/absences")
    def absence_create(request: Request, start: str = Form(...), end: str = Form(""), substitute: str = Form(""), reason: str = Form("")):
        a = _date(start)
        b = _date(end) if end else a
        with db.session_scope() as s:
            t = current_teacher(request, s)
            if not substitute and default_substitute_type(s, t.id, a, b) == "long_term_sub":
                # Ask instead of assuming a long-term substitute (LB-24).
                return page(request, "absence_new.html", ask_long_term={"start": a.isoformat(), "end": b.isoformat(), "reason": reason})
            ab = create_absence(s, t.id, a, b, substitute_type=substitute or None, reason=reason[:200])
            record_touch(s, t.id, "absence", "field", "report absence")
            plan_absence(s, ab)
            aid = ab.id
        return RedirectResponse(f"/absences/{aid}", status_code=303)

    @app.get("/absences/{absence_id}", response_class=HTMLResponse)
    def absence_view(request: Request, absence_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            sections = {sec.id: sec for sec in ab.teacher.sections}
            return page(request, "absence.html", absence=ab, sections=sections, plans=sorted(ab.sub_plans, key=lambda p: (p.date, p.section.period)), leave=ab.leave_plan,
                        pending=[p for p in ab.proposals if p.status == ProposalStatus.pending],
                        needs_ack=any(p.constraints.get("decisions_required") for p in ab.proposals if p.status == ProposalStatus.pending),
                        needs_replan=[sections[p.teacher_course_id] for p in ab.proposals if p.status in (ProposalStatus.stale, ProposalStatus.superseded)
                                      and not any(q.status in (ProposalStatus.pending, ProposalStatus.approved) and q.teacher_course_id == p.teacher_course_id and q.kind == p.kind for q in ab.proposals)])

    @app.post("/absences/{absence_id}/approve")
    def absence_approve_all(request: Request, absence_id: str, acknowledge: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            try:
                apply_all_for_absence(s, ab, acknowledge=bool(acknowledge))
            except StaleProposalError:
                s.commit()
                raise
            record_touch(s, t.id, "absence", "decision", "approve all proposals")
        return RedirectResponse(f"/absences/{absence_id}", status_code=303)

    @app.post("/absences/{absence_id}/replan")
    def absence_replan(request: Request, absence_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            plan_absence(s, own(s, t, AbsenceEvent, absence_id))
        return RedirectResponse(f"/absences/{absence_id}", status_code=303)

    @app.post("/absences/{absence_id}/cancel")
    def absence_cancel(request: Request, absence_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            cancel_absence(s, own(s, t, AbsenceEvent, absence_id))
            record_touch(s, t.id, "absence", "decision", "cancel absence")
        return RedirectResponse(f"/absences/{absence_id}", status_code=303)

    @app.post("/absences/{absence_id}/subplans")
    def absence_generate(request: Request, absence_id: str, only_stale: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            record_touch(s, t.id, "absence", "click", "generate plans")
            aid = ab.id
        generate_plans_for_absence(aid, llm=LLMClient(settings), cfg=settings, only_stale=bool(only_stale))  # outside any transaction (LB-41)
        return RedirectResponse(f"/absences/{aid}", status_code=303)

    @app.post("/absences/{absence_id}/leave")
    def absence_leave(request: Request, absence_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            leave_mod.pre_leave_analysis(s, ab)
            leave_mod.draft_placeholders(s, ab, llm=LLMClient(settings))
            record_touch(s, t.id, "leave", "click", "build leave packet")
            aid = ab.id
        ids = generate_plans_for_absence(aid, days_limit=10, llm=LLMClient(settings), cfg=settings)
        with db.session_scope() as s:
            ab = s.get(AbsenceEvent, aid)
            plans = [s.get(SubPlan, i) for i in ids]
            leave_mod.build_handoff(s, ab, {p.calendar_entry_id: p.id for p in plans})
            leave_mod.weekly_frameworks(s, ab)
        return RedirectResponse(f"/absences/{aid}/leave", status_code=303)

    @app.get("/absences/{absence_id}/leave", response_class=HTMLResponse)
    def absence_leave_view(request: Request, absence_id: str):
        from ..schemas import WeeklyFramework

        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            lp = ab.leave_plan
            if lp is None:
                return RedirectResponse(f"/absences/{absence_id}", status_code=303)
            text = (md.render_pre_leave(lp.pre_leave) if lp.pre_leave else "") + "\n\n" + (md.render_handoff(lp.handoff, lp.pre_leave) if lp.handoff else "")
            text += "\n\n" + (md.render_weekly([WeeklyFramework(**w) for w in lp.weekly_frameworks]) if lp.weekly_frameworks else "")
            return page(request, "document.html", title=f"Leave packet — absence {ab.id}", body=safe_html(text), back=f"/absences/{ab.id}")

    @app.get("/absences/{absence_id}/brief", response_class=HTMLResponse)
    def absence_brief(request: Request, absence_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            ab = own(s, t, AbsenceEvent, absence_id)
            return page(request, "document.html", title=f"Return brief — absence {ab.id}", body=safe_html(md.render_return_brief(leave_mod.return_brief(s, ab))), back=f"/absences/{ab.id}")

    @app.get("/subplans/{plan_id}", response_class=HTMLResponse)
    def subplan_view(request: Request, plan_id: str):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            p = _get(s, SubPlan, plan_id)
            if p.absence.teacher_id != t.id:
                raise NotFound("No such plan for this teacher.")
            meta = f"generator: {p.generator}; attempts: {p.attempts}; status: {p.status.value}"
            if p.status.value == "stale":
                meta += " — the calendar day changed after this plan was generated; regenerate it from the absence page."
            return page(request, "document.html", title=f"Substitute plan {p.date} P{p.section.period}", body=safe_html(p.rendered_markdown), back=f"/absences/{p.absence_id}", meta=meta)

    # -------------------------------------------------------------- rules
    @app.get("/rules", response_class=HTMLResponse)
    def rules_view(request: Request):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            rules = list(s.scalars(select(TeacherRule).where(TeacherRule.teacher_id == t.id).order_by(TeacherRule.id)))
            return page(request, "rules.html", rules=[(r, describe_rule(r.structured or {})) for r in rules], teacher=t)

    @app.post("/rules")
    def rules_add(request: Request, text: str = Form(...), scope: str = Form("teacher"), target: str = Form("")):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            sc = RuleScope(scope) if scope in ("teacher", "course", "unit", "lesson") else RuleScope.teacher
            add_rule(s, t.id, text[:500], scope=sc, scope_id=resolve_target(s, t.id, sc, target or None))
            record_touch(s, t.id, "rules", "field", "add rule")
        return RedirectResponse("/rules", status_code=303)

    # ---------------------------------------------------------- documents
    @app.get("/documents", response_class=HTMLResponse)
    def documents_view(request: Request, q: str = ""):
        with db.session_scope() as s:
            t = current_teacher(request, s)
            docs = list(s.scalars(select(Document).where((Document.scope == f"teacher:{t.id}") | (Document.kind == DocumentKind.public)).order_by(Document.kind, Document.title)))
            hits = fts_search(s, q, limit=10, scopes=[f"teacher:{t.id}", "district:lcps"]) if q else []
            return page(request, "documents.html", documents=docs, q=q, hits=hits, message=request.query_params.get("msg", ""))

    @app.post("/documents/upload")
    async def documents_upload(request: Request, file: UploadFile, doc_type: str = Form("other"), subject: str = Form(""), new_document: str = Form("")):
        data = await file.read()
        if not data:
            raise ValueError("The uploaded file is empty.")
        dt = DocumentType(doc_type)
        with db.session_scope() as s:
            t = current_teacher(request, s)
            res = ingest_bytes(s, data=data, name=file.filename or "upload", kind=DocumentKind.teacher, doc_type=dt, title=(file.filename or "upload").rsplit(".", 1)[0],
                               scope=f"teacher:{t.id}", content_type=file.content_type, subject=subject or None, as_new_version=not new_document, cfg=settings)
            if not s.scalar(select(TeacherDocument).where(TeacherDocument.teacher_id == t.id, TeacherDocument.document_id == res.document.id)):
                s.add(TeacherDocument(teacher_id=t.id, document_id=res.document.id, role=doc_type))
            record_touch(s, t.id, "documents", "field", f"upload {doc_type}")
            out = interpret_upload(s, t, res.version.parsed_text, dt, subject=subject or None, llm=LLMClient(settings), label=file.filename or "upload")
            msg = f"Stored '{res.document.title}'. Rules read: {len(out['rules'])}." + (f" Curriculum proposals: {', '.join(map(str, out['proposals']))}." if out["proposals"] else "")
        from urllib.parse import quote

        return RedirectResponse(f"/documents?msg={quote(msg)}", status_code=303)

    return app
