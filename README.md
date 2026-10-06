# LessonBridge

LessonBridge is an AI-assisted instructional continuity tool that reduces teacher workload by building and maintaining lesson calendars, preparing substitute plans, adapting instruction around absences, and intelligently reconciling schedule changes using district, curriculum, and teacher-specific context.

This repository contains the MVP described in [LESSONBRIDGE_BRAINSTORMING.md](LESSONBRIDGE_BRAINSTORMING.md): one district (Loudoun County Public Schools), one grade (7), two subjects (English and Civics), one teacher, one school year, with a planned extended leave as the primary validation scenario.

> **If LessonBridge can reliably find, infer, remember, reuse, or generate something itself, it should not ask the teacher to provide it.**

## What it does

| Workflow | What happens | Teacher touches |
|---|---|---|
| **Onboarding** | Loads LCPS public context (academic calendar, grading periods, Virginia SOL summaries, default curriculum sequences), reads the teacher's syllabus, pacing guide and procedures (Markdown, Word with tables, PDF, CSV, XLSX), extracts units and classroom rules, and merges them with the default sequence. Teacher order and stated lengths win; low-confidence matches and conflicts are listed for the teacher; units with no known lessons get "lesson to be detailed" days rather than invented lessons. Builds a full-year calendar per section. Running it again updates the same teacher. | about 8 |
| **Report an absence** | Per course-day, the decision engine picks **KEEP / MODIFY / REPLACE / POSTPONE / REORDER**. A lesson is kept for a day-to-day substitute only if nothing it depends on was displaced. The reconciler then repairs the calendar quarter by quarter and produces a **diff proposal**. Nothing changes until the teacher approves, and one approval covers all sections. | 2 |
| **Substitute plans** | One generation per course-day (shared by parallel sections of a course), each run through a quality gate. Failures are fed back and regenerated; the deterministic template is the fallback and passes the gate for every shipped lesson, both substitute types and periods from 25 to 90 minutes. | 1 |
| **Extended leave** | Pre-leave analysis (prerequisite chains and units that cross the leave start, work due during leave, pull-forward candidates, undetailed days, hard deadlines, the return date), drafted lessons for "to be detailed" days in the first ten school days, a handoff with ten detailed days and every classroom rule, weekly pacing frameworks, and a return brief limited to that absence. | 1 |
| **Progress, slips and edits** | Mark days completed or skipped; report that a lesson ran long; edit a day, fill a placeholder with your own lesson, change a substitute activity in a pending proposal; add documents after onboarding. | 1 each |

## What the planner guarantees

Every proposal is checked before it is saved, and the property-based test in `tests/test_planner_properties.py` drives random sequences of absences, slips, progress marks, out-of-order approvals, cancellations, undos and rebuilds through these checks:

- **No lesson is ever deleted.** When a quarter is full, the latest lessons carry into the next quarter. Anything that still has no day goes to the **owed-lesson list**, which every later plan and the "schedule owed lessons" action try to place. Optional content dropped to make room is kept on the same list.
- **Prerequisites come first**, including across substitute days of other absences.
- **Every day of an approved absence stays a substitute day**, whatever is planned later.
- **Completed and skipped days are never rewritten.**
- **An assessment never crosses a quarter end silently.** The proposal lists it under "Needs your decision" and cannot be approved without an explicit confirmation.
- **Outdated proposals are refused.** Each proposal row records the exact day it expects; approval checks every row against the live calendar and marks the proposal stale on any mismatch. Approving a proposal supersedes the section's overlapping pending proposals.
- **The diff shows every change.** Only days approval leaves untouched are collapsed, and the explanation is generated from what the diff actually does.
- **Plans follow the calendar.** A substitute plan whose day changes after it was generated is marked stale and can be regenerated.

Approved proposals can be undone through an undo proposal, and absences can be cancelled; both go through the same checks.

### Reconciliation example

A day-to-day substitute covers Monday of a week that ends with the Federalism Quiz on Thursday. A naive shift pushes the quiz across the quarter end. With the shipped Civics lessons and a 50-minute period, LessonBridge proposes:

