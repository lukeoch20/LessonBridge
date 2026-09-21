# LessonBridge Brainstorming

## Project summary

LessonBridge is an AI-assisted instructional continuity tool designed to reduce planning and administrative burden on teachers.

The core goal is simple:

> **If LessonBridge can reliably find, infer, remember, reuse, or generate something itself, it should not ask the teacher to provide it.**

Rather than functioning only as a “generate a substitute plan” tool, LessonBridge should maintain enough instructional context to understand what was supposed to happen, determine what should happen when instruction is disrupted, generate the appropriate substitute materials, and reconcile the instructional calendar afterward.

---

## MVP scope

The initial MVP will be intentionally narrow:

- **District:** Loudoun County Public Schools (LCPS)
- **Grade:** 7th grade
- **Subjects:** English and Civics
- **Primary user:** One teacher profile
- **School year:** One active school year
- **Primary first use case:** Planned extended maternity leave beginning approximately mid-to-late October
- **Secondary use cases:** One-day and short multi-day absences

The MVP should validate whether LessonBridge can:

1. Build an initial instructional context with minimal teacher effort.
2. Generate a useful instructional calendar.
3. Plan for an extended teacher absence.
4. Generate high-quality daily substitute plans.
5. Adjust the calendar intelligently when instruction is disrupted.
6. Maintain enough persistent context that the teacher is not repeatedly asked for the same information.

---

## Core product principles

### 1. Minimize teacher effort

Teacher effort should be treated as a scarce resource.

Design rules:

- Never ask for publicly discoverable information.
- Never ask twice for persistent information.
- Prefer confirmation over data entry.
- Prefer generated drafts over blank forms.
- Infer when confidence is high.
- Ask only when ambiguity materially affects the result.
- Preserve teacher corrections as future preferences.
- Keep routine workflows to as few interactions as possible.
- Make consequential changes visible before applying them.

A useful future product metric may be **teacher touches**: how many fields, clicks, corrections, or decisions are required to complete a workflow.

---

### 2. Public-first information gathering

During onboarding, LessonBridge should gather as much institutional context as possible before asking the teacher for documents.

For the LCPS MVP, LessonBridge should attempt to retrieve and store:

- Academic calendar
- Holidays and non-instructional days
- Teacher workdays
- Early-release days
- Quarter/semester boundaries
- Grading-period deadlines when publicly available
- State standards
- Public curriculum frameworks
- Public course descriptions
- Public pacing guides/curriculum maps when available
- Public assessment/testing calendars
- Previous-year public pacing or instructional documents when useful

The teacher should primarily be asked to provide teacher-specific documents such as:

- **Syllabus**
- **Teacher pacing guide**, if one exists

An existing lesson calendar may also be accepted as an optional shortcut.

---

### 3. Human control over instructional decisions

LessonBridge may recommend instructional changes, but the teacher remains the authority over what students learn and when.

Consequential schedule changes should be proposed as a clear diff before they become authoritative.

---

## First-run onboarding

The initial LessonBridge experience should create the teacher’s instructional context rather than assume one already exists.

### Minimal teacher inputs

For the LCPS MVP:

1. Select school.
2. Confirm grade level.
3. Select subjects.
4. Enter teaching schedule.
5. Upload syllabus.
6. Upload pacing guide if available.
7. Review the public information LessonBridge found.
8. Review the draft instructional calendar.

Because the MVP is LCPS-specific, district and state selection are not necessary.

---

## Persistent teacher profile

The teacher profile should represent instructional context, not just identity.

### Basic context

- School
- Grade
- Subjects
- Teaching schedule
- Course sections
- Planning periods

### Public institutional context

- LCPS academic calendar
- Grading periods
- Virginia standards
- Public curriculum information
- Assessment/testing dates
- Public pacing information

### Teacher-owned context

- Syllabus
- Pacing guide
- Classroom procedures
- Course-specific rules
- Teacher preferences

### LessonBridge-generated state

- Curriculum interpretation
- Unit sequence
- Instructional calendar
- Lesson dependencies
- Replacement activity library
- Teacher rules learned through confirmation
- Absence history
- Calendar changes

---

## Source and document architecture

LessonBridge should persist institutional source material rather than repeatedly retrieving it.

The same LCPS documents should not be duplicated separately for each teacher.

### Recommended conceptual layers

#### 1. Structured application database

A SQL database stores application state and relationships.

For local MVP development, **SQLite + SQLAlchemy** is likely sufficient while keeping migration to PostgreSQL straightforward.

Potential entities:

- teachers
- schools
- courses
- teacher_courses
- documents
- document_versions
- teacher_documents
- school_calendar_events
- curriculum_units
- lessons
- lesson_dependencies
- instructional_calendar
- teacher_preferences
- teacher_rules
- replacement_activities
- absence_events
- absence_decisions
- sub_plans

