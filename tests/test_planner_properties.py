"""Property-based test of the planner (audit recommendation 1).

Random sequences of absences, slips, progress marks, out-of-order approvals,
cancellations, undos and rebuilds run against the sample calendar. After every
approval: every lesson is still scheduled (or owed) exactly once, prerequisites
come first, every absence day is a substitute day, and completed or skipped
days are untouched. The planner must also never produce a proposal that the
invariant gate blocks.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from lessonbridge import db
from lessonbridge.absence.service import (
    PlanningError,
    StaleProposalError,
    apply_proposal,
    cancel_absence,
    create_absence,
    mark_progress,
    plan_absence,
    propose_undo,
    reconcile_slip,
)
from lessonbridge.config import Settings
from lessonbridge.curriculum.calendar import load_year_structure, propose_rebuild, school_days
from lessonbridge.models import ProposalStatus, Teacher

from invariants import check_section, snapshot
from lessonbridge.absence.state import state_keys

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
_BASE: dict = {}


def base_db() -> tuple[Path, list[date]]:
    if not _BASE:
        from lessonbridge.profile.onboarding import OnboardingInput, run_onboarding

        d = Path(tempfile.mkdtemp(prefix="lb-prop-base-"))
        cfg = Settings(data_dir=d, allow_network=False, generator="template")
        db.init_engine(cfg)
        with db.session_scope() as s:
            run_onboarding(s, OnboardingInput.from_yaml(EXAMPLES / "teacher_profile.yaml"), cfg=cfg, today=date(2026, 9, 1))
            ys = load_year_structure(s, "lcps", "2026-2027")
            days = [x.date for x in school_days(ys, date(2026, 9, 8), date(2027, 2, 12))]
        db.get_engine().dispose()
        _BASE.update(path=d, days=days)
    return _BASE["path"], _BASE["days"]


def fresh_session():
    base, days = base_db()
    d = Path(tempfile.mkdtemp(prefix="lb-prop-"))
    for f in base.iterdir():
        if f.is_file():
            shutil.copy(f, d / f.name)
    db.init_engine(Settings(data_dir=d, allow_network=False, generator="template"))
    return db.get_sessionmaker()(), d, days


ops = st.lists(
    st.one_of(
        st.tuples(st.just("absence"), st.integers(0, 100), st.integers(1, 6), st.sampled_from(["any_sub", "any_sub", "long_term_sub"])),
        st.tuples(st.just("slip"), st.integers(0, 3), st.integers(0, 100), st.integers(1, 2)),
        st.tuples(st.just("mark"), st.integers(0, 3), st.integers(0, 100), st.sampled_from(["completed", "skipped"])),
        st.tuples(st.just("stale_pair"), st.integers(0, 100), st.integers(0, 100)),
        st.tuples(st.just("cancel_last")),
        st.tuples(st.just("undo_last")),
        st.tuples(st.just("rebuild"), st.integers(0, 3), st.integers(0, 100)),
    ),
    min_size=1,
    max_size=6,
)


def approve_checked(s, teacher, props, problems, label, removable=frozenset()):
    before = {sec.id: snapshot(sec) for sec in teacher.sections}
    applied = []
    for p in props:
        assert p.status != ProposalStatus.blocked, f"{label}: planner produced a blocked proposal: {p.constraints['violations']}"
        if p.status == ProposalStatus.pending:
            apply_proposal(s, p, acknowledge=True)
            applied.append(p)
    s.flush()
    for sec in teacher.sections:
        for msg in check_section(s, sec, before[sec.id], removable):
            problems.append(f"{label}: {sec.section_name}: {msg}")
    return applied


@settings(max_examples=int(os.environ.get("LB_PROPERTY_EXAMPLES", "25")), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(ops)
def test_planner_invariants_hold_under_random_operations(sequence):
    s, d, days = fresh_session()
    problems: list[str] = []
    absences, approved = [], []
    try:
        t = s.get(Teacher, 1)
        secs = t.sections
        for op in sequence:
            kind = op[0]
            if kind == "absence":
                a = days[op[1] % len(days)]
                b = days[min(len(days) - 1, (op[1] % len(days)) + op[2] - 1)]
                ab = create_absence(s, t.id, a, b, substitute_type=op[3])
                absences.append(ab)
                approved += approve_checked(s, t, plan_absence(s, ab), problems, f"absence {a}..{b}")
            elif kind == "slip":
                sec = secs[op[1]]
                day = days[op[2] % len(days)]
                try:
                    p = reconcile_slip(s, sec, day, op[3])
                except PlanningError:
                    continue
                approved += approve_checked(s, t, [p], problems, f"slip {sec.period} {day}")
            elif kind == "mark":
                try:
                    mark_progress(s, secs[op[1]], days[op[2] % len(days)], op[3])
                except PlanningError:
                    pass
            elif kind == "stale_pair":
                a = days[op[1] % len(days)]
                ab = create_absence(s, t.id, a, a, substitute_type="any_sub")
                absences.append(ab)
                first = plan_absence(s, ab)
                try:
                    slip = reconcile_slip(s, secs[0], days[op[2] % len(days)], 1)
                except PlanningError:
                    slip = None
                if slip is not None:
                    approved += approve_checked(s, t, [slip], problems, "slip before stale absence")
                for p in first:
                    before = {sec.id: snapshot(sec) for sec in t.sections}
                    try:
                        approved += approve_checked(s, t, [p], problems, f"possibly stale absence {a}")
                    except StaleProposalError:
                        for sec in t.sections:  # a refused stale approval must leave the calendar untouched
                            assert snapshot(sec) == before[sec.id]
            elif kind == "cancel_last" and absences:
                ab = absences.pop()
                if ab.status.value != "cancelled":
                    approved += approve_checked(s, t, cancel_absence(s, ab), problems, f"cancel absence {ab.start_date}")
            elif kind == "undo_last" and approved:
                p = approved.pop()
                if p.status == ProposalStatus.approved:
                    try:
                        u = propose_undo(s, p)
                    except (StaleProposalError, PlanningError):
                        continue
                    created = set()
                    for raw in p.diff:
                        created |= set(state_keys(raw.get("after"))) - set(state_keys(raw.get("before")))
                    approve_checked(s, t, [u], problems, f"undo {p.kind}", created)
            elif kind == "rebuild":
                sec = secs[op[1]]
                prop, _ = propose_rebuild(s, sec, start=days[op[2] % len(days)])
                approved += approve_checked(s, t, [prop], problems, f"rebuild {sec.period}")
        assert not problems, "\n".join(problems[:10])
    finally:
        s.rollback()
        s.close()
        db.get_engine().dispose()
        shutil.rmtree(d, ignore_errors=True)
