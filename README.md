# LessonBridge

LessonBridge is an AI-assisted instructional continuity tool that reduces teacher workload by building and maintaining lesson calendars, preparing substitute plans, adapting instruction around absences, and intelligently reconciling schedule changes using district, curriculum, and teacher-specific context.

This repository contains the MVP described in [LESSONBRIDGE_BRAINSTORMING.md](LESSONBRIDGE_BRAINSTORMING.md): one district (Loudoun County Public Schools), one grade (7), two subjects (English and Civics), one teacher, one school year, with a planned extended leave as the primary validation scenario.

> **If LessonBridge can reliably find, infer, remember, reuse, or generate something itself, it should not ask the teacher to provide it.**

## What it does

| Workflow | What happens | Teacher touches |
|---|---|---|
| **Onboarding** | Loads LCPS public context (academic calendar, grading periods, Virginia SOL summaries, default curriculum sequences), ingests the teacher's syllabus / pacing guide / procedures, extracts units and classroom rules, merges them with the default sequence (teacher order wins, conflicts surfaced), and builds a full-year instructional calendar per section. | ~10 |
| **Report an absence** | Per affected course-day, the decision engine picks **KEEP / MODIFY / REPLACE / POSTPONE / REORDER** from lesson type, delivery requirement, substitute type, absence length, dependencies, assessment dates and the replacement library. The reconciler then repairs the calendar as a constrained planning problem (shift → merge/compress → drop optional → defer → unresolved) up to the next quarter boundary and produces a **diff proposal**. Nothing changes until the teacher approves. | 2 |
| **Substitute plans** | One generation call **per course-day**, each run through a quality gate (available materials only, fits class length exactly, covers the objective, right for the substitute type, no invented assignments, every applicable classroom rule included, timed routines such as a pre-quiz study period present). Failures are fed back and regenerated; a deterministic template is the final fallback so a substitute always has a usable plan. | 0 |
| **Extended leave** | Pre-leave analysis (units, assessments, sensitive dependencies, pull-forward candidates, hard deadlines, undetailed days), a handoff packet with 10 detailed days, weekly pacing frameworks for the rest of the leave, and a return brief. | 1 |
| **Progress & slips** | Mark days completed/skipped; report that a lesson ran long and get a repaired schedule that absorbs the nearest slack instead of shifting the whole year. | 1 |

Rules are first-class: typed in plain language ("On quiz days, students receive 15 minutes to study before the quiz."), parsed into a structured form, scoped (lesson > unit > course > teacher > default), inserted into every applicable plan and enforced by the validator.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

# End-to-end walkthrough with the sample teacher (works offline, no API key needed)
lessonbridge demo
ls out/demo/           # proposals, substitute plans, pre-leave analysis, handoff, weekly frameworks, return brief

# Web interface (review calendar, approve diffs, open plans, add rules, upload documents)
lessonbridge serve     # http://127.0.0.1:8000
```

Typical CLI session:

```bash
lessonbridge init                                   # database + LCPS public context
lessonbridge onboard examples/teacher_profile.yaml  # build the teacher's context
lessonbridge calendar events                        # review what was found; provisional dates are flagged
lessonbridge calendar confirm                       # one touch confirms them all
lessonbridge calendar show --section "Period 3" --start 2026-10-19 --end 2026-11-06

lessonbridge absence plan 2026-10-26                # decisions + reconciliation -> pending proposals
lessonbridge absence approve --all 1                # apply the diff
lessonbridge absence subplans 1 --out out/plans     # one validated plan per course-day
lessonbridge absence brief 1                        # return brief

lessonbridge absence plan 2026-11-09 --end 2027-01-29 --reason "maternity leave"
lessonbridge absence approve --all 2
lessonbridge leave plan 2 --out out/leave           # analysis, 10-day handoff, weekly frameworks