#### 2. Document storage

Original PDFs, Word files, spreadsheets, and other resources should live outside SQL.

Examples:

- LCPS calendar PDF
- Virginia standards
- Curriculum documents
- Teacher syllabus
- Teacher pacing guide

The database should store metadata such as:

- document ID
- source URL
- document type
- title
- version
- retrieval date
- checksum
- file location
- content type

#### 3. Search/retrieval layer

Stored documents should be parsed and searchable so LessonBridge can answer questions such as:

- What does the pacing guide expect during Quarter 2?
- What standards relate to argumentative writing?
- What dates are non-instructional?
- Is this assessment near the end of the quarter?

A vector database is not necessarily required for the MVP; a simpler indexed retrieval mechanism may be enough initially.

---

## LCPS source ingestion

The MVP does not need a generalized national district scraper.

Instead, implement an LCPS-specific source provider with a standardized interface that can later be generalized.

Conceptually:

```text
DistrictProvider

get_academic_calendar()
get_grading_periods()
get_standards(subject, grade)
get_curriculum(subject, grade)
get_pacing_guide(subject, grade)
```

Initial implementation:

```text
LoudounCountyProvider
```

Future implementations could add other districts without changing the rest of the application.

### Ingestion pipeline

```text
Source
  ↓
Discover
  ↓
Download
  ↓
Validate
  ↓
Extract
  ↓
Normalize metadata
  ↓
Store original
  ↓
Store parsed content
  ↓
Index
  ↓
Register version
```

Sources should be versioned instead of silently overwritten.

LessonBridge should also track:

- last checked
- last changed
- refresh interval
- checksum
- current version

Public sources should not be fetched every time the teacher opens the application.

---

## Source precedence

When sources conflict, LessonBridge should use an explicit hierarchy:

```text
Teacher-confirmed information
        ↓
Teacher syllabus / pacing guide
        ↓
School or district curriculum
        ↓
State standards/frameworks
        ↓
LessonBridge inference
```

LessonBridge should surface meaningful conflicts rather than silently resolving them.

---

## Instructional calendar

The system should separate the **curriculum sequence** from the **calendar mapping**.

Example:

```text
CURRICULUM

Lesson A
Lesson B
Lesson C
Quiz
```

Mapped to:

```text
CALENDAR

Oct 5 → Lesson A
Oct 6 → Lesson B
Oct 7 → Lesson C
Oct 8 → Review
Oct 9 → Quiz
```

This distinction allows LessonBridge to move lessons without changing the underlying curriculum sequence.

### Planning depth

The preferred model is:

- Generate the full school year at a high level.
- Generate upcoming weeks at higher resolution.
- Allow later dates to remain unit-level placeholders.
- Increase detail as dates approach.

---

## Lesson metadata

Lessons should contain enough structure for LessonBridge to reason about scheduling and substitute suitability.

Potential fields:

```yaml
subject: civics
unit: federalism
title: Introduction to Federalism
lesson_type: direct_instruction
duration_minutes: 45
minimum_viable_minutes: 25
priority: required

delivery_requirement:
  - regular_teacher
  - long_term_sub
  - any_sub
  - independent

prerequisites:
  - federal_state_powers_vocab

dependencies:
  before:
    - federalism_guided_practice
    - federalism_quiz

materials:
  - federalism_slides
  - guided_notes

student_output:
  - completed_guided_notes

can_move: true
quarter_boundary_allowed: false
```

A lesson may also distinguish between required and optional components so LessonBridge can compress instruction intelligently.

---

## Teacher-specific classroom rules

Classroom-specific rules should be first-class persistent data, not miscellaneous notes.

Example discovered during MVP brainstorming:

> On quiz days, students receive **15 minutes to study before the quiz**.

That rule should automatically apply to every relevant quiz-day plan unless explicitly overridden.

Potential rule categories:

- Assessment routines
- Homework expectations
- Early-finisher procedures
- Independent reading expectations
- Chromebook/device rules
- Bathroom/hall-pass procedures
- Group-work expectations
- Late-work procedures
- What substitutes may grade
- Assignment collection procedures
- Attendance procedures
- End-of-class cleanup

### Rule scope

Rules may apply at different levels:

```text
Specific lesson override
        ↓
Unit rule
        ↓
Course rule
        ↓
Teacher-wide classroom rule
        ↓
LessonBridge default
```

More specific rules override more general ones.

### Rules vs. preferences

A **rule** is mandatory:

> Students receive 15 minutes to study before quizzes.

A **preference** guides planning:

> Prefer independent reading for early finishers.

Rules should participate in both generation and validation.

---

## Absence planning

For each affected lesson, LessonBridge should choose among:

- **KEEP**
- **MODIFY**
- **REPLACE**
- **POSTPONE**
- **REORDER**

