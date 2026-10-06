"""CLI and configuration (LB-19, LB-44, LB-45, LB-57, LB-59, LB-60)."""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from lessonbridge import db
from lessonbridge.cli import _section, app


@pytest.fixture()
def runner():
    return CliRunner()


def test_lb19_demo_never_deletes_the_output_folder(runner, tmp_path):
    target = tmp_path / "Documents"
    target.mkdir()
    (target / "grades.txt").write_text("keep me")
    r = runner.invoke(app, ["--data-dir", str(tmp_path / "d"), "demo", "--out", str(target)])
    assert r.exit_code == 1 and "not empty" in r.output
    assert (target / "grades.txt").read_text() == "keep me"


def test_lb44_section_matching_never_guesses(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        from lessonbridge.models import Teacher

        t = s.get(Teacher, tid)
        assert _section(s, t, "5").period == "5"  # a period, never a database id
        assert _section(s, t, "id:1").id == 1
        assert _section(s, t, "Civics & Economics - Period 3").period == "3"
        from lessonbridge.absence.service import PlanningError

        with pytest.raises(PlanningError):
            _section(s, t, "English")  # two English sections: ambiguous


def test_lb44_cli_refuses_to_guess_between_teachers(runner, onboarded):
    cfg, tid = onboarded
    from lessonbridge.models import Teacher

    with db.session_scope() as s:
        t = s.get(Teacher, tid)
        s.add(Teacher(name="Second Teacher", school_id=t.school_id))
    r = runner.invoke(app, ["--data-dir", str(cfg.data_dir), "status"])
    assert r.exit_code == 1 and "pass --teacher-id" in " ".join(r.output.split())
    r = runner.invoke(app, ["--data-dir", str(cfg.data_dir), "status", "--teacher-id", str(tid)])
    assert r.exit_code == 0


def test_lb57_rules_add_escapes_output_and_validates_scope(runner, onboarded):
    cfg, _ = onboarded
    r = runner.invoke(app, ["--data-dir", str(cfg.data_dir), "rules", "add", "Students line up at the door [bold]quietly[/bold]."])
    assert r.exit_code == 0 and "[procedure" in r.output
    r = runner.invoke(app, ["--data-dir", str(cfg.data_dir), "rules", "add", "x", "--scope", "galaxy"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["--data-dir", str(cfg.data_dir), "rules", "add", "Use the word wall.", "--scope", "unit", "--target", "nope"])
    assert r.exit_code == 1 and "No unit" in r.output


def test_cli_absence_flow_with_approve_all_and_undo(runner, onboarded):
    cfg, _ = onboarded
    base = ["--data-dir", str(cfg.data_dir)]
    r = runner.invoke(app, base + ["absence", "plan", "2026-10-27"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, base + ["absence", "approve", "--all", "1"])
    assert r.exit_code == 0 and "approved" in r.output
    r = runner.invoke(app, base + ["absence", "reject", "1"])
    assert r.exit_code == 1 and "undo" in r.output
    r = runner.invoke(app, base + ["absence", "plan", "2026-11-09", "--end", "2027-01-29"])
    assert r.exit_code == 1 and "--substitute" in r.output  # never assumes a long-term substitute (LB-24)


def test_lb45_same_named_documents_for_different_subjects_stay_separate(onboarded):
    from lessonbridge.ingestion.pipeline import ingest_bytes
    from lessonbridge.models import DocumentKind, DocumentType

    cfg, tid = onboarded
    with db.session_scope() as s:
        a = ingest_bytes(s, data=b"Unit 1: Short Fiction", name="syllabus.md", kind=DocumentKind.teacher, doc_type=DocumentType.syllabus, title="Syllabus", scope="teacher:9", subject="english", cfg=cfg)
        b = ingest_bytes(s, data=b"Unit 1: Founding Documents", name="syllabus.md", kind=DocumentKind.teacher, doc_type=DocumentType.syllabus, title="Syllabus", scope="teacher:9", subject="civics", cfg=cfg)
        assert a.document.id != b.document.id
        c = ingest_bytes(s, data=b"Unit 1: Poetry", name="syllabus.md", kind=DocumentKind.teacher, doc_type=DocumentType.syllabus, title="Syllabus", scope="teacher:9", subject="english",
                         as_new_version=False, cfg=cfg)
        assert c.document.id != a.document.id and c.document.title == "Syllabus (2)"


@pytest.mark.parametrize("raw,expected", [("0", False), ("False", False), ("OFF", False), (" no ", False), ("1", True), ("Yes", True), ("on", True)])
def test_lb59_network_flag_parsing(monkeypatch, raw, expected):
    from lessonbridge.config import Settings

    monkeypatch.setenv("LESSONBRIDGE_ALLOW_NETWORK", raw)
    assert Settings().allow_network is expected


def test_lb59_unknown_network_flag_is_an_error(monkeypatch):
    from lessonbridge.config import Settings

    monkeypatch.setenv("LESSONBRIDGE_ALLOW_NETWORK", "sometimes")
    with pytest.raises(ValueError):
        Settings()


def test_lb60_thin_live_pages_do_not_replace_snapshot_text(monkeypatch):
    from lessonbridge.providers import loudoun

    p = loudoun.LoudounCountyProvider()
    monkeypatch.setattr(p, "_try_live", lambda desc: {"text": "Loading...", "raw": b"x", "content_type": "text/html"})
    payload = p.fetch("academic_calendar", allow_network=True)
    assert "Thanksgiving" in payload.text and payload.origin == "snapshot"
    good = "Holiday schedule " + " ".join(["Thanksgiving break closed November"] * 40)
    monkeypatch.setattr(p, "_try_live", lambda desc: {"text": good, "raw": b"x", "content_type": "text/html"})
    payload = p.fetch("academic_calendar", allow_network=True)
    assert "Election Day" in payload.text and "[live page]" in payload.text and payload.origin == "live"