lessonbridge progress slip "Period 3" 2026-12-01    # lesson ran long -> repaired schedule proposal
lessonbridge rules add "Substitutes may not grade student work."
lessonbridge search "quarter end"
```

## Using Claude

Set `ANTHROPIC_API_KEY` (or log in with `ant auth login`) and LessonBridge uses Claude Opus 5.5 (`claude-opus-5-5`) through the official Anthropic SDK for:

- substitute-plan generation (structured JSON output, validated before it is stored, regenerated with explicit feedback on failure),
- curriculum/rule extraction from uploaded syllabi and pacing guides (falls back to heuristics).

Server-side refusal fallbacks (`fallbacks: "default"`) are enabled so a classifier false positive does not stall plan generation. Without credentials everything runs on the deterministic template generator, which is what the tests and `lessonbridge demo` use.

| Variable | Default | Purpose |
|---|---|---|
| `LESSONBRIDGE_DATA_DIR` | `./data` | SQLite database and stored documents |
| `LESSONBRIDGE_MODEL` | `claude-opus-5-5` | Model for generation |
| `LESSONBRIDGE_EFFORT` | `medium` | Effort level for generation |
| `LESSONBRIDGE_GENERATOR` | `auto` | `auto`, `claude` or `template` |
| `LESSONBRIDGE_ALLOW_NETWORK` | `1` | Try live retrieval of public sources |

## Architecture

```
lessonbridge/
  models.py              SQLAlchemy entities (teachers, sections, documents + versions, calendar events,
                         units, lessons, dependencies, instructional calendar, rules, preferences,
                         replacement activities, absences, decisions, proposals, sub plans, attempts,
                         leave plans, teacher touches)
  providers/             DistrictProvider interface; LoudounCountyProvider with bundled snapshots and
                         live-refresh; default curriculum sequences; replacement-activity library
  ingestion/             discover -> download -> validate -> extract -> normalize -> store original ->
                         store parsed -> index (SQLite FTS5) -> register version
  curriculum/            syllabus/pacing-guide interpretation and merge; calendar builder
                         (school days, quarter-aware slack allocation, curriculum -> date mapping)
  profile/               onboarding workflow, rules/preferences engine, teacher-touch metric
  absence/engine.py      pure planning core: Item/Slot/repair (shift, merge, compress, drop, defer,
                         preserve) with hard constraints and soft-constraint notes
  absence/decisions.py   KEEP / MODIFY / REPLACE / POSTPONE / REORDER per course-day
  absence/service.py     DB adapter: proposals, approval, slips, progress
  absence/leave.py       pre-leave analysis, handoff, weekly frameworks, return brief
  generation/            Claude client, prompts, template generator, validator, atomic per-day generation
  render/                Markdown renderers for plans, diffs, briefs, packets
  cli.py                 Typer CLI;  web/  FastAPI + Jinja review screens
```

Source precedence when documents disagree: teacher-confirmed > teacher syllabus/pacing guide > district curriculum > state standards > LessonBridge inference. Conflicts are reported at onboarding, not resolved silently.

### Reconciliation example (from the design document)

A substitute covers Monday of a week that ends the quarter with a federalism quiz on Thursday. A naive one-day shift pushes the quiz across the boundary. LessonBridge instead proposes:

| Date | Original | Proposed |
|---|---|---|
| Mon | Introduce Federalism | Constitution review **[SUB]** |
| Tue | Guided Practice | Introduce Federalism + shortened Guided Practice |
| Wed | Review | Review |
| Thu | Quiz | Quiz (with the 15-minute pre-quiz study period) |

This scenario is `tests/test_engine.py::test_lost_monday_compresses_instead_of_pushing_quiz_past_quarter_end`.

## Data caveats

- The container this was built in could not reach `lcps.org` or `doe.virginia.gov`, so the Loudoun provider ships **provisional snapshots**: a 2026–2027 calendar assembled from LCPS's usual pattern and strand-level SOL summaries. Every snapshot date is flagged `needs_confirmation` and shown to the teacher during onboarding; `lessonbridge calendar confirm` clears the flag in one touch. When the network allows, `lessonbridge init --force` retrieves the live pages, versions them and indexes their text.
- The default lesson sequences are LessonBridge inference. A real pacing guide replaces them; until then, days a unit allots beyond its known lessons appear as unit-level placeholders ("lesson to be detailed"), which the pre-leave analysis lists so the teacher can fill them before leave.

## Development

```bash
pytest -q          # 26 tests: planning core, decisions, rules, validator, ingestion, curriculum, end-to-end, LLM wrapper
```

## Not in the MVP

Multi-district support, generic scraping, rosters, accommodations, grades, LMS integration, parent communication, full curriculum generation, assessment generation and multi-teacher collaboration are explicitly deferred, as in the design document. The provider interface and the scoped data model are designed so none of these are blocked later.
