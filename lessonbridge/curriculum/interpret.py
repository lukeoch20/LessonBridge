"""Curriculum interpretation.

Turns the teacher's syllabus / pacing guide (plus the district default
sequence) into units and lessons. Source precedence: teacher-confirmed >
teacher syllabus/pacing guide > district curriculum > state standards >
LessonBridge inference. Conflicts and low-confidence matches are surfaced for
the teacher, never resolved silently, and nothing is invented: a teacher unit
with no matching default gets "lesson to be detailed" days, not made-up lessons
or assessments (LB-11).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CurriculumUnit, Lesson, LessonDependency, LessonType, Priority, TeacherCourse
from ..providers.defaults import DEFAULT_CURRICULA
from ..schemas import CurriculumSpec, ExtractedTeacherContext, RuleSpec, UnitSpec

log = logging.getLogger(__name__)

_STOP = {"the", "and", "of", "a", "an", "to", "in", "unit", "quarter", "week", "weeks", "with", "for", "on", "its", "our", "us", "s", "vs"}
MONTHS = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_MONTH_RE = re.compile(rf"\b{MONTHS}\b", re.I)
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")

_QUARTER = re.compile(r"^(?:quarter|qtr|q|marking\s+period|mp|nine\s+weeks|9\s+weeks)\s*([1-4])\b\s*[:\-–—.)]?\s*(.*)$", re.I)
_QUARTER_ORD = re.compile(r"^(first|second|third|fourth)\s+(?:quarter|marking\s+period|nine\s+weeks)\b\s*[:\-–—.)]?\s*(.*)$", re.I)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_UNIT = re.compile(r"^(?:unit|module|topic)\s*([0-9]+[a-z]?)?\s*[:\-–—.)]\s*(.+?)$", re.I)
_WEEK = re.compile(r"^weeks?\s*(\d+)(?:\s*(?:-|–|—|to|through)\s*(\d+))?\s*(?:\(([^)]*)\))?\s*[:\-–—.)]\s*(.+?)$", re.I)
_LENGTH = re.compile(r"[\s(\[,–—-]*\(?\s*(\d+)\s*(days?|class\s+periods?|periods?|lessons?|blocks?|weeks?)\s*\)?\]?\s*$", re.I)
_RULE_HINT = re.compile(r"\b(quiz|quizzes|test|tests|assessment|minutes|late work|early finisher|substitute|sub\b|homework|chromebook|device|phone|bathroom|hall pass|collect|grade|grading|independent reading|procedure|policy|expectation|attendance|seat|seating|dismiss|bell|lunch)", re.I)
_MODAL = re.compile(r"\b(will|must|should|may|are|is|receive|receives|get|gets|have|has|no|always|never|only|need|needs)\b", re.I)
_IMPERATIVE = re.compile(r"^(?:please\s+)?(take|do|don't|do not|never|always|collect|give|allow|keep|have|ensure|check|leave|send|let|make|follow|use|return|remind|provide|start|begin|write|read|call|sign|record|place|put|hand|distribute|supervise|walk|line|dismiss|post|turn|seat|assign|direct|tell|ask|remember|note|report|lock|close|open|pass|monitor)\b", re.I)
_NOT_RULES = ("students need", "students bring", "bring ", "you will need", "materials", "welcome", "this course", "grading scale", "contact")

SUBJECT_WORDS = {
    "english": {"english", "reading", "writing", "novel", "poetry", "fiction", "narrative", "essay", "grammar", "vocabulary", "literary", "argumentative", "nonfiction", "author", "theme", "literature", "language arts", "ela"},
    "civics": {"civics", "government", "constitution", "citizenship", "economics", "federalism", "court", "courts", "political", "amendments", "judicial", "legislative", "executive", "policy", "economy", "founding", "rights", "voting"},
}
SUBJECT_HEADINGS = {"english": re.compile(r"\b(english|language arts|ela)\b", re.I), "civics": re.compile(r"\b(civics|government|social studies|economics)\b", re.I)}


def _keywords(text: str) -> set[str]:
    return {w.rstrip("s") if len(w) > 4 else w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "unit"


# ---------------------------------------------------------------- line prep
def clean_line(raw: str) -> str:
    """Strip Markdown heading / list / emphasis markup before matching (LB-28)."""
    line = raw.strip()
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^(?:[-*+•·▪◦]|\d{1,2}[.)]|[a-z][.)])\s+", "", line, flags=re.I)
    line = re.sub(r"(\*\*|__|`)", "", line)
    line = re.sub(r"^\*(.+)\*$", r"\1", line)
    line = re.sub(r"\s+", " ", line)
    return line.strip(" \t:")


def table_cells(raw: str) -> Optional[list[str]]:
    if raw.count("|") < 1:
        return None
    cells = [c.strip() for c in raw.strip().strip("|").split("|")]
    if len(cells) < 2:
        return None
    if all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
        return []  # Markdown separator row
    return [clean_line(c) for c in cells]


def looks_like_title(text: str) -> bool:
    """Text after a quarter number is a unit title only if it reads like one (LB-14)."""
    t = text.strip(" -–—:()")
    if len(t) < 4 or text.strip().startswith("("):
        return False
    if _MONTH_RE.search(t) or _DATE_RE.search(t):
        return False
    if re.search(r"\b(begins?|starts?|ends?|weeks?\s*\d|days?\s*\d)\b", t, re.I):
        return False
    if sum(ch.isdigit() for ch in t) > 2:
        return False
    return True


def split_length(title: str) -> tuple[str, Optional[int], bool]:
    """'Narrative Writing (12 days)' -> ('Narrative Writing', 12, False); weeks are converted to days."""
    m = _LENGTH.search(title)
    if not m:
        return title.strip(" -–—:"), None, False
    n, unit = int(m.group(1)), m.group(2).lower()
    days = n * 5 if unit.startswith("week") else n
    return title[: m.start()].strip(" -–—:,("), days, unit.startswith("week")


# ---------------------------------------------------------------- tables
_HEADER_KEYS = {
    "quarter": re.compile(r"^(quarter|qtr|q|mp|marking period|term|nine weeks)$", re.I),
    "unit": re.compile(r"^(unit|unit #|unit no\.?|#|no\.?)$", re.I),
    "title": re.compile(r"^(title|topic|unit title|unit name|name|focus|theme|content|unit/topic)$", re.I),
    "days": re.compile(r"^(days|# of days|number of days|length|duration|class days|periods|lessons)$", re.I),
    "weeks": re.compile(r"^(weeks|# of weeks|number of weeks)$", re.I),
}


@dataclass
class _TableState:
    columns: dict[str, int] = field(default_factory=dict)


def parse_table_row(cells: list[str], state: _TableState, quarter: Optional[int]) -> Optional[UnitSpec]:
    """A pacing-guide table row -> unit (LB-28). Header rows set the column map."""
    lowered = [c.lower().strip() for c in cells]
    header = {}
    for i, c in enumerate(lowered):
        for key, rx in _HEADER_KEYS.items():
            if rx.match(c):
                header.setdefault(key, i)
    if len(header) >= 2 and ("title" in header or "unit" in header):
        state.columns = header
        return None
    col = state.columns
    q, title, days = quarter, None, None
    if col:
        get = lambda k: cells[col[k]] if k in col and col[k] < len(cells) else ""
        qtxt = get("quarter")
        mq = re.search(r"([1-4])", qtxt or "")
        if mq:
            q = int(mq.group(1))
        title = get("title") or get("unit")
        unit_cell = get("unit")
        if get("title") and unit_cell and not re.fullmatch(r"\d+[a-z]?", unit_cell):
            title = get("title")
        dtxt = get("days") or ""
        wtxt = get("weeks") or ""
        if re.search(r"\d+", dtxt):
            days = int(re.search(r"\d+", dtxt).group(0)) * (5 if "week" in dtxt.lower() else 1)
        elif re.search(r"\d+", wtxt):
            days = int(re.search(r"\d+", wtxt).group(0)) * 5
    else:
        for c in cells:
            if re.fullmatch(r"(?:q(?:uarter)?\s*)?([1-4])", c, re.I) and q is None:
                q = int(re.search(r"[1-4]", c).group(0))
            elif re.fullmatch(r"\d+\s*(?:days?|weeks?)?", c, re.I):
                n = int(re.search(r"\d+", c).group(0))
                if "week" in c.lower():
                    days = n * 5
                elif days is None and title is not None:
                    days = n
            elif title is None and re.search(r"[A-Za-z]{3,}", c):
                mu = _UNIT.match(c)
                title = mu.group(2) if mu else c
        if title is None or (days is None and q is None and not _UNIT.match(cells[0] if cells else "")):
            return None  # e.g. a grading table, not a pacing table
    if not title:
        return None
    mu = _UNIT.match(title)
    if mu:
        title = mu.group(2)
    title, stated, _ = split_length(title)
    if days is None:
        days = stated
    if not title or not re.search(r"[A-Za-z]{3,}", title):
        return None
    return UnitSpec(slug=_slug(title), title=title, quarter=q, planned_days=days, source="syllabus")


# ---------------------------------------------------------------- subjects
def score_subject(text: str) -> dict[str, int]:
    words = re.findall(r"[a-z]+(?: arts)?", text.lower())
    return {s: sum(1 for w in words if w in kws) for s, kws in SUBJECT_WORDS.items()}


def infer_subject(text: str) -> Optional[str]:
    scores = score_subject(text)
    best = max(scores, key=scores.get)
    others = [v for k, v in scores.items() if k != best]
    if scores[best] >= 3 and scores[best] >= 2 * max(others + [0]):
        return best
    return None


def split_by_subject(text: str) -> dict[str, str]:
    """Split a combined document at short headings naming a subject (LB-29). '' holds text before any heading."""
    parts: dict[str, list[str]] = {"": []}
    current = ""
    for raw in text.splitlines():
        line = clean_line(raw)
        is_heading = raw.lstrip().startswith("#") or (0 < len(line) < 60 and not line.endswith(".") and not _UNIT.match(line) and not _QUARTER.match(line))
        if is_heading:
            hits = [s for s, rx in SUBJECT_HEADINGS.items() if rx.search(line)]
            if len(hits) == 1 and not _UNIT.match(line):
                current = hits[0]
                parts.setdefault(current, [])
                continue
        parts[current].append(raw)
    return {k: "\n".join(v) for k, v in parts.items() if "\n".join(v).strip()}


# ------------------------------------------------------------ extraction
def _rule_candidate(line: str, *, procedures: bool) -> bool:
    low = line.lower()
    if len(line) < 12 or len(line) > 300 or low.startswith(_NOT_RULES):
        return False
    if _UNIT.match(line) or _QUARTER.match(line) or _WEEK.match(line):
        return False
    if re.search(r"\bweighted\b|grading scale|\bpercent of\b|of (?:the|your) grade", low) or low.count("%") >= 2:
        return False  # grade weighting, not a classroom procedure
    if procedures:
        return len(line.split()) >= 3  # every instruction in a procedures document counts (LB-30)
    if not _RULE_HINT.search(line):
        return False
    return bool(_MODAL.search(line) or _IMPERATIVE.match(line))


def _sentences(line: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", line)
    return [p.strip() for p in parts if p.strip()]


def heuristic_extract(text: str, subject: str, *, doc_type: str = "syllabus") -> ExtractedTeacherContext:
    """Deterministic extraction used when no LLM is configured (and as a safety net)."""
    units: list[UnitSpec] = []
    rules: list[RuleSpec] = []
    materials: set[str] = set()
    assessments: list[str] = []
    prefs: dict[str, str] = {}
    notes: list[str] = []
    quarter: Optional[int] = None
    table = _TableState()
    procedures = doc_type == "classroom_procedures"

    def add_unit(u: UnitSpec) -> None:
        if units and u.from_weeks and units[-1].from_weeks and units[-1].title.lower() == u.title.lower() and units[-1].quarter == u.quarter:
            units[-1].planned_days = (units[-1].planned_days or 0) + (u.planned_days or 0)  # consecutive weeks of one topic (LB-26)
            return
        if any(x.slug == u.slug and x.quarter == u.quarter for x in units):
            return
        units.append(u)

    for raw in text.splitlines():
        if not raw.strip():
            continue
        cells = table_cells(raw)
        if cells is not None:
            if cells:
                u = parse_table_row(cells, table, quarter)
                if u is not None:
                    add_unit(u)
            continue
        line = clean_line(raw)
        if not line:
            continue
        is_heading = raw.lstrip().startswith("#") or (len(line) < 70 and not re.search(r"[.!?;:]$", line) and sum(w[:1].isupper() for w in line.split()) >= max(2, len(line.split()) - 1))
        mq = _QUARTER.match(line) or _QUARTER_ORD.match(line)
        if mq and len(line) < 90:
            quarter = int(mq.group(1)) if mq.group(1).isdigit() else _ORDINALS[mq.group(1).lower()]
            rest = mq.group(2).strip(" :-–—")
            if rest and looks_like_title(rest) and not _RULE_HINT.search(rest):
                title, days, _ = split_length(rest)
                add_unit(UnitSpec(slug=_slug(title), title=title, quarter=quarter, planned_days=days, source="syllabus"))
            table = _TableState()
            continue
        mw = _WEEK.match(line)
        if mw and len(line) < 160:
            a, b = int(mw.group(1)), int(mw.group(2) or mw.group(1))
            title, _days, _ = split_length(mw.group(4))
            if title and re.search(r"[A-Za-z]{3,}", title):
                add_unit(UnitSpec(slug=_slug(title), title=title, quarter=quarter, planned_days=(b - a + 1) * 5, source="syllabus", from_weeks=True))
            continue
        mu = _UNIT.match(line)
        if mu and len(line) < 160:
            title, days, _ = split_length(mu.group(2))
            if title and re.search(r"[A-Za-z]{3,}", title):
                add_unit(UnitSpec(slug=_slug(title), title=title, quarter=quarter, planned_days=days, source="syllabus"))
            continue
        for sent in ([] if is_heading else (_sentences(line) if procedures else [line])):
            if _rule_candidate(sent, procedures=procedures):
                if sent not in [r.text for r in rules]:
                    rules.append(RuleSpec(text=sent, category="procedure"))
        if re.search(r"\b(quiz|test|exam|essay due|project due|assessment)\b", line, re.I) and len(line) < 120:
            assessments.append(line)
        for m in re.finditer(r"\b(textbook|novel|packet|slides|workbook|chromebook|notebook|binder|handout)\b", line, re.I):
            materials.add(m.group(1).lower())
        if "prefer" in line.lower() and len(line) < 200:
            prefs[_slug(line)[:40]] = line
    if procedures:
        notes.append(f"Captured {len(rules)} procedure(s) as classroom rules.")
    return ExtractedTeacherContext(units=units, rules=rules, preferences=prefs, materials=sorted(materials), assessments=assessments[:30], confidence=0.4 if units else 0.2, notes=notes, subject=subject)


# -------------------------------------------------------------------- merging
@dataclass
class MergeResult:
    spec: CurriculumSpec
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def match_score(teacher_title: str, default_title: str, default_summary: str = "") -> float:
    """Whole-title similarity (LB-11): coverage both ways, Jaccard, and a weak summary signal."""
    t, d, s = _keywords(teacher_title), _keywords(default_title), _keywords(default_summary)
    if not t or not d:
        return 0.0
    inter = len(t & d)
    cov = max(inter / len(t), inter / len(d))
    jac = inter / len(t | d)
    sum_cov = len(t & (d | s)) / len(t)
    first = 0.05 if teacher_title.split() and default_title.split() and _keywords(teacher_title.split()[0]) == _keywords(default_title.split()[0]) else 0.0
    return round(min(1.0, 0.45 * cov + 0.35 * jac + 0.2 * sum_cov + first), 3)


MATCH_THRESHOLD = 0.45
CONFIRM_BELOW = 0.8


def merge_curriculum(subject: str, extracted: ExtractedTeacherContext | None, default: CurriculumSpec | None = None) -> MergeResult:
    """Combine the teacher's units with the default sequence. Teacher order and lengths win.

    Pairs are assigned globally, best score first, so one shared keyword cannot
    steal another unit's lessons. Matches below the confirmation bar are listed
    for the teacher. Default units the syllabus does not mention are kept in
    their own quarter (not appended at the end of the year) and listed.
    """
    default = default or DEFAULT_CURRICULA[subject]()
    if not extracted or not extracted.units:
        return MergeResult(default, notes=["No units found in the teacher's documents; using the district/LessonBridge default sequence."])
    tunits = extracted.units
    def pair_score(tu: UnitSpec, du: UnitSpec) -> float:
        score = match_score(tu.title, du.title, du.summary)
        if tu.quarter and du.quarter and abs(tu.quarter - du.quarter) >= 2:
            score -= 0.25  # a Q1 topic is unlikely to be the Q3 unit
        return round(score, 3)

    pairs = sorted(((pair_score(tu, du), i, j) for i, tu in enumerate(tunits) for j, du in enumerate(default.units)), reverse=True)
    t_to_d: dict[int, tuple[int, float]] = {}
    used_d: set[int] = set()
    for score, i, j in pairs:
        if score < MATCH_THRESHOLD:
            break
        if i in t_to_d or j in used_d:
            continue
        if tunits[i].from_weeks and score < CONFIRM_BELOW:
            continue  # weekly topics are granular; only a clear title match borrows a whole unit (LB-26)
        t_to_d[i] = (j, score)
        used_d.add(j)

    merged: list[UnitSpec] = []
    conflicts: list[str] = []
    notes: list[str] = []
    for i, tu in enumerate(tunits):
        if i in t_to_d:
            j, score = t_to_d[i]
            du = default.units[j]
            unit = du.model_copy(deep=True)
            unit.title = tu.title
            unit.match_confidence, unit.matched_default = score, du.title
            if tu.quarter and du.quarter and tu.quarter != du.quarter:
                conflicts.append(f"{subject}: the syllabus places '{tu.title}' in Q{tu.quarter}; the district default has it in Q{du.quarter}. Using the syllabus.")
            unit.quarter = tu.quarter or du.quarter
            if tu.planned_days is not None:  # a stated length always wins, including exactly 10 (LB-27)
                if tu.planned_days < len(unit.lessons):
                    conflicts.append(f"{subject}: '{tu.title}' is given {tu.planned_days} days but its lesson sequence has {len(unit.lessons)} lessons; the unit will run over.")
                unit.planned_days = tu.planned_days
            unit.source = "syllabus"
            if score < CONFIRM_BELOW:
                notes.append(f"{subject}: matched your unit '{tu.title}' to the default unit '{du.title}' (confidence {score:.2f}); confirm or replace its lessons.")
            merged.append(unit)
        else:
            unit = tu.model_copy(deep=True)
            unit.lessons = []
            unit.source = "syllabus"
            if unit.planned_days is None:
                unit.planned_days = 10
                notes.append(f"{subject}: '{tu.title}' has no stated length; planned as 10 days.")
            notes.append(f"{subject}: no lessons are known for '{tu.title}'; its {unit.planned_days} days are 'lesson to be detailed' days. Upload a pacing guide or fill them in.")
            merged.append(unit)
    default_pos = {du.title: j for j, du in enumerate(default.units)}
    for j, du in enumerate(default.units):
        if j in used_d:
            continue
        unit = du.model_copy(deep=True)
        unit.source = "default"
        # Keep it in its own quarter, in its default position relative to the units around it (LB-13).
        q = du.quarter or 4
        pos = len(merged)
        for k, m in enumerate(merged):
            mq = m.quarter or q
            mj = default_pos.get(m.matched_default) if m.matched_default else default_pos.get(m.title)
            if mq > q or (mq == q and mj is not None and mj > j):
                pos = k
                break
        merged.insert(pos, unit)
        notes.append(f"{subject}: the default unit '{du.title}' is not in your syllabus; it is kept in Q{du.quarter}. Remove it if you do not teach it.")
    last_q = 1
    for u in merged:
        if u.quarter is None:
            u.quarter = last_q
        last_q = u.quarter
        if u.planned_days is None:
            u.planned_days = max(10, len(u.lessons))
    off_subject = [u.title for u in tunits if score_subject(u.title).get(subject, 0) == 0 and max(score_subject(u.title).values()) > 0]
    if off_subject and len(off_subject) * 2 >= len(tunits):
        conflicts.append(f"{subject}: {len(off_subject)} of {len(tunits)} units in this document look like another subject ({', '.join(off_subject[:3])}…). Check the document's subject.")
    spec = CurriculumSpec(subject=subject, grade=default.grade, units=merged, source_notes=["Merged teacher syllabus with default sequence."])
    return MergeResult(spec, conflicts, notes)


# ---------------------------------------------------------------- persistence
def load_curriculum(session: Session, section: TeacherCourse, spec: CurriculumSpec, *, replace: bool = True) -> int:
    """Persist a CurriculumSpec as units/lessons/dependencies for a section. Returns lesson count.

    When the section already has calendar days, earlier units are kept inactive
    (completed days still reference them) instead of being deleted.
    """
    if replace:
        if section.calendar:
            for u in section.units:
                u.active = False
        else:
            for u in list(section.units):
                session.delete(u)
        session.flush()
    base_seq = max([u.sequence for u in section.units] + [0])
    slug_to_lesson: dict[str, Lesson] = {}
    count = 0
    for ui, u in enumerate(spec.units, start=1):
        unit = CurriculumUnit(teacher_course_id=section.id, sequence=base_seq + ui, slug=u.slug, title=u.title, summary=u.summary, standards=u.standards,
                              quarter=u.quarter, planned_days=u.planned_days if u.planned_days is not None else max(10, len(u.lessons)), source=u.source, active=True)
        session.add(unit)
        session.flush()
        for li, l in enumerate(u.lessons, start=1):
            lesson = Lesson(
                unit_id=unit.id, sequence=li, slug=l.slug, title=l.title, objective=l.objective,
                lesson_type=LessonType(l.lesson_type), duration_minutes=l.duration_minutes, minimum_viable_minutes=l.minimum_viable_minutes,
                priority=Priority(l.priority), delivery_requirement=l.delivery_requirement, materials=l.materials, student_output=l.student_output,
                standards=l.standards, required_components=l.required_components, optional_components=l.optional_components,
                can_move=l.can_move, quarter_boundary_allowed=l.quarter_boundary_allowed, hard_date=l.hard_date, notes=l.notes,
                origin="syllabus" if u.source == "syllabus" else "curriculum",
            )
            session.add(lesson)
            session.flush()
            slug_to_lesson[l.slug] = lesson
            count += 1
    for u in spec.units:
        for l in u.lessons:
            for pre in l.prerequisites:
                if pre in slug_to_lesson and l.slug in slug_to_lesson:
                    session.add(LessonDependency(lesson_id=slug_to_lesson[l.slug].id, depends_on_lesson_id=slug_to_lesson[pre].id, kind="before"))
    session.flush()
    session.refresh(section)
    return count


@dataclass
class Extraction:
    context: ExtractedTeacherContext
    method: str
    error: Optional[str] = None


def extract_teacher_context(text: str, subject: str, *, llm=None, doc_type: str = "syllabus") -> Extraction:
    """Use Claude when available, otherwise heuristics. Failures are logged and reported, never swallowed (LB-16)."""
    heur = heuristic_extract(text, subject, doc_type=doc_type)
    if llm is not None and llm.available():
        try:
            ctx = llm.extract_teacher_context(text, subject)
        except Exception as exc:  # noqa: BLE001 - reported to the teacher below
            log.warning("Claude extraction failed for %s: %s", subject, exc)
            return Extraction(heur, "heuristic", f"Claude extraction failed ({exc}); used the built-in parser.")
        if not ctx.units and heur.units:
            ctx.units = heur.units  # a reply without units is never treated as the teacher's curriculum
            return Extraction(ctx, "claude+heuristic", "Claude returned no units; units come from the built-in parser.")
        if not ctx.rules and heur.rules:
            ctx.rules = heur.rules
        return Extraction(ctx, "claude")
    return Extraction(heur, "heuristic")
