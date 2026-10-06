"""Loudoun County Public Schools provider.

Live retrieval is attempted when the network allows it; otherwise (and as the
structured baseline) the bundled snapshots are used. Snapshot data is marked
``needs_confirmation`` so onboarding asks the teacher to confirm rather than
to type the dates.
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

from ..schemas import CalendarEventSpec, GradingPeriodSpec, SourcePayload, StandardSpec
from . import defaults
from .base import DistrictProvider, SourceDescriptor, register

LIVE_URLS = {
    "academic_calendar": "https://www.lcps.org/calendar",
    "standards_english": "https://www.doe.virginia.gov/teaching-learning-assessment/k-12-standards-instruction/english-reading-literacy",
    "standards_civics": "https://www.doe.virginia.gov/teaching-learning-assessment/k-12-standards-instruction/history-and-social-science",
    "curriculum_english": "https://www.lcps.org/academics/english-language-arts",
    "curriculum_civics": "https://www.lcps.org/academics/social-science",
    "assessment_calendar": "https://www.lcps.org/assessment",
}


def _useful_text(text: str) -> bool:
    """A live page is worth indexing only if it extracted real text (not a JS shell or an error marker)."""
    t = (text or "").strip()
    if t.startswith("[extraction failed") or len(t) < 300:
        return False
    words = t.split()
    return len(words) >= 50 and sum(w.isalpha() for w in words) / len(words) > 0.5


def _load(name: str) -> dict[str, Any]:
    with resources.files("lessonbridge.providers.snapshots").joinpath(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


@register
class LoudounCountyProvider(DistrictProvider):
    slug = "lcps"
    name = "Loudoun County Public Schools"
    state = "VA"

    def __init__(self) -> None:
        self._calendar = _load("lcps_calendar_2026_2027.json")
        self._standards = _load("va_standards.json")
        self._schools = _load("lcps_schools.json")

    # ---------------------------------------------------------------- sources
    def sources(self) -> list[SourceDescriptor]:
        return [
            SourceDescriptor("academic_calendar", "academic_calendar", "LCPS academic calendar 2026-2027", LIVE_URLS["academic_calendar"], 14),
            SourceDescriptor("grading_periods", "grading_periods", "LCPS grading periods 2026-2027", LIVE_URLS["academic_calendar"], 30),
            SourceDescriptor("standards_english", "standards", "Virginia English SOL, grade 7", LIVE_URLS["standards_english"], 180, "english"),
            SourceDescriptor("standards_civics", "standards", "Virginia Civics & Economics SOL", LIVE_URLS["standards_civics"], 180, "civics"),
            SourceDescriptor("curriculum_english", "curriculum_framework", "English 7 curriculum sequence", LIVE_URLS["curriculum_english"], 90, "english"),
            SourceDescriptor("curriculum_civics", "curriculum_framework", "Civics & Economics curriculum sequence", LIVE_URLS["curriculum_civics"], 90, "civics"),
            SourceDescriptor("assessment_calendar", "assessment_calendar", "Spring SOL testing window", LIVE_URLS["assessment_calendar"], 60),
        ]

    def fetch(self, key: str, *, allow_network: bool = True) -> SourcePayload:
        desc = {s.key: s for s in self.sources()}[key]
        payload = self._snapshot_payload(desc)
        if allow_network and desc.url:
            live = self._try_live(desc)
            if live is not None and _useful_text(live["text"]):
                # Keep the structured snapshot (it is what the planner consumes) and index the
                # snapshot text together with the live text, so a thin or broken live page never
                # hides the snapshot from search (LB-60).
                payload.text = payload.text + "\n\n[live page]\n" + live["text"]
                payload.origin = "live"
                payload.notes = "Live document retrieved; structured dates still come from the snapshot until confirmed."
        return payload

    def _snapshot_payload(self, desc: SourceDescriptor) -> SourcePayload:
        if desc.key == "academic_calendar":
            events = self._calendar["events"]
            text = "\n".join(f"{e['date']}{' to ' + e['end_date'] if e.get('end_date') else ''}: {e['title']} ({e['event_type']})" for e in events)
            return SourcePayload(source_key=desc.key, doc_type=desc.doc_type, title=desc.title, url=desc.url, text=text,
                                 structured={"events": events, "status": self._calendar["status"], "notes": self._calendar["notes"]},
                                 refresh_interval_days=desc.refresh_interval_days)
        if desc.key == "grading_periods":
            gp = self._calendar["grading_periods"]
            text = "\n".join(f"Quarter {g['quarter']}: {g['start']} to {g['end']}; grades due {g.get('grades_due')}" for g in gp)
            return SourcePayload(source_key=desc.key, doc_type=desc.doc_type, title=desc.title, url=desc.url, text=text,
                                 structured={"grading_periods": gp, "status": self._calendar["status"]}, refresh_interval_days=desc.refresh_interval_days)
        if desc.key.startswith("standards_"):
            block = self._standards[desc.subject]
            text = "\n".join(f"{s['code']} {s['title']} ({s['strand']}): {s['description']}" for s in block["standards"])
            return SourcePayload(source_key=desc.key, doc_type=desc.doc_type, title=desc.title, url=desc.url, text=text,
                                 structured={"standards": block["standards"], "status": self._standards["status"], "notes": self._standards["notes"]},
                                 refresh_interval_days=desc.refresh_interval_days, subject=desc.subject)
        if desc.key.startswith("curriculum_"):
            spec = defaults.DEFAULT_CURRICULA[desc.subject]()
            text = "\n".join(
                f"Unit {i+1} (Q{u.quarter}): {u.title} — {u.summary} Standards: {', '.join(u.standards)}. Lessons: "
                + "; ".join(l.title for l in u.lessons)
                for i, u in enumerate(spec.units)
            )
            return SourcePayload(source_key=desc.key, doc_type=desc.doc_type, title=desc.title, url=desc.url, text=text,
                                 structured={"curriculum": spec.model_dump(mode="json"), "status": "inference", "notes": spec.source_notes[0]},
                                 refresh_interval_days=desc.refresh_interval_days, subject=desc.subject)
        if desc.key == "assessment_calendar":
            windows = [e for e in self._calendar["events"] if e["event_type"] == "testing_window"]
            text = "\n".join(f"{e['date']} to {e.get('end_date')}: {e['title']}" for e in windows)
            return SourcePayload(source_key=desc.key, doc_type=desc.doc_type, title=desc.title, url=desc.url, text=text,
                                 structured={"windows": windows, "status": "provisional"}, refresh_interval_days=desc.refresh_interval_days)
        raise KeyError(desc.key)

    def _try_live(self, desc: SourceDescriptor) -> dict[str, Any] | None:
        try:
            import httpx

            resp = httpx.get(desc.url, timeout=10.0, follow_redirects=True, headers={"User-Agent": "LessonBridge/0.1"})
            if resp.status_code != 200 or not resp.content:
                return None
            from ..ingestion.extract import extract_text

            ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip()
            text = extract_text(resp.content, ctype, desc.url)
            return {"text": text, "raw": resp.content, "content_type": ctype}
        except Exception:
            return None

    # ------------------------------------------------------------- accessors
    def get_academic_calendar(self, school_year: str) -> list[CalendarEventSpec]:
        if school_year != self._calendar["school_year"]:
            return []
        provisional = self._calendar["status"] != "confirmed"
        return [CalendarEventSpec(**{**e, "needs_confirmation": provisional}) for e in self._calendar["events"]]

    def get_grading_periods(self, school_year: str) -> list[GradingPeriodSpec]:
        if school_year != self._calendar["school_year"]:
            return []
        provisional = self._calendar["status"] != "confirmed"
        return [GradingPeriodSpec(**{**g, "needs_confirmation": provisional}) for g in self._calendar["grading_periods"]]

    def get_standards(self, subject: str, grade: int) -> list[StandardSpec]:
        block = self._standards.get(subject)
        if not block or block.get("grade") != grade:
            return []
        return [StandardSpec(**s) for s in block["standards"]]

    def get_curriculum(self, subject: str, grade: int) -> dict:
        return defaults.DEFAULT_CURRICULA[subject]().model_dump(mode="json")

    def get_pacing_guide(self, subject: str, grade: int) -> dict | None:
        return None  # LCPS does not publish a teacher-level pacing guide publicly.

    def get_schools(self) -> list[dict]:
        return list(self._schools["schools"])
