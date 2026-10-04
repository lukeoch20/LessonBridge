"""Pure planning core: the design document's worked examples."""
from datetime import date, timedelta

from lessonbridge.absence.decisions import ReplacementOption, decide_days
from lessonbridge.absence.engine import Item, Slot, merge_items, repair

MON = date(2026, 10, 26)
DAYS = [MON + timedelta(days=i) for i in range(5)]
ANY = {"regular_teacher", "long_term_sub", "any_sub"}


def federalism_items():
    return [
        Item("intro", "Introduce Federalism", lesson_type="direct_instruction", min_minutes=25, sequence=1, unit="fed"),
        Item("gp", "Guided Practice", lesson_type="guided_practice", min_minutes=20, prereqs={"intro"}, sequence=2, unit="fed", delivery=ANY),
        Item("rev", "Review", kind="review", lesson_type="review", min_minutes=20, prereqs={"gp"}, sequence=3, unit="fed", delivery=ANY),
        Item("quiz", "Quiz", kind="assessment", lesson_type="assessment", min_minutes=35, prereqs={"rev"}, sequence=4, unit="fed", delivery=ANY),
    ]


def slots_for(items, days):
    return [Slot(d, 1, existing_key=i.key if i else None, existing_title=i.title if i else None, existing_kind=(i.kind if i else "flex")) for d, i in zip(days, items)]


def test_lost_monday_compresses_instead_of_pushing_quiz_past_quarter_end():
    items = federalism_items()
    sub = Item("sub", "Constitution review [SUB]", kind="filler", pinned=MON, is_sub_day=True)
    res = repair(slots_for(items, DAYS[:4]), [sub] + items, minutes=50, study_minutes=15)
    titles = [it.title for _, it in res.assignments]
    assert titles[0] == "Constitution review [SUB]"
    assert titles[1].startswith("Introduce Federalism +")  # merged with shortened guided practice
    assert titles[2] == "Review"
    assert titles[3] == "Quiz"  # quiz stays before the quarter boundary
    assert not res.unresolved and not res.deferred


def test_slack_absorbs_a_lost_day_when_flex_exists():
    items = federalism_items()
    flex = Item("flex-1", "Flex / work day", kind="flex", sequence=2, unit="fed", priority="optional", min_minutes=0, duration=0)
    seq = [items[0], items[1], flex, items[2], items[3]]
    for i, it in enumerate(seq):
        it.sequence = i
    sub = Item("sub", "Reading [SUB]", kind="filler", pinned=MON, is_sub_day=True)
    res = repair(slots_for(seq, DAYS), [sub] + seq, minutes=50)
    titles = [it.title for _, it in res.assignments]
    assert titles == ["Reading [SUB]", "Introduce Federalism", "Guided Practice", "Review", "Quiz"]
    assert not res.merged_pairs


def test_nearest_flex_is_used_so_cascade_stays_short():
    a = Item("a", "A", sequence=0, unit="u")
    f1 = Item("f1", "Flex 1", kind="flex", sequence=1, unit="u", priority="optional", min_minutes=0)
    b = Item("b", "B", sequence=2, unit="u")
    f2 = Item("f2", "Flex 2", kind="flex", sequence=3, unit="u", priority="optional", min_minutes=0)
    c = Item("c", "C", sequence=4, unit="u")
    seq = [a, f1, b, f2, c]
    cont = Item("a-cont", "A (continued)", sequence=0, unit="u", prereqs={"a"})
    a.pinned = DAYS[0]
    cont.sequence = 0.5
    res = repair(slots_for(seq, DAYS), [a, cont, f1, b, f2, c], minutes=50)
    titles = [it.title for _, it in res.assignments]
    assert titles == ["A", "A (continued)", "B", "Flex 2", "C"]


def test_unresolved_when_required_content_cannot_fit():
    items = [Item(f"l{i}", f"Lesson {i}", sequence=i, unit="u", min_minutes=40, lesson_type="assessment", kind="assessment") for i in range(4)]
    sub = Item("sub", "Sub day", kind="filler", pinned=DAYS[0], is_sub_day=True)
    res = repair(slots_for(items, DAYS[:4]), [sub] + items, minutes=50, allow_defer=False)
    assert res.unresolved
    assert len(res.deferred) == 1


def test_defers_non_assessment_next_unit_content_past_boundary():
    u1 = [Item("a", "A", sequence=0, unit="u1"), Item("q", "Quiz", kind="assessment", lesson_type="assessment", sequence=1, unit="u1", min_minutes=45, prereqs={"a"})]
    u2 = [Item("b", "Next unit intro", sequence=2, unit="u2")]
    sub = Item("sub", "Sub day", kind="filler", pinned=DAYS[0], is_sub_day=True)
    res = repair(slots_for(u1 + u2, DAYS[:3]), [sub] + u1 + u2, minutes=50)
    assert [it.title for _, it in res.assignments] == ["Sub day", "A", "Quiz"]
    assert [d.title for d in res.deferred] == ["Next unit intro"]
    assert not res.unresolved