Decision inputs may include:

- Lesson type
- Delivery requirement
- Substitute type
- Absence duration
- Lesson dependencies
- Assessment dates
- Quarter deadlines
- Available replacement activities
- School calendar
- Teacher rules
- Teacher preferences

LessonBridge should produce a proposal rather than silently change the schedule.

---

## Short-term absence behavior

For a one-day or short absence, LessonBridge may decide that a teacher-dependent instructional lesson should be replaced with:

- Curriculum-preserving independent work
- Skill-maintenance work
- Emergency filler

Replacement should not automatically mean unrelated filler.

Potential English fallback activities:

- Grammar spiral review
- Independent reading response
- Editing practice
- Vocabulary
- Writing review

Potential Civics fallback activities:

- Constitution review
- Primary-source analysis
- Vocabulary review
- Current-events analysis
- Guided textbook reading

---

## Primary MVP use case: maternity leave

The first real-world validation case is a planned extended maternity leave beginning approximately mid-to-late October.

This should be treated differently from a one-day substitute scenario.

A long-term substitute can reasonably:

- Teach new material
- Administer assessments
- Manage multi-day assignments
- Advance through the curriculum

### Leave planning phases

#### 1. Pre-leave planning

LessonBridge should:

- Understand the expected leave window.
- Identify units and assessments likely to occur during leave.
- Flag sensitive instructional dependencies.
- Identify material that may be advantageous to complete before leave.
- Preserve hard deadlines.

#### 2. Initial handoff

When leave begins, LessonBridge generates:

- Detailed plans for the first 10 instructional days
- Materials and links
- Classroom rules
- Unit context
- Assessment guidance
- Substitute flexibility boundaries

#### 3. Ongoing leave

After the initial 10 instructional days, LessonBridge should transition to:

- Weekly pacing frameworks
- Unit objectives
- Required assessments
- Important deadlines
- Required standards/content
- Suggested sequencing
- Available materials
- Teacher-defined constraints

The long-term substitute should have increasing autonomy rather than receive months of daily scripted plans.

#### 4. Return reconciliation

LessonBridge should produce:

- What was completed
- What moved
- What was skipped
- Assessment status
- Outstanding work
- Recommended re-entry point
- Suggested first week back

---

## Atomic daily-plan generation

Detailed lesson-plan generation should be **atomic at the course-day level**.

LessonBridge may plan a multi-day sequence holistically, but each detailed day should be generated in its own LLM call.

Example:

```text
Two-week sequence planning
        ↓
Day 1 English → Generate → Validate → Store
Day 1 Civics  → Generate → Validate → Store
Day 2 English → Generate → Validate → Store
Day 2 Civics  → Generate → Validate → Store
...
```

For 10 instructional days and two subjects, this would generally mean approximately 20 primary generation calls.

### Why atomic generation matters

- Better generation quality
- Isolated retries
- Stable teacher edits
- Easier validation
- Easier regeneration when classroom progress changes
- Better cost/model control
- Reduced risk of later days becoming shallow or repetitive

The daily generation call should receive the relevant prior state, current objective, next-day dependency, materials, teacher rules, and class duration.

---

## Daily-plan validation

Every generated daily plan should pass a quality gate before being accepted.

Potential checks:

- Uses only available materials
- Matches curriculum sequence
- Respects school date and schedule
- Appropriate for substitute type
- Does not invent assignments
- Covers required objective
- Fits class duration
- Includes applicable classroom rules
- Respects lesson dependencies

Example:

```text
Quiz detected
        ↓
Teacher rule:
15-minute pre-quiz study period
        ↓
Generated plan omits review period
        ↓
FAIL
        ↓
Regenerate with explicit feedback
```

---

## Dynamic calendar reconciliation

A missed instructional day must **not** default to shifting every future lesson one day to the right.

LessonBridge should treat schedule repair as a constrained planning problem.

Possible actions:

- **Shift** — move the lesson when schedule slack exists
- **Merge** — combine essential content with another compatible lesson
- **Compress** — shorten related lessons while preserving core objectives
- **Replace** — remove a lower-priority activity
- **Defer** — move content into a later grading period when permitted
- **Drop** — remove optional/enrichment content
- **Preserve** — keep hard-date items unchanged

### Example

Original:

```text
Mon — Introduce Federalism
Tue — Guided Practice
Wed — Review
Thu — Quiz
Fri — Quarter Ends
```

Monday becomes a filler substitute day.

A simple shift would push the quiz beyond the quarter boundary.

A better repaired schedule may be:

```text
Mon — Filler activity [SUB]

Tue — Federalism introduction
      + shortened guided practice

Wed — Remaining guided practice
      + review

Thu — Quiz
      + 15-minute pre-quiz study period

Fri — Quarter Ends
```