| Day | Before | After approval |
|---|---|---|
| Mon | Introduction to federalism | Constitution review **[SUB]** |
| Tue | Federalism guided practice | Introduction to federalism + shortened Federalism guided practice |
| Wed | Federalism review | Federalism review |
| Thu | Federalism Quiz | Federalism Quiz (with the 15-minute pre-quiz study period) |

Compression keeps each lesson's required components and skips its optional ones. The scenario is `tests/test_audit_regressions.py::test_lb23_the_readme_federalism_example_compresses_with_shipped_lessons`.

The repair order inside each quarter segment is: absorb the nearest slack day (flex first, then "to be detailed" days), drop optional lessons nothing depends on, merge adjacent lessons on their core components, drop recommended lessons nothing depends on, then carry the latest lessons into the next quarter. Absences that cross a quarter end are repaired one quarter at a time, and each quarter end stays a hard constraint.

## Rules

Rules are typed in plain language and shown back with their interpretation ("15-minute study period before the assessment, on assessment days"). Common wordings of the pre-quiz study rule are recognised; a rule that mentions minutes but cannot be parsed is flagged for rewording. Rules can apply to all classes, one subject, one unit or one lesson. For routines such as hall passes or late work every rule at the most specific scope applies; for single-valued settings such as the study period, the newest rule wins and the older one is shown as superseded.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# End-to-end walkthrough in its own database (offline, no API key needed). It never deletes anything:
# the output folder must be new or empty, or pass --force to write into a new subfolder.
lessonbridge demo --out out/demo

# Web interface on this computer only
lessonbridge serve     # http://127.0.0.1:8000
```

Typical CLI session:

```bash
lessonbridge onboard examples/teacher_profile.yaml  # build (or update) the teacher's context
lessonbridge calendar events                        # provisional dates are flagged
lessonbridge calendar confirm                       # one touch confirms them all
lessonbridge calendar show --section 3 --start 2026-10-19 --end 2026-11-06

lessonbridge absence plan 2026-10-26                # decisions + reconciliation -> pending proposals
lessonbridge absence approve --all 1                # one approval for every section
lessonbridge absence subplans 1 --out out/plans     # one validated plan per course-day
lessonbridge absence brief 1                        # return brief for this absence only
lessonbridge absence undo 4                         # propose reverting an approved proposal
lessonbridge absence cancel 1                       # withdraw an absence

lessonbridge absence plan 2026-11-09 --end 2027-01-29 --substitute long_term_sub --reason "maternity leave"
lessonbridge absence approve --all 2 --acknowledge  # accept the decisions the proposals list
lessonbridge leave plan 2 --out out/leave           # analysis, drafted first days, handoff, weekly frameworks

