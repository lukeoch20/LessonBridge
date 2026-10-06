"""Regression tests for the findings in the independent LessonBridge audit (LB-xx ids in each test)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from lessonbridge import db
from lessonbridge.absence import leave as leave_mod
from lessonbridge.absence.engine import Item, Slot, repair
from lessonbridge.absence.service import (
    PlanningError,
    ProposalNotPending,
    StaleProposalError,
    apply_all_for_absence,
    apply_proposal,
    cancel_absence,
    classify_absence,
    create_absence,
    mark_progress,
    plan_absence,
    propose_undo,
    reconcile_slip,
    reject_proposal,
)
from lessonbridge.absence.state import entry_state, state_keys
from lessonbridge.curriculum.calendar import entries_between, propose_rebuild
from lessonbridge.models import AbsenceEvent, CalendarEntry, EntryStatus, OwedLesson, Proposal, ProposalStatus, SubPlan, Teacher
from lessonbridge.render import markdown as md
from lessonbridge.schemas import DiffItem

from invariants import check_section, snapshot


def teacher(s, tid) -> Teacher:
    return s.get(Teacher, tid)


def approve_and_check(s, t, props, **kw):
    before = {sec.id: snapshot(sec) for sec in t.sections}
    for p in props:
        assert p.status == ProposalStatus.pending, p.constraints.get("violations")
        apply_proposal(s, p, acknowledge=True, **kw)
    problems = []
    for sec in t.sections:
        problems += check_section(s, sec, before[sec.id])
    assert not problems, problems[:5]


def test_lb01_lb05_nothing_is_deleted_at_or_across_a_quarter_end(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        for a, b in [(date(2026, 10, 28), date(2026, 10, 30)), (date(2026, 10, 28), date(2026, 11, 5))]:
            before_keys = {sec.id: {k for st in snapshot(sec).values() for k in state_keys(st)} for sec in t.sections}
            ab = create_absence(s, t.id, a, b, substitute_type="any_sub")
            props = plan_absence(s, ab)
            hard = " ".join(h for p in props for h in p.constraints["hard"])
            assert "Q1 end" in hard  # the quarter end stays a hard constraint even when the absence crosses it
            approve_and_check(s, t, props)
            for sec in t.sections:
                after = {k for st in snapshot(sec).values() for k in state_keys(st)} | {o.key for o in sec.owed if o.open}
                assert before_keys[sec.id] <= after


def test_lb02_a_second_absence_keeps_the_first_absences_substitute_days(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        first = create_absence(s, t.id, date(2026, 10, 29), date(2026, 10, 29), substitute_type="any_sub")
        approve_and_check(s, t, plan_absence(s, first))
        thursday = {sec.id: entry_state(next(e for e in sec.calendar if e.date == date(2026, 10, 29))) for sec in t.sections}
        assert all(st["is_sub_day"] for st in thursday.values())
        second = create_absence(s, t.id, date(2026, 10, 26), date(2026, 10, 26), substitute_type="any_sub")
        approve_and_check(s, t, plan_absence(s, second))
        for sec in t.sections:
            assert entry_state(next(e for e in sec.calendar if e.date == date(2026, 10, 29))) == thursday[sec.id]


def test_lb03_an_outdated_proposal_is_refused_and_leaves_the_calendar_alone(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        sec = t.sections[1]
        ab_start, ab_end = date(2026, 10, 26), date(2026, 10, 28)
        lesson_day = next(e.date for e in entries_between(s, sec.id, ab_start, ab_end) if e.lesson)  # its continuation lands inside the window
        ab = create_absence(s, t.id, ab_start, ab_end, substitute_type="any_sub")
        stale = [p for p in plan_absence(s, ab) if p.teacher_course_id == sec.id][0]
        slip = reconcile_slip(s, sec, lesson_day, 1)
        approve_and_check(s, t, [slip])
        assert stale.status == ProposalStatus.superseded  # approval supersedes overlapping pending proposals
        before = snapshot(sec)
        stale.status = ProposalStatus.pending  # even if forced back to pending, the before-state check refuses it
        with pytest.raises(StaleProposalError):
            apply_proposal(s, stale, acknowledge=True)
        assert snapshot(sec) == before and stale.status == ProposalStatus.stale
        fresh = [p for p in plan_absence(s, ab) if p.teacher_course_id == sec.id]
        approve_and_check(s, t, fresh)


def test_lb04_the_diff_lists_every_row_approval_will_write(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        leave = create_absence(s, t.id, date(2026, 11, 9), date(2027, 1, 29), substitute_type="long_term_sub")
        props = plan_absence(s, leave)
        p = props[0]
        rows = [DiffItem(**r) for r in p.diff]
        text = md.render_diff(rows)
        changed = [r for r in rows if r.changed]
        assert changed and all(f"{r.date:%a %b %d}" in text for r in changed)
        unchanged = len(rows) - len(changed)
        assert (f"*{unchanged} other day(s)" in text) if unchanged else ("other day(s)" not in text)
        assert "No lesson is removed from the calendar" in p.explanation


def test_lb06_lb07_multi_day_absences_keep_prerequisite_order_and_cover_every_day(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        start = date(2026, 9, 8)
        for i in range(0, 30, 6):
            a = start + timedelta(days=i)
            if a.isoweekday() > 5:
                continue
            ab = create_absence(s, t.id, a, a + timedelta(days=4), substitute_type="any_sub")
            approve_and_check(s, t, plan_absence(s, ab))


def test_lb08_lb09_slips_keep_substitute_days_and_work_after_progress_marks(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        leave = create_absence(s, t.id, date(2026, 11, 9), date(2027, 1, 29), substitute_type="long_term_sub")
        approve_and_check(s, t, plan_absence(s, leave))
        civ = t.sections[2]
        flagged_before = {e.date for e in civ.calendar if e.is_sub_day}
        day = next(e for e in entries_between(s, civ.id, date(2026, 12, 1), date(2026, 12, 10)) if e.lesson and not e.lesson.is_assessment)
        mark_progress(s, civ, day.date, "completed")  # LB-09: used to crash
        later = next(e for e in entries_between(s, civ.id, date(2026, 12, 14), date(2026, 12, 18)) if e.status == EntryStatus.planned)
        mark_progress(s, civ, later.date, "skipped")
        approve_and_check(s, t, [reconcile_slip(s, civ, day.date, 1)])
        assert flagged_before <= {e.date for e in civ.calendar if e.is_sub_day}  # LB-08
        assert later.status == EntryStatus.skipped and entry_state(later)["title"] == later.title


def test_lb10_rebuild_respects_progress_and_keeps_the_end_of_the_year(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        sec = t.sections[0]
        for e in entries_between(s, sec.id, date(2026, 8, 20), date(2026, 10, 2)):
            mark_progress(s, sec, e.date, "completed")
        all_lessons = {l.slug for u in sec.active_units for l in u.lessons}
        prop, report = propose_rebuild(s, sec, start=date(2026, 10, 5))
        approve_and_check(s, t, [prop])
        taught = [st["lesson_slug"] for st in snapshot(sec).values() if st["lesson_slug"]]
        assert len(taught) == len(set(taught))  # nothing re-taught
        assert all_lessons <= set(taught) | {o.key for o in sec.owed if o.open}  # nothing lost


def test_lb20_skipped_and_completed_days_are_never_rewritten(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        sec = t.sections[0]
        mark_progress(s, sec, date(2026, 10, 28), "skipped")
        before = entry_state(next(e for e in sec.calendar if e.date == date(2026, 10, 28)))
        ab = create_absence(s, t.id, date(2026, 10, 26), date(2026, 10, 26), substitute_type="any_sub")
        approve_and_check(s, t, plan_absence(s, ab))
        assert entry_state(next(e for e in sec.calendar if e.date == date(2026, 10, 28))) == before


def test_lb21_merged_lessons_survive_later_planning(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        sec = t.sections[0]
        days = [e for e in entries_between(s, sec.id, date(2026, 10, 12), date(2026, 10, 30)) if e.lesson and not e.lesson.is_assessment]
        a, b = days[0], days[1]
        folded = a.lesson.slug
        b.merged_keys, b.merged_lesson_ids, b.title = [folded], [a.lesson_id], f"{a.title} + shortened {b.title}"
        a.lesson_id, a.title = None, "Flex / work day"
        from lessonbridge.models import EntryKind

        a.kind = EntryKind.flex
        s.flush()
        ab = create_absence(s, t.id, a.date - timedelta(days=3) if a.date.isoweekday() > 3 else a.date, a.date, substitute_type="any_sub")
        approve_and_check(s, t, plan_absence(s, ab))
        assert folded in {k for st in snapshot(sec).values() for k in state_keys(st)} | {o.key for o in sec.owed if o.open}


def test_lb22_continuation_days_survive_an_absence_on_the_original_day(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        sec = t.sections[2]
        day = next(e for e in entries_between(s, sec.id, date(2026, 9, 14), date(2026, 9, 25)) if e.lesson and not e.lesson.is_assessment)
        approve_and_check(s, t, [reconcile_slip(s, sec, day.date, 1)])
        cont_key = f"{day.lesson.slug}#c1"
        assert cont_key in {k for st in snapshot(sec).values() for k in state_keys(st)}
        ab = create_absence(s, t.id, day.date, day.date, substitute_type="any_sub")
        approve_and_check(s, t, plan_absence(s, ab))
        assert cont_key in {k for st in snapshot(sec).values() for k in state_keys(st)} | {o.key for o in sec.owed if o.open}


def test_lb23_the_readme_federalism_example_compresses_with_shipped_lessons():
    from lessonbridge.providers.defaults import civics_7

    unit = next(u for u in civics_7().units if u.slug == "federalism-state-government")
    by = {l.slug: l for l in unit.lessons}
    names = ["federalism-intro", "federalism-guided-practice", "federalism-review", "federalism-quiz"]

    def item(slug, i):
        l = by[slug]
        kind = "assessment" if l.lesson_type == "assessment" else ("review" if l.lesson_type == "review" else "lesson")
        return Item(key=slug, title=l.title, kind=kind, lesson_slug=slug, lesson_type=l.lesson_type, min_minutes=l.minimum_viable_minutes, duration=l.duration_minutes,
                    prereqs=set(l.prerequisites), unit=unit.slug, sequence=i, delivery=set(l.delivery_requirement))

    items = [item(n, i) for i, n in enumerate(names)]
    mon = date(2026, 10, 26)
    days = [mon + timedelta(days=i) for i in range(4)]
    sub = Item("sub", "Constitution review [SUB]", kind="filler", pinned=mon, is_sub_day=True)
    res = repair([Slot(d, 1, existing_key=it.key, existing_title=it.title) for d, it in zip(days, items)], [sub] + items, minutes=50, study_minutes=15)
    titles = [it.title for _, it in res.assignments]
    assert titles[1].startswith("Introduction to federalism + ")
    assert titles[3] == "Federalism Quiz" and not res.deferred


def test_lb24_absences_are_classified_by_school_days_and_validated(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        assert create_absence(s, tid, date(2026, 11, 23), date(2026, 12, 3)).absence_type.value == "short"  # 6 school days
        assert create_absence(s, tid, date(2026, 12, 17), date(2027, 1, 5)).absence_type.value == "short"  # winter break
        assert create_absence(s, tid, date(2026, 11, 30), date(2026, 12, 10)).substitute_type.value == "any_sub"
        with pytest.raises(PlanningError):
            create_absence(s, tid, date(2026, 10, 30), date(2026, 10, 26))
        with pytest.raises(PlanningError):
            create_absence(s, tid, date(2026, 12, 26), date(2026, 12, 27))  # no school days
    assert classify_absence(11).value == "extended_leave"


def test_lb34_lb35_lb36_lb52_leave_artefacts(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        one = create_absence(s, t.id, date(2026, 10, 26), date(2026, 10, 26), substitute_type="any_sub")
        plan_absence(s, one)
        apply_all_for_absence(s, one)
        first_brief = [len(x.moved) for x in leave_mod.return_brief(s, one).sections]
        leave = create_absence(s, t.id, date(2026, 11, 9), date(2027, 1, 29), substitute_type="long_term_sub")
        plan_absence(s, leave)
        apply_all_for_absence(s, leave, acknowledge=True)
        assert [len(x.moved) for x in leave_mod.return_brief(s, one).sections] == first_brief  # LB-36
        pre = leave_mod.pre_leave_analysis(s, leave)
        assert pre["leave_window"]["return"] == "2027-02-01"  # LB-52: a Monday, not Saturday the 30th
        assert any(sec["sensitive_dependencies"] for sec in pre["sections"])  # LB-35
        drafted = leave_mod.draft_placeholders(s, leave)
        days = [e for sec in t.sections for e in leave_mod.handoff_days(s, leave, sec)]
        assert drafted and sum(1 for e in days if e.lesson is None and e.kind.value == "placeholder") == 0
        packet = leave_mod.build_handoff(s, leave)
        handoff = md.render_handoff(packet, pre)
        assert "15 minutes to study" in handoff and "assessment days" in handoff  # LB-34
        weeks = leave_mod.weekly_frameworks(s, leave)
        assert all(any("Substitutes may not grade" in c for c in w.constraints) for w in weeks)


def test_lb37_plans_are_marked_stale_when_their_day_changes(onboarded):
    from lessonbridge.generation.subplan import generate_sub_plans

    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        ab = create_absence(s, t.id, date(2026, 10, 27), date(2026, 10, 27), substitute_type="any_sub")
        plan_absence(s, ab)
        apply_all_for_absence(s, ab)
        plans = generate_sub_plans(s, ab, cfg=cfg)
        assert all(p.status.value == "accepted" for p in plans)
        cancel_props = cancel_absence(s, ab)
        approve_and_check(s, t, cancel_props)
        assert ab.status.value == "cancelled"
        assert all(p.status.value == "stale" for p in ab.sub_plans)


def test_lb43_lb58_cancel_undo_reject_and_delete(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        ab = create_absence(s, t.id, date(2026, 10, 27), date(2026, 10, 27), substitute_type="any_sub")
        props = plan_absence(s, ab)
        approve_and_check(s, t, props)
        with pytest.raises(ProposalNotPending):
            reject_proposal(s, props[0])  # LB-58: approved proposals are undone, not "rejected"
        before = snapshot(t.sections[0])
        undo = propose_undo(s, props[0])
        apply_proposal(s, undo)
        assert props[0].status == ProposalStatus.reverted
        assert snapshot(t.sections[0]) != before
        # A never-approved absence is cancelled at once, and absences and teachers can be deleted.
        other = create_absence(s, t.id, date(2026, 11, 16), date(2026, 11, 16), substitute_type="any_sub")
        plan_absence(s, other)
        assert cancel_absence(s, other) == [] and other.status.value == "cancelled"
        s.delete(other)
        s.flush()
        s.delete(t)
        s.flush()
        assert s.get(Teacher, tid) is None


def test_lb41_generation_does_not_hold_the_write_lock_during_model_calls(onboarded, monkeypatch):
    from lessonbridge.generation import subplan as sp
    from lessonbridge.profile.rules import add_rule

    cfg, tid = onboarded
    with db.session_scope() as s:
        ab = create_absence(s, tid, date(2026, 10, 27), date(2026, 10, 27), substitute_type="any_sub")
        plan_absence(s, ab)
        apply_all_for_absence(s, ab)
        aid = ab.id
    real = sp.run_job
    writes = []

    def slow_job(ctx, llm, cfg_):
        with db.session_scope() as other:  # another request writing while a "model call" is in flight
            add_rule(other, tid, f"Pencils are on the back table ({len(writes)}).")
        writes.append(1)
        return real(ctx, llm, cfg_)

    monkeypatch.setattr(sp, "run_job", slow_job)
    ids = sp.generate_plans_for_absence(aid, cfg=cfg)
    assert ids and writes


def test_lb48_preference_values_are_parsed(onboarded):
    from lessonbridge.absence.decisions import pref_bool, pref_int

    assert pref_bool({"x": "no"}, "x", True) is False and pref_bool({"x": "Off"}, "x", True) is False and pref_bool({"x": "huh"}, "x", True) is True
    assert pref_int({"x": "abc"}, "x", 5) == 5
    quiz = Item("quiz", "Quiz", kind="assessment", lesson_type="assessment", sequence=0, unit="u1", min_minutes=40)
    nxt = Item("n", "Next unit intro", sequence=1, unit="u2")
    mon = date(2026, 10, 26)
    res = repair([Slot(mon, 1, existing_key="quiz"), Slot(mon + timedelta(days=1), 1, existing_key="n")], [quiz, nxt], minutes=50, soft_prefs={"no_monday_assessments": "no"})
    assert [it.title for _, it in res.assignments] == ["Quiz", "Next unit intro"]


def test_lb47_monday_swap_never_separates_an_assessment_from_its_review():
    mon = date(2026, 10, 26)
    rev = Item("rev", "Review", kind="review", lesson_type="review", sequence=0, unit="u1")
    quiz = Item("quiz", "Quiz", kind="assessment", lesson_type="assessment", sequence=1, unit="u1", prereqs={"rev"})
    nxt = Item("n", "Next unit intro", sequence=2, unit="u2")
    days = [mon - timedelta(days=3), mon, mon + timedelta(days=1)]
    res = repair([Slot(d, 1) for d in days], [rev, quiz, nxt], minutes=50)
    assert [it.title for _, it in res.assignments] == ["Review", "Quiz", "Next unit intro"]


def test_lb50_replacement_activities_follow_the_current_unit():
    from lessonbridge.absence.decisions import ReplacementOption, choose_replacement, keywords
    from lessonbridge.providers.defaults import default_replacement_activities

    opts = [ReplacementOption(a["slug"], a["title"], a["description"], a["duration_minutes"], a["category"], a["materials"], a["student_output"], a["tags"])
            for a in default_replacement_activities() if a["subject"] == "civics"]
    pick = choose_replacement(opts, used=set(), context=keywords("Local Government and Public Policy", "local policy"))
    assert pick.slug != "constitution-review"


def test_lb53_early_release_days_use_the_shorter_period(onboarded):
    from lessonbridge.generation.subplan import build_context

    cfg, tid = onboarded
    with db.session_scope() as s:
        t = teacher(s, tid)
        ab = create_absence(s, t.id, date(2026, 11, 24), date(2026, 11, 24), substitute_type="any_sub")
        plan_absence(s, ab)
        apply_all_for_absence(s, ab, acknowledge=True)
        sec = t.sections[0]
        e = next(x for x in sec.calendar if x.date == date(2026, 11, 24))
        ctx = build_context(s, ab, sec, e)
        assert ctx["early_release"] and ctx["class_minutes"] == 30


def test_lb55_parallel_sections_share_one_generation(onboarded):
    from lessonbridge.generation import subplan as sp

    cfg, tid = onboarded
    calls = []
    real = sp.run_job

    def counting(ctx, llm, cfg_):
        calls.append(ctx["course"])
        return real(ctx, llm, cfg_)

    with db.session_scope() as s:
        ab = create_absence(s, tid, date(2026, 10, 27), date(2026, 10, 27), substitute_type="any_sub")
        plan_absence(s, ab)
        apply_all_for_absence(s, ab)
        sp.run_job = counting
        try:
            plans = sp.generate_sub_plans(s, ab, cfg=cfg)
        finally:
            sp.run_job = real
    assert len(plans) == 4 and len(calls) == 2  # one per course, not one per section


def test_lb54_onboarding_counts_only_explicit_teacher_actions(onboarded):
    from lessonbridge.models import TeacherTouch

    cfg, tid = onboarded
    with db.session_scope() as s:
        labels = [t.label for t in s.scalars(select(TeacherTouch).where(TeacherTouch.teacher_id == tid))]
    assert not any("review" in l for l in labels)