The system should explain why it made the adjustment.

### Hard constraints

Examples:

- Quarter end
- Required assessment date
- State testing window
- Teacher return date
- Long-term substitute handoff
- Required project deadline

### Soft constraints

Examples:

- Prefer not to test on Monday
- Prefer review before a quiz
- Prefer new instruction earlier in the week
- Prefer one major assignment per day

The reconciliation engine should satisfy hard constraints first and optimize around softer preferences.

---

## Calendar diff and approval

After an absence, LessonBridge should show the teacher exactly what it proposes to change.

Example:

```text
ORIGINAL

Tue — Counterclaims
Wed — Guided Practice
Thu — Drafting
Fri — Peer Review

PROPOSED

Tue — Independent Writing Review [SUB]
Wed — Counterclaims
Thu — Guided Practice
Fri — Drafting
Mon — Peer Review
```

Or, when compression is necessary:

```text
Tue — Counterclaims + shortened guided practice
Wed — Remaining practice + review
Thu — Quiz
```

The teacher approves or edits the proposal before it becomes authoritative.

---

## Substitute plan structure

A generated substitute plan should be standardized and practical.

Potential structure:

```text
LESSONBRIDGE SUBSTITUTE PLAN

Date

Period / Course

Objective

Materials

Instructions

Student Deliverable

What to Collect

Early-Finisher Activity

Classroom-Specific Rules

Notes for Next Day
```

Persistent classroom information should be automatically inserted rather than repeatedly requested.

---

## Return-to-school brief

After an absence, LessonBridge should produce a concise teacher-facing reconciliation summary.

Example:

```text
RETURN BRIEF

English
Students completed argumentative-writing review.
Counterclaims instruction moved to Wednesday.

Civics
Federalism Quiz remained on schedule.

Calendar Changes
English sequence was compressed by one day.

Follow-Up
Collect review packets.
Resume English with Counterclaims.
```

For extended leave, this becomes a richer state-of-course report.

---

## Initial architecture

```text
                  LCPS PUBLIC SOURCES
                          │
                  LCPS Source Ingestion
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
       Document Storage         Structured Database
              │                       │
              └───────────┬───────────┘
                          ▼
                  LCPS Knowledge Base
                          │
                          ▼
                    Teacher Profile
                          │
             ┌────────────┴────────────┐
             ▼                         ▼
      Teacher Documents          Public Documents
             │                         │
             └────────────┬────────────┘
                          ▼
                Instructional Context
                          │
                          ▼
                 Calendar Generation
                          │
                          ▼
               Active Lesson Calendar
                          │
                    Absence Event
                          │
                          ▼
                Lesson Decision Engine
                          │
                          ▼
                 Calendar Reconciliation
                          │
                    Teacher Approval
                     /            \
                    ▼              ▼
            Updated Calendar    Sub Plans
                                   │
                                   ▼
                           Return/Re-entry Brief
```

---

## Explicit MVP non-goals

For now, defer:

- Multi-district support
- Nationwide district discovery
- Generic arbitrary web scraping
- Multi-state standards ingestion
- Student rosters
- Student-level accommodations
- Grades
- LMS integration
- Parent communication
- Full curriculum generation
- District administration
- Automatic substitute assignment
- AI-generated assessments as a core function
- Multi-teacher collaboration

The architecture should not prevent these later, but the MVP should not depend on them.

---

## MVP hypothesis

> **Given LCPS public instructional context plus a teacher’s syllabus and optional pacing guide, LessonBridge can create and maintain a useful instructional calendar, prepare a high-quality maternity-leave handoff, generate detailed substitute plans with minimal teacher effort, and intelligently reconcile the instructional schedule as reality changes.**

---

## Current high-value validation scenario

The first implementation should be tested against a real planned extended leave.

The key questions are:

1. Can LessonBridge accurately assemble the relevant LCPS context?
2. Can it produce a useful draft calendar from public context plus teacher documents?
3. Can it identify the likely instructional state at the start of leave?
4. Can it generate 10 high-quality course-day plans independently?
5. Can those plans reliably respect teacher-specific classroom rules?
6. Can it transition from daily plans to weekly long-term-substitute guidance?
7. Can it repair the calendar intelligently when days are lost or lessons take longer than expected?
8. Can it produce a useful return-to-school instructional state?
9. Can all of this happen with materially less teacher effort than preparing the leave manually?

---

## Working definition of success

LessonBridge MVP is successful when a teacher can provide a minimal amount of teacher-specific context, review rather than manually construct the initial instructional calendar, report or activate an absence, receive high-quality substitute planning appropriate to the duration of that absence, and return to a coherent instructional calendar without manually reconstructing what happened.

The product should ultimately feel less like an AI lesson-plan generator and more like a persistent instructional continuity assistant whose primary purpose is to give time back to teachers.
