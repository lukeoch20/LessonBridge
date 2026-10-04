from datetime import date

from sqlalchemy import select

from lessonbridge import db
from lessonbridge.absence import leave as leave_mod
from lessonbridge.absence.service import apply_proposal, create_absence, mark_progress, plan_absence, reconcile_slip
from lessonbridge.curriculum.calendar import entries_between
from lessonbridge.generation.subplan import generate_sub_plans
from lessonbridge.models import CalendarEntry, EntryKind, Proposal, ProposalStatus, Teacher
from lessonbridge.profile.onboarding import count_touches
from lessonbridge.render import markdown as md
from lessonbridge.schemas import DiffItem


def test_onboarding_builds_context_with_few_touches(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = s.get(Teacher, tid)
        assert len(t.sections) == 4
        assert all(len(sec.units) >= 9 for sec in t.sections)
        for sec in t.sections:
            entries = sec.calendar
            assert len(entries) >= 175
            # Quarter-aware: the first Q2 unit from the syllabus starts after the Q1 boundary.
            q2_first = next(e for e in entries if e.unit and e.unit.quarter == 2)
            assert q2_first.date > date(2026, 10, 30)
            lessons = [e for e in entries if e.lesson_id]
            assert len(lessons) >= 60
        assert count_touches(s, tid, "onboarding") <= 12
        assert any(r.structured.get("action") == "study_period" for r in t.rules)


def test_one_day_absence_proposal_apply_plans_and_brief(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        ab = create_absence(s, tid, date(2026, 10, 26), date(2026, 10, 26), reason="sick")
        props = plan_absence(s, ab)
        assert len(props) == 4 and all(p.status == ProposalStatus.pending for p in props)
        # Nothing changed on the calendar yet (human control).
        for p in props:
            sec_entries = {e.date: e for e in entries_between(s, p.teacher_course_id, date(2026, 10, 26), date(2026, 10, 26))}
            assert not sec_entries[date(2026, 10, 26)].is_sub_day
            diff = [DiffItem(**d) for d in p.diff]
            assert diff[0].date == date(2026, 10, 26) and diff[0].is_sub_day
            # Window never extends past the Q1 boundary.
            assert max(d.date for d in diff) <= date(2026, 10, 30)
            text = md.render_proposal(p.explanation, diff, p.constraints, heading="x")
            assert "PROPOSED CALENDAR CHANGES" in text
        for p in props:
            apply_proposal(s, p)
        for p in props:
            e = entries_between(s, p.teacher_course_id, date(2026, 10, 26), date(2026, 10, 26))[0]
            assert e.is_sub_day and e.origin == "reconciliation"
        plans = generate_sub_plans(s, ab, cfg=cfg)
        assert len(plans) == 4 and all(p.status.value == "accepted" for p in plans)
        for p in plans:
            assert p.content["total_minutes"] == 50
            # The study-period rule is assessment-triggered, so it must not leak into a non-quiz day.
            assert not any("15 minutes to study" in r for r in p.content["classroom_rules"])
            assert any("Substitutes may not grade" in r for r in p.content["classroom_rules"])
            assert "LESSONBRIDGE SUBSTITUTE PLAN" in p.rendered_markdown
        for sec in s.get(Teacher, tid).sections:
            mark_progress(s, sec, date(2026, 10, 26), "completed")
        brief = leave_mod.return_brief(s, ab)
        assert len(brief.sections) == 4
        assert all(sec.completed for sec in brief.sections)
        assert all("Resume with" in sec.recommended_reentry_point for sec in brief.sections)


def test_extended_leave_packet(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        leave = create_absence(s, tid, date(2026, 11, 9), date(2027, 1, 29), reason="maternity leave")
        assert leave.absence_type.value == "extended_leave" and leave.substitute_type.value == "long_term_sub"
        props = plan_absence(s, leave)
        for p in props:
            diff = [DiffItem(**d) for d in p.diff]
            # Long-term sub keeps the sequence: no filler replacements.
            assert not any(d.action == "replace" for d in diff)
            apply_proposal(s, p)
        pre = leave_mod.pre_leave_analysis(s, leave)
        assert any("Quarter 2 ends" in h for h in pre["hard_deadlines"])
        assert all(sec["course_days_during_leave"] > 30 for sec in pre["sections"])
        plans = generate_sub_plans(s, leave, days_limit=10, cfg=cfg)
        assert len(plans) == 40  # 10 detailed days x 4 sections
        packet = leave_mod.build_handoff(s, leave, {p.calendar_entry_id: p.id for p in plans})
        assert all(len(sec["days"]) == 10 for sec in packet["sections"])
        weeks = leave_mod.weekly_frameworks(s, leave)
        assert weeks and all(w.week_start > date(2026, 11, 20) for w in weeks)
        assert any(w.required_assessments for w in weeks)
        assert "WEEKLY PACING FRAMEWORKS" in md.render_weekly(weeks)


def test_slip_is_absorbed_by_nearby_slack(onboarded):
    cfg, tid = onboarded
    with db.session_scope() as s:
        t = s.get(Teacher, tid)
        civ = t.sections[2]
        entry = next(e for e in entries_between(s, civ.id, date(2026, 9, 8), date(2026, 10, 20)) if e.lesson and not e.lesson.is_assessment)
        prop = reconcile_slip(s, civ, entry.date, 1)
        diff = [DiffItem(**d) for d in prop.diff]
        changed = [d for d in diff if d.action not in ("keep", "preserve")]
        assert 1 <= len(changed) <= 6  # short cascade thanks to flex
        assert any("(continued)" in (d.after_title or "") for d in changed)
        n = apply_proposal(s, prop)
        assert n >= 1
        after = {e.date: e for e in entries_between(s, civ.id, entry.date, entry.date + __import__("datetime").timedelta(days=10))}
        assert any("(continued)" in e.title for e in after.values())
