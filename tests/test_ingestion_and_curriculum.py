from datetime import date

from lessonbridge import db
from lessonbridge.curriculum.calendar import allocate_unit_slots, load_year_structure, school_days
from lessonbridge.curriculum.interpret import heuristic_extract, merge_curriculum
from lessonbridge.ingestion.index import search
from lessonbridge.ingestion.pipeline import ingest_bytes, register_public_sources, sync_calendar_events
from lessonbridge.models import DocumentKind, DocumentType
from lessonbridge.providers import get_provider

SYLLABUS = (__import__("pathlib").Path(__file__).resolve().parent.parent / "examples" / "teacher_docs" / "english7_syllabus.md").read_text()


def test_public_sources_are_versioned_and_not_refetched(cfg):
    p = get_provider("lcps")
    with db.session_scope() as s:
        first = register_public_sources(s, p, cfg=cfg)
        assert len(first) == len(p.sources()) and all(r.created_version for r in first)
        assert sync_calendar_events(s, p, "2026-2027") > 20
        assert sync_calendar_events(s, p, "2026-2027") == 0
        again = register_public_sources(s, p, cfg=cfg)
        assert again == []  # not due
        forced = register_public_sources(s, p, force=True, cfg=cfg)
        assert all(not r.created_version for r in forced)  # same checksum -> no new version
        hits = search(s, "federalism quarter", limit=5)
        assert hits and any("Civics" in h.title or "calendar" in h.title.lower() for h in hits)


def test_teacher_upload_creates_new_version_on_change(cfg):
    with db.session_scope() as s:
        a = ingest_bytes(s, data=b"Unit 1: Poetry\nOn quiz days, students receive 10 minutes to study before the quiz.", name="syl.txt", kind=DocumentKind.teacher, doc_type=DocumentType.syllabus, title="Syllabus", scope="teacher:1", cfg=cfg)
        b = ingest_bytes(s, data=b"Unit 1: Poetry\nUnit 2: Drama", name="syl.txt", kind=DocumentKind.teacher, doc_type=DocumentType.syllabus, title="Syllabus", scope="teacher:1", cfg=cfg)
        assert a.document.id == b.document.id and b.version.version_no == 2 and b.created_version
        assert a.document.current_version.id == b.version.id
        assert search(s, "drama", scopes=["teacher:1"])


def test_heuristic_extraction_and_merge():
    ctx = heuristic_extract(SYLLABUS, "english")
    titles = [u.title for u in ctx.units]
    assert "Narrative Writing" in titles and "Argumentative Writing — claims, evidence, counterclaims" in titles
    assert ctx.units[0].quarter == 1 and ctx.units[0].planned_days == 6
    assert any("15 minutes to study" in r.text for r in ctx.rules)
    assert not any(r.text.startswith("Students need") for r in ctx.rules)
    merged = merge_curriculum("english", ctx)
    slugs = [u.slug for u in merged.spec.units]
    # Teacher order wins (argumentative before nonfiction), default lessons attached.
    assert slugs.index("argumentative-writing") < slugs.index("nonfiction")
    assert all(u.lessons for u in merged.spec.units)
    assert merged.spec.units[2].slug == "narrative-writing"


def test_school_days_and_quarter_allocation(cfg):
    p = get_provider("lcps")
    with db.session_scope() as s:
        register_public_sources(s, p, cfg=cfg)
        sync_calendar_events(s, p, "2026-2027")
        ys = load_year_structure(s, "lcps", "2026-2027")
        days = school_days(ys, ys.first_day, ys.last_day)
        assert 175 <= len(days) <= 185
        assert date(2026, 9, 7) not in {d.date for d in days}  # Labor Day
        assert ys.quarter_for(date(2026, 10, 26)) == 1 and ys.quarter_for(date(2026, 11, 5)) == 2
        assert ys.quarter_end_on_or_after(date(2026, 10, 26)) == date(2026, 10, 30)

    class U:  # lightweight stand-in for CurriculumUnit
        def __init__(self, id, q, planned, n):
            self.id, self.quarter, self.planned_days, self.lessons = id, q, planned, [None] * n

    units = [U(1, 1, 10, 6), U(2, 1, 20, 10), U(3, 2, 20, 10)]
    with db.session_scope() as s:
        ys = load_year_structure(s, "lcps", "2026-2027")
        days = school_days(ys, ys.first_day, ys.last_day)
    slots = allocate_unit_slots(units, days)
    q1 = sum(1 for d in days if d.quarter == 1)
    assert slots[1] + slots[2] == q1  # quarter 1 fully used, slack spread proportionally
    assert slots[1] >= 10 and slots[2] >= 20 and slots[2] > slots[1]