def test_pinned_assessment_is_unpinned_when_its_review_was_displaced():
    items = federalism_items()
    # Teacher is out Wednesday (review day); quiz Thursday was going to be KEPT but review is displaced.
    sub = Item("sub", "Filler [SUB]", kind="filler", pinned=DAYS[2], is_sub_day=True)
    items[3].pinned = DAYS[3]
    extra = Item("next", "Next unit", sequence=5, unit="u2")
    res = repair(slots_for(items + [extra], DAYS), [sub] + items + [extra], minutes=50, study_minutes=15)
    titles = [it.title for _, it in res.assignments]
    assert titles[2] == "Filler [SUB]"
    assert titles.index("Quiz") > titles.index("Review")


def test_merge_respects_minimum_viable_minutes_and_study_rule():
    rev = Item("rev", "Review", kind="review", lesson_type="review", min_minutes=20, unit="u")
    quiz = Item("quiz", "Quiz", kind="assessment", lesson_type="assessment", min_minutes=30, unit="u")
    assert merge_items(rev, quiz, 50, study_minutes=0) is not None
    assert merge_items(rev, quiz, 50, study_minutes=15) is None  # 20 + 30 + 15 > 50
    assert merge_items(quiz, rev, 60) is None  # nothing is scheduled after an assessment on its day


def test_monday_assessment_swaps_when_harmless():
    quiz = Item("quiz", "Quiz", kind="assessment", lesson_type="assessment", sequence=0, unit="u1", min_minutes=40)
    nxt = Item("n", "Next unit intro", sequence=1, unit="u2")
    res = repair(slots_for([quiz, nxt], [MON, MON + timedelta(days=1)]), [quiz, nxt], minutes=50, soft_prefs={"no_monday_assessments": True})
    assert [it.title for _, it in res.assignments] == ["Next unit intro", "Quiz"]
    assert any("off Monday" in n for n in res.soft_notes)


# ----------------------------------------------------------------- decisions
def reps():
    return [
        ReplacementOption("a", "Constitution review", "", 45, "curriculum_preserving", [], "packet"),
        ReplacementOption("b", "Current events", "", 45, "skill_maintenance", [], "form"),
        ReplacementOption("c", "Emergency packet", "", 45, "emergency_filler", [], "packet"),
    ]


def test_short_absence_decisions():
    items = federalism_items()
    by_date = dict(zip(DAYS, items))
    decs = decide_days(DAYS[:1], by_date, items, substitute_type="any_sub", absence_days=1, replacements=reps())
    assert decs[0].decision == "REPLACE" and decs[0].replacement.slug == "a"
    decs = decide_days([DAYS[1]], by_date, items, substitute_type="any_sub", absence_days=1, replacements=reps())
    assert decs[0].decision == "KEEP"  # guided practice is any_sub
    decs = decide_days([DAYS[3]], by_date, items, substitute_type="any_sub", absence_days=1, replacements=reps())
    assert decs[0].decision == "KEEP"  # sub may administer quiz when review was not disrupted
    decs = decide_days(DAYS[2:4], by_date, items, substitute_type="any_sub", absence_days=2, replacements=reps())
    assert decs[0].decision == "KEEP" and decs[1].decision == "KEEP"
    decs = decide_days(DAYS[:4], by_date, items, substitute_type="any_sub", absence_days=4, replacements=reps())
    assert decs[3].decision == "POSTPONE"  # absence longer than two days
    assert len({d.replacement.slug for d in decs if d.replacement}) >= 2  # does not repeat the same activity


def test_reorder_pulls_forward_sub_deliverable_lesson():
    a = Item("a", "Teacher lesson", sequence=0, unit="u")
    b = Item("b", "Independent reading", sequence=1, unit="u", delivery={"any_sub", "regular_teacher"}, lesson_type="reading")
    decs = decide_days([MON], {MON: a}, [a, b], substitute_type="any_sub", absence_days=1, replacements=reps())
    assert decs[0].decision == "REORDER" and decs[0].reorder_with is b


def test_long_term_sub_keeps_new_material_and_flex():
    items = federalism_items()
    flex = Item("f", "Flex", kind="flex", sequence=9, unit="fed")
    decs = decide_days(DAYS[:2], {DAYS[0]: items[0], DAYS[1]: flex}, items, substitute_type="long_term_sub", absence_days=40, replacements=reps())
    assert [d.decision for d in decs] == ["KEEP", "KEEP"]
    teacher_only = Item("t", "Seminar", sequence=0, unit="u", delivery={"regular_teacher"})
    decs = decide_days([MON], {MON: teacher_only}, [teacher_only], substitute_type="long_term_sub", absence_days=40, replacements=reps())
    assert decs[0].decision == "MODIFY"