lessonbridge progress slip 3 2026-12-01             # lesson ran long -> repair proposal
lessonbridge calendar fill 3 2026-12-03 --title "Debate prep" --objective "Prepare arguments for Friday's debate"
lessonbridge backlog list                           # owed lessons
lessonbridge documents add my_pacing_guide.xlsx --type teacher_pacing_guide --subject english
lessonbridge rules add "Before each quiz, give students fifteen minutes to study."
lessonbridge rules add "Use the unit word wall." --scope unit --target federalism-state-government
```

Sections are chosen by period (`--section 3`), exact name, or `id:N`; an ambiguous name is refused. When several teacher profiles exist, pass `--teacher-id`.

## Using Claude

Set `ANTHROPIC_API_KEY` (or log in with `ant auth login`) and LessonBridge uses Claude Opus 5.5 (`claude-opus-5-5`) through the official Anthropic SDK for substitute plans (structured output, validated, regenerated with feedback), syllabus and pacing-guide extraction, and drafting "to be detailed" days in a leave handoff. Server-side refusal fallbacks (`fallbacks: "default"`) are enabled.

Without usable credentials everything runs on the deterministic template generator, and any Claude failure (missing or rejected credentials, rate limits, refusals) falls back to the template for that one day. Model calls never run inside a database transaction, so the web interface stays responsive while a leave packet is generated. Extraction failures are reported in the onboarding result instead of being hidden.

| Variable | Default | Purpose |
|---|---|---|
| `LESSONBRIDGE_DATA_DIR` | `./data` | SQLite database and stored documents |
| `LESSONBRIDGE_DATABASE_URL` | SQLite in the data dir | Another database URL |
| `LESSONBRIDGE_MODEL` | `claude-opus-5-5` | Model for generation |
| `LESSONBRIDGE_EFFORT` | `medium` | Effort level for generation |
| `LESSONBRIDGE_GENERATOR` | `auto` | `auto`, `claude` or `template` |
| `LESSONBRIDGE_ALLOW_NETWORK` | `1` | Live retrieval of public sources. `0/false/no/off` disable it, `1/true/yes/on` enable it; anything else is an error. |

Existing databases are upgraded in place: new columns are added automatically on start.

## Web interface security

The web interface is meant for one teacher on their own computer. It still assumes the browser may visit hostile pages:

- Stored text (rules, titles, documents, model output) is rendered without raw HTML or `javascript:` links, under a Content Security Policy that allows no scripts.
- State-changing requests from other sites are refused (`Sec-Fetch-Site` / `Origin` checks), and only local `Host` headers are accepted.
- The onboarding form accepts documents only as uploads or from the `examples/` folder; it cannot read other files on the server.
- `lessonbridge serve --host <address>` on anything other than localhost prints a launch link with a token, and every request without that token is refused.
- Unknown ids and invalid input return 4xx pages.

## Architecture

```
lessonbridge/
  models.py              entities, plus the owed-lesson backlog, proposal before/after states and plan fingerprints
  db.py                  engine (WAL, busy timeout) and additive SQLite migration
  providers/             DistrictProvider interface; LoudounCountyProvider with snapshots and live refresh;
                         default sequences with lesson components; replacement library tagged by unit keywords
  ingestion/             versioned ingestion, document-order DOCX extraction, SQLite FTS5 search
  curriculum/interpret   markup-tolerant parsing (headings, lists, tables, week plans, dated quarters),
                         subject inference, whole-title global matching, curriculum versions
  curriculum/calendar    quarter-aware layout (assessment last in each unit), rebuild proposals, SOL-window notes
  curriculum/editing     day edits, filling placeholders, proposal edits, documents after onboarding
  profile/               onboarding (idempotent), rules engine with per-rule precedence, teacher-touch metric
  absence/engine.py      pure repair core: never deletes, carries overflow, prerequisite checks
  absence/decisions.py   KEEP / MODIFY / REPLACE / POSTPONE / REORDER
  absence/state.py       exact day states used for diffs, stale checks and plan fingerprints
  absence/service.py     planning from stored state, quarter segments, invariant gate, approval, undo, cancel, slips
  absence/leave.py       pre-leave analysis, drafting, handoff, weekly frameworks, return brief
  generation/            Claude client, prompts, template, quality gate, isolated per-day generation
  render/, cli.py, web/  Markdown renderers, Typer CLI, FastAPI review screens
```

Source precedence when documents disagree: teacher-confirmed > teacher syllabus or pacing guide > district curriculum > state standards > LessonBridge inference.

## Data caveats

- The Loudoun provider ships **provisional snapshots**: a 2026–2027 calendar assembled from LCPS's usual pattern and strand-level SOL summaries. Every snapshot date is flagged `needs_confirmation`; `lessonbridge calendar confirm` clears the flag in one touch. Live pages, when reachable, are indexed alongside the snapshot text, never instead of it.
- The default lesson sequences are LessonBridge inference. Days a unit has beyond its known lessons appear as "lesson to be detailed" days. The leave handoff drafts the ones in its first ten days and marks them for review.
- Early-release period lengths are not published in the snapshot. Set `early_release_minutes` per section in the profile; otherwise plans assume 60% of the period and onboarding says so.

## Development

```bash
pytest -q                                   # unit, service, regression, parsing-corpus, web and CLI tests
LB_PROPERTY_EXAMPLES=250 pytest -q tests/test_planner_properties.py   # longer property-based run
```

`tests/test_audit_regressions.py` has one test per finding of the independent code audit (LB-01 to LB-62) that can be expressed as a test.

## Not in the MVP

Multi-district support, generic scraping, rosters, accommodations, grades, LMS integration, parent communication, full curriculum generation, assessment generation and multi-teacher collaboration are explicitly deferred, as in the design document. The provider interface and the scoped data model are designed so none of these are blocked later.
