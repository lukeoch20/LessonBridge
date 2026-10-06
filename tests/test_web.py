"""Web interface: escaping, cross-site protection, host checks, onboarding path restriction, 4xx errors (audit test recommendation 5)."""
from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from lessonbridge import db
from lessonbridge.config import settings
from lessonbridge.models import Proposal, ProposalStatus, SubPlan, Teacher

PAYLOAD = '<img src=x onerror="alert(1)">'


@pytest.fixture()
def client(onboarded, monkeypatch):
    cfg, _tid = onboarded
    monkeypatch.setattr(settings, "data_dir", cfg.data_dir)
    monkeypatch.setattr(settings, "generator", "template")
    monkeypatch.setattr(settings, "allow_network", False)
    from lessonbridge.web.app import create_app

    return TestClient(create_app())


def test_pages_render_and_send_security_headers(client):
    for path in ["/", "/calendar/1", "/events", "/rules", "/documents?q=federalism", "/absences/new", "/onboard"]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert "script-src 'none'" in r.headers["content-security-policy"]
        assert "<script" not in r.text


def test_stored_markup_is_never_rendered_as_html(client):
    assert client.post("/rules", data={"text": f"Take attendance {PAYLOAD} quickly."}).status_code in (200, 303)
    r = client.post("/absences", data={"start": "2026-10-26", "end": "2026-10-26", "substitute": "any_sub", "reason": PAYLOAD}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(r.headers["location"])
    assert PAYLOAD not in page.text
    client.post(r.headers["location"] + "/approve")
    client.post(r.headers["location"] + "/subplans")
    with db.session_scope() as s:
        plan_id = s.query(SubPlan.id).first()[0]
    plan = client.get(f"/subplans/{plan_id}")
    assert plan.status_code == 200
    assert "<img" not in plan.text and "&lt;img" in plan.text
    # A slip reason and a javascript: link in stored text stay inert on the proposal page.
    r = client.post("/calendar/1/slip", data={"on": _first_lesson_day(), "days": 1, "reason": f"[x](javascript:alert(1)) {PAYLOAD}"}, follow_redirects=False)
    prop = client.get(r.headers["location"])
    assert "<img" not in prop.text and 'href="javascript' not in prop.text


def _first_lesson_day() -> str:
    from lessonbridge.models import CalendarEntry

    with db.session_scope() as s:
        e = s.query(CalendarEntry).filter(CalendarEntry.teacher_course_id == 1, CalendarEntry.lesson_id.isnot(None), CalendarEntry.date >= date(2026, 11, 2)).order_by(CalendarEntry.date).first()
        return e.date.isoformat()


def test_cross_site_posts_and_foreign_hosts_are_refused(client):
    r = client.post("/rules", data={"text": "Evil rule."}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.post("/rules", data={"text": "Evil rule."}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    ok = client.post("/rules", data={"text": "No phones during class."}, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert ok.status_code == 303
    with db.session_scope() as s:
        texts = [r.text for r in s.get(Teacher, 1).rules]
    assert "Evil rule." not in texts and "No phones during class." in texts


def test_launch_token_required_when_not_local(onboarded, monkeypatch):
    cfg, _ = onboarded
    monkeypatch.setattr(settings, "data_dir", cfg.data_dir)
    from lessonbridge.web.app import create_app

    c = TestClient(create_app(token="sekrit", extra_hosts=["192.168.1.20"]), base_url="http://192.168.1.20")
    assert c.get("/").status_code == 403
    r = c.get("/?token=sekrit", follow_redirects=False)
    assert r.status_code == 303 and "lb_token" in r.headers.get("set-cookie", "")
    assert c.get("/").status_code == 200


def test_onboarding_form_cannot_read_server_files(client, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("Take attendance. TOP SECRET VALUE.")
    profile = f"""teacher_name: Mallory
school: harper-park-ms
subjects: [english]
documents:
  - {{path: {secret}, doc_type: classroom_procedures}}
  - {{path: ../../../../etc/hostname, doc_type: syllabus}}
  - {{path: /proc/self/environ, doc_type: syllabus}}
"""
    r = client.post("/onboard", data={"profile_yaml": profile})
    assert r.status_code == 200
    import re

    outside_form = re.sub(r"(?s)<textarea.*?</textarea>", "", r.text)  # the submitted profile is echoed back to its author
    assert "TOP SECRET" not in r.text
    assert str(secret) not in outside_form and "/proc" not in outside_form and "hostname" not in outside_form
    assert "refused" in outside_form
    hits = client.get("/documents?q=SECRET").text
    assert "TOP SECRET" not in hits


def test_onboarding_accepts_uploads_and_updates_the_same_teacher(client):
    profile = open("examples/teacher_profile.yaml").read().split("documents:")[0]
    files = {"doc1": ("syllabus.md", b"Quarter 1\nUnit 1: Short Fiction (18 days)\nOn quiz days, students receive 10 minutes to study before the quiz.\n", "text/markdown")}
    r = client.post("/onboard", data={"profile_yaml": profile, "doc1_type": "syllabus", "doc1_subject": "english"}, files=files)
    assert r.status_code == 200 and "Updated your existing profile" in r.text
    with db.session_scope() as s:
        assert s.query(Teacher).count() == 1


def test_bad_input_returns_4xx_not_500(client):
    assert client.get("/calendar/999").status_code == 404
    assert client.get("/calendar/abc").status_code in (400, 404)
    assert client.get("/proposals/424242").status_code == 404
    assert client.post("/absences", data={"start": "not-a-date"}).status_code == 400
    assert client.post("/absences", data={"start": "2026-10-30", "end": "2026-10-26"}).status_code == 400
    assert client.post("/calendar/1/mark", data={"on": "2026-10-26", "status": "bogus"}).status_code == 400
    assert client.post("/onboard", data={"profile_yaml": "teacher_name: [unclosed"}).status_code == 400
    assert client.post("/rules", data={"text": "x", "scope": "lesson", "target": "no-such-lesson"}).status_code == 400
    r = client.post("/absences", data={"start": "2026-10-26", "end": "2026-10-26", "substitute": "any_sub"}, follow_redirects=False)
    client.post(r.headers["location"] + "/approve")
    with db.session_scope() as s:
        pid = s.query(Proposal.id).filter(Proposal.status == ProposalStatus.approved).first()[0]
    assert client.post(f"/proposals/{pid}/approve").status_code == 409
    assert client.post(f"/proposals/{pid}/reject").status_code == 409


def test_long_absence_asks_about_the_substitute(client):
    r = client.post("/absences", data={"start": "2026-11-09", "end": "2027-01-29"})
    assert r.status_code == 200 and "longer than ten school days" in r.text


def test_fresh_install_redirects_to_onboarding(tmp_path, monkeypatch):
    from lessonbridge.config import Settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "empty")
    monkeypatch.setattr(settings, "allow_network", False)
    from lessonbridge.web.app import create_app

    c = TestClient(create_app())
    for path in ["/", "/rules", "/documents"]:
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/onboard"
