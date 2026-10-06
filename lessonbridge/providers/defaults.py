"""Default curriculum sequences used when the teacher provides no pacing guide.

These are *LessonBridge inference* (the lowest rung of the source precedence
ladder). Anything the teacher's syllabus or pacing guide says overrides them,
and the onboarding review marks them as inferred so the teacher can correct the
order with a drag rather than typing a plan from scratch.
"""
from __future__ import annotations

from ..schemas import CurriculumSpec, LessonSpec, UnitSpec

RT = ["regular_teacher", "long_term_sub"]
ANY = ["regular_teacher", "long_term_sub", "any_sub"]
IND = ["regular_teacher", "long_term_sub", "any_sub", "independent"]


# Core (required) and optional parts per lesson type. Compression keeps the core and skips the optional
# parts, so two lessons can share one class period when their cores fit (LB-23).
COMPONENTS = {
    "direct_instruction": (["mini-lesson on the key concept", "check for understanding"], ["extended modeling", "exit ticket"], 25),
    "guided_practice": (["guided practice on the core task"], ["extension problems", "whole-class debrief"], 20),
    "independent_practice": (["independent practice set"], ["extension work", "partner check"], 20),
    "review": (["targeted review of key ideas"], ["review game", "extra practice items"], 15),
    "discussion": (["structured discussion on the core question"], ["extended debate", "written reflection"], 25),
    "writing_workshop": (["focused writing time"], ["conferencing", "sharing"], 25),
    "reading": (["reading time"], ["response discussion"], 25),
    "project": (["project work time"], ["presentations"], 30),
    "enrichment": ([], ["enrichment activity"], 15),
}


def _l(slug, title, objective, lesson_type="direct_instruction", delivery=RT, priority="required", prereqs=(), materials=(), output=(), standards=(), mins=50, mvm=None, can_move=True, optional_components=None, required_components=None):
    req, opt, core = COMPONENTS.get(lesson_type, ([], [], 30))
    return LessonSpec(
        slug=slug, title=title, objective=objective, lesson_type=lesson_type,
        delivery_requirement=list(delivery), priority=priority, prerequisites=list(prereqs),
        materials=list(materials), student_output=list(output), standards=list(standards),
        duration_minutes=mins, minimum_viable_minutes=mvm if mvm is not None else min(core, mins), can_move=can_move,
        optional_components=list(optional_components if optional_components is not None else opt),
        required_components=list(required_components if required_components is not None else req),
    )


def _quiz(slug, title, prereqs, standards, mins=50):
    return LessonSpec(
        slug=slug, title=title, objective=f"Demonstrate mastery: {title}", lesson_type="assessment",
        delivery_requirement=list(ANY), priority="required", prerequisites=list(prereqs), standards=list(standards),
        materials=[f"{slug}_form"], student_output=[f"completed {title.lower()}"], duration_minutes=mins,
        minimum_viable_minutes=mins, can_move=True, quarter_boundary_allowed=False,
        required_components=["assessment administered"],
    )


def english_7() -> CurriculumSpec:
    units = [
        UnitSpec(slug="launch-reading-writing", title="Launching Readers and Writers", quarter=1, planned_days=8, standards=["7.1", "7.3", "7.4"],
                 summary="Classroom routines, independent reading launch, reading response, vocabulary notebooks.",
                 lessons=[
                     _l("launch-routines", "Classroom routines and reading life", "Establish reading and writing routines; set independent reading goals.", "discussion", ANY, standards=["7.1"], materials=["reading_survey", "goal_sheet"], output=["reading goal sheet"]),
                     _l("launch-annotation", "Active reading and annotation", "Annotate a short text for key ideas and reactions.", "guided_practice", ANY, prereqs=["launch-routines"], standards=["7.4"], materials=["annotation_guide", "short_text_1"], output=["annotated text"]),
                     _l("launch-vocab-routine", "Vocabulary notebook and context clues", "Use context clues and word parts to determine meaning.", "direct_instruction", ANY, standards=["7.3"], materials=["vocab_notebook_template"], output=["vocab notebook entries"]),
                     _l("launch-response", "Reading response paragraphs", "Write a focused response paragraph with text evidence.", "writing_workshop", RT, prereqs=["launch-annotation"], standards=["7.4", "7.6"], materials=["response_frames"], output=["response paragraph"]),
                     _l("launch-discussion", "Academic discussion norms", "Practice accountable talk in small-group discussion.", "discussion", RT, prereqs=["launch-routines"], standards=["7.1"], materials=["discussion_stems"], output=["discussion notes"]),
                     _quiz("launch-vocab-quiz", "Vocabulary Quiz 1", ["launch-vocab-routine"], ["7.3"], mins=30),
                 ]),
        UnitSpec(slug="short-fiction", title="Short Fiction: Plot, Conflict and Character", quarter=1, planned_days=18, standards=["7.4", "7.3", "7.1"],
                 summary="Plot structure, internal/external conflict, characterization, point of view and theme through short stories.",
                 lessons=[
                     _l("fiction-plot-structure", "Plot structure and conflict", "Identify exposition, rising action, climax, falling action, resolution and the central conflict.", "direct_instruction", RT, standards=["7.4"], materials=["plot_diagram", "short_story_1"], output=["completed plot diagram"]),
                     _l("fiction-plot-practice", "Plot practice with a second story", "Apply plot and conflict analysis independently.", "independent_practice", ANY, prereqs=["fiction-plot-structure"], standards=["7.4"], materials=["short_story_2", "plot_diagram"], output=["plot diagram"]),
                     _l("fiction-characterization", "Direct and indirect characterization", "Infer character traits from STEAL evidence.", "direct_instruction", RT, prereqs=["fiction-plot-structure"], standards=["7.4"], materials=["steal_chart", "short_story_2"], output=["characterization chart"]),
                     _l("fiction-pov", "Point of view", "Distinguish first, third limited and third omniscient point of view and its effect.", "direct_instruction", RT, prereqs=["fiction-characterization"], standards=["7.4"], materials=["pov_slides", "pov_passages"], output=["POV exit ticket"]),
                     _l("fiction-inference", "Making inferences", "Draw and support inferences with text evidence.", "guided_practice", ANY, prereqs=["fiction-characterization"], standards=["7.4"], materials=["inference_passages"], output=["inference chart"]),
                     _l("fiction-theme", "Theme vs. topic", "Determine theme and trace its development.", "direct_instruction", RT, prereqs=["fiction-inference"], standards=["7.4"], materials=["theme_slides", "short_story_3"], output=["theme statement"]),
                     _l("fiction-figurative", "Figurative language and connotation", "Interpret simile, metaphor, personification and connotation in context.", "guided_practice", ANY, prereqs=["fiction-theme"], standards=["7.3", "7.4"], materials=["fig_lang_stations"], output=["station responses"]),
                     _l("fiction-socratic", "Socratic seminar on short stories", "Discuss theme and character choices using evidence.", "discussion", RT, prereqs=["fiction-theme"], standards=["7.1", "7.4"], materials=["seminar_questions"], output=["seminar prep sheet"], priority="recommended"),
                     _l("fiction-review", "Short fiction review", "Review plot, character, POV, inference and theme.", "review", ANY, prereqs=["fiction-figurative"], standards=["7.4"], materials=["review_packet"], output=["review packet"]),
                     _quiz("fiction-test", "Short Fiction Unit Test", ["fiction-review"], ["7.4", "7.3"]),
                 ]),
        UnitSpec(slug="narrative-writing", title="Narrative Writing", quarter=1, planned_days=12, standards=["7.6", "7.7"],
                 summary="Personal or fictional narrative: narrative arc, dialogue, sensory detail, revision and editing.",
                 lessons=[
                     _l("narr-mentor-texts", "Mentor text study", "Identify narrative techniques in mentor texts.", "guided_practice", ANY, standards=["7.6", "7.4"], materials=["mentor_texts"], output=["technique tracker"]),
                     _l("narr-planning", "Planning a narrative", "Plan a narrative arc with a clear conflict.", "writing_workshop", RT, prereqs=["narr-mentor-texts"], standards=["7.6"], materials=["narrative_planner"], output=["narrative plan"]),
                     _l("narr-drafting-1", "Drafting: opening and rising action", "Draft an engaging opening and rising action.", "writing_workshop", ANY, prereqs=["narr-planning"], standards=["7.6"], materials=["drafting_checklist"], output=["draft pages"]),
                     _l("narr-dialogue", "Dialogue and punctuation", "Punctuate and format dialogue correctly.", "direct_instruction", RT, prereqs=["narr-drafting-1"], standards=["7.7"], materials=["dialogue_slides", "dialogue_practice"], output=["dialogue practice sheet"]),
                     _l("narr-drafting-2", "Drafting: climax and resolution", "Complete the draft with a resolution that reflects the conflict.", "writing_workshop", ANY, prereqs=["narr-dialogue"], standards=["7.6"], materials=["drafting_checklist"], output=["complete draft"]),
                     _l("narr-revision", "Revision: show don't tell", "Revise for sensory detail and pacing.", "writing_workshop", RT, prereqs=["narr-drafting-2"], standards=["7.6"], materials=["revision_stations"], output=["revised draft"]),
                     _l("narr-peer-editing", "Peer editing", "Peer-edit for conventions using the editing checklist.", "guided_practice", ANY, prereqs=["narr-revision"], standards=["7.7"], materials=["editing_checklist"], output=["peer edit form"]),
                     _l("narr-publish", "Final narrative due", "Publish the final narrative.", "assessment", ANY, prereqs=["narr-peer-editing"], standards=["7.6", "7.7"], materials=["submission_instructions"], output=["final narrative"], can_move=True),
                 ]),
        UnitSpec(slug="nonfiction", title="Nonfiction: Structure, Purpose and Main Idea", quarter=2, planned_days=15, standards=["7.5", "7.3"],
                 summary="Text structures and features, main idea, author's purpose and viewpoint, fact and opinion.",
                 lessons=[
                     _l("nf-text-features", "Text features and structures", "Identify text features and five organizational structures.", "direct_instruction", RT, standards=["7.5"], materials=["text_structure_slides", "nf_article_1"], output=["structure graphic organizer"]),
                     _l("nf-main-idea", "Main idea and supporting details", "Determine main idea and distinguish supporting details.", "guided_practice", ANY, prereqs=["nf-text-features"], standards=["7.5"], materials=["nf_article_2", "main_idea_organizer"], output=["main idea organizer"]),
                     _l("nf-summary", "Objective summary", "Write an objective summary of a nonfiction text.", "writing_workshop", ANY, prereqs=["nf-main-idea"], standards=["7.5", "7.6"], materials=["summary_frame"], output=["summary paragraph"]),
                     _l("nf-authors-purpose", "Author's purpose and viewpoint", "Analyze author's purpose, viewpoint and word choice.", "direct_instruction", RT, prereqs=["nf-main-idea"], standards=["7.5"], materials=["purpose_slides", "paired_articles"], output=["purpose analysis"]),
                     _l("nf-fact-opinion", "Fact, opinion and bias", "Distinguish fact from opinion and identify bias.", "guided_practice", ANY, prereqs=["nf-authors-purpose"], standards=["7.5"], materials=["fact_opinion_sort"], output=["sort and justification"]),
                     _l("nf-paired-texts", "Comparing paired texts", "Compare two texts on the same topic.", "guided_practice", RT, prereqs=["nf-fact-opinion"], standards=["7.5"], materials=["paired_articles", "venn_organizer"], output=["comparison chart"]),
                     _l("nf-review", "Nonfiction review", "Review nonfiction skills.", "review", ANY, prereqs=["nf-paired-texts"], standards=["7.5"], materials=["nf_review_packet"], output=["review packet"]),
                     _quiz("nf-test", "Nonfiction Unit Test", ["nf-review"], ["7.5"]),
                 ]),
        UnitSpec(slug="argumentative-writing", title="Argumentative Writing", quarter=2, planned_days=16, standards=["7.6", "7.7", "7.8"],
                 summary="Claims, evidence, reasoning, counterclaims and rebuttal; drafting and revising an argumentative essay.",
                 lessons=[
                     _l("arg-claims-evidence", "Claims, evidence and reasoning", "Distinguish claim, evidence and reasoning.", "direct_instruction", RT, standards=["7.6"], materials=["cer_slides", "sample_arguments"], output=["CER sort"]),
                     _l("arg-evaluating-evidence", "Evaluating evidence", "Evaluate the relevance and credibility of evidence.", "guided_practice", ANY, prereqs=["arg-claims-evidence"], standards=["7.6", "7.8"], materials=["evidence_cards"], output=["evidence ranking"]),
                     _l("arg-thesis", "Writing a thesis", "Write a clear, arguable thesis.", "writing_workshop", RT, prereqs=["arg-claims-evidence"], standards=["7.6"], materials=["thesis_frames"], output=["thesis statements"]),
                     _l("arg-counterclaims", "Counterclaims and rebuttal", "Acknowledge and rebut a counterclaim.", "direct_instruction", RT, prereqs=["arg-thesis"], standards=["7.6"], materials=["counterclaim_slides", "counterclaim_practice"], output=["counterclaim paragraph"]),
                     _l("arg-guided-practice", "Guided practice: body paragraphs", "Draft body paragraphs using CER with transitions.", "guided_practice", ANY, prereqs=["arg-counterclaims"], standards=["7.6"], materials=["body_paragraph_frames"], output=["two body paragraphs"]),
                     _l("arg-drafting", "Drafting the essay", "Draft a complete argumentative essay.", "writing_workshop", ANY, prereqs=["arg-guided-practice"], standards=["7.6"], materials=["essay_checklist"], output=["full draft"]),
                     _l("arg-peer-review", "Peer review", "Give and receive feedback on argument and organization.", "guided_practice", RT, prereqs=["arg-drafting"], standards=["7.6", "7.7"], materials=["peer_review_form"], output=["peer review form"]),
                     _l("arg-revision", "Revision and editing", "Revise for elaboration and edit for conventions.", "writing_workshop", ANY, prereqs=["arg-peer-review"], standards=["7.7"], materials=["editing_checklist"], output=["revised essay"]),
                     _l("arg-essay-due", "Argumentative essay due", "Submit the final argumentative essay.", "assessment", ANY, prereqs=["arg-revision"], standards=["7.6", "7.7"], materials=["submission_instructions"], output=["final essay"]),
                 ]),
        UnitSpec(slug="novel-study", title="Novel Study: Theme and Character Development", quarter=3, planned_days=22, standards=["7.4", "7.1", "7.3"],
                 summary="Whole-class novel: character change, theme development, text evidence, literary discussion.",
                 lessons=[
                     _l("novel-launch", "Novel launch and background", "Build background and set reading schedule.", "direct_instruction", RT, standards=["7.4"], materials=["novel_class_set", "reading_schedule"], output=["anticipation guide"]),
                     _l("novel-reading-1", "Reading and response: part 1", "Read and respond with text evidence.", "reading", IND, prereqs=["novel-launch"], standards=["7.4"], materials=["novel_class_set", "response_journal"], output=["journal entries"]),
                     _l("novel-character-change", "Tracking character change", "Analyze how characters change in response to events.", "direct_instruction", RT, prereqs=["novel-reading-1"], standards=["7.4"], materials=["character_tracker"], output=["character tracker"]),
                     _l("novel-reading-2", "Reading and response: part 2", "Read and respond with text evidence.", "reading", IND, prereqs=["novel-character-change"], standards=["7.4"], materials=["novel_class_set", "response_journal"], output=["journal entries"]),
                     _l("novel-theme-development", "Theme development", "Trace how theme develops across the novel.", "guided_practice", RT, prereqs=["novel-reading-2"], standards=["7.4"], materials=["theme_tracker"], output=["theme tracker"]),
                     _l("novel-reading-3", "Reading and response: part 3", "Finish the novel and respond.", "reading", IND, prereqs=["novel-theme-development"], standards=["7.4"], materials=["novel_class_set", "response_journal"], output=["journal entries"]),
                     _l("novel-seminar", "Socratic seminar", "Discuss theme and author's choices with evidence.", "discussion", RT, prereqs=["novel-reading-3"], standards=["7.1", "7.4"], materials=["seminar_questions"], output=["seminar reflection"]),
                     _l("novel-essay", "Literary analysis paragraph", "Write an analysis paragraph on theme or character.", "writing_workshop", ANY, prereqs=["novel-seminar"], standards=["7.4", "7.6"], materials=["analysis_frame"], output=["analysis paragraph"]),
                     _quiz("novel-test", "Novel Test", ["novel-reading-3"], ["7.4"]),
                 ]),
        UnitSpec(slug="research", title="Research and Source Evaluation", quarter=3, planned_days=12, standards=["7.8", "7.6"],
                 summary="Research question, evaluating sources, note-taking, citation and a short research product.",
                 lessons=[
                     _l("research-question", "Developing a research question", "Narrow a topic into a researchable question.", "direct_instruction", RT, standards=["7.8"], materials=["question_funnel"], output=["research question"]),
                     _l("research-evaluating-sources", "Evaluating sources", "Evaluate credibility, accuracy and relevance.", "guided_practice", ANY, prereqs=["research-question"], standards=["7.8"], materials=["source_evaluation_checklist", "sample_sources"], output=["source evaluation"]),
                     _l("research-notes", "Note-taking and paraphrasing", "Paraphrase and record notes with source information.", "guided_practice", ANY, prereqs=["research-evaluating-sources"], standards=["7.8"], materials=["note_cards"], output=["note cards"]),
                     _l("research-citation", "Citing sources", "Create MLA citations and avoid plagiarism.", "direct_instruction", RT, prereqs=["research-notes"], standards=["7.8"], materials=["citation_guide"], output=["works cited draft"]),
                     _l("research-drafting", "Drafting the research product", "Draft the research product.", "writing_workshop", ANY, prereqs=["research-citation"], standards=["7.6", "7.8"], materials=["product_checklist"], output=["draft"]),
                     _l("research-due", "Research product due", "Submit the research product.", "assessment", ANY, prereqs=["research-drafting"], standards=["7.8"], materials=["submission_instructions"], output=["final product"]),
                 ]),
        UnitSpec(slug="poetry-media", title="Poetry and Media Literacy", quarter=4, planned_days=12, standards=["7.4", "7.2", "7.3"],
                 summary="Poetic devices and structure; analyzing media messages and persuasive techniques.",
                 lessons=[
                     _l("poetry-devices", "Poetic devices", "Identify sound devices and figurative language in poems.", "direct_instruction", RT, standards=["7.4", "7.3"], materials=["poetry_packet"], output=["device chart"]),
                     _l("poetry-structure", "Poetic form and structure", "Analyze how form contributes to meaning.", "guided_practice", ANY, prereqs=["poetry-devices"], standards=["7.4"], materials=["poetry_packet"], output=["TPCASTT analysis"]),
                     _l("media-techniques", "Persuasive techniques in media", "Identify persuasive techniques in advertisements.", "direct_instruction", RT, standards=["7.2"], materials=["media_slides", "ad_examples"], output=["technique tracker"]),
                     _l("media-analysis", "Media message analysis", "Analyze purpose, audience and message of a media text.", "guided_practice", ANY, prereqs=["media-techniques"], standards=["7.2"], materials=["media_analysis_frame"], output=["analysis frame"]),
                     _quiz("poetry-media-quiz", "Poetry and Media Quiz", ["poetry-structure", "media-analysis"], ["7.2", "7.4"], mins=40),
                 ]),
        UnitSpec(slug="sol-review", title="Reading SOL Review and Reflective Writing", quarter=4, planned_days=14, standards=["7.4", "7.5", "7.3", "7.6"],
                 summary="Spiral review of reading skills before the Reading 7 SOL; reflective writing to close the year.",
                 lessons=[
                     _l("sol-fiction-review", "Fiction skills review", "Review fiction skills with released items.", "review", ANY, standards=["7.4"], materials=["released_items_fiction"], output=["practice set"]),
                     _l("sol-nonfiction-review", "Nonfiction skills review", "Review nonfiction skills with released items.", "review", ANY, standards=["7.5"], materials=["released_items_nonfiction"], output=["practice set"]),
                     _l("sol-vocab-review", "Vocabulary review", "Review word analysis and context strategies.", "review", IND, standards=["7.3"], materials=["vocab_review_set"], output=["practice set"]),
                     _l("sol-test-strategies", "Test strategies", "Practice pacing and strategy on a mixed set.", "review", ANY, prereqs=["sol-fiction-review", "sol-nonfiction-review"], standards=["7.4", "7.5"], materials=["mixed_practice"], output=["practice set"]),
                     _l("reflective-writing", "Reflective writing", "Write a reflective piece on growth as a reader and writer.", "writing_workshop", ANY, standards=["7.6"], materials=["reflection_prompt"], output=["reflection"], priority="recommended"),
                 ]),
    ]
    return CurriculumSpec(subject="english", grade=7, units=units, source_notes=["LessonBridge default sequence (inference). Replace with the teacher's pacing guide when available."])


def civics_7() -> CurriculumSpec:
    units = [
        UnitSpec(slug="civics-skills", title="Civics Skills and Foundations of Government", quarter=1, planned_days=12, standards=["CE.1", "CE.2"],
                 summary="Analyzing sources and political cartoons; purposes of government; fundamental political principles.",
                 lessons=[
                     _l("skills-sources", "Analyzing primary and secondary sources", "Distinguish primary from secondary sources and analyze them.", "direct_instruction", ANY, standards=["CE.1"], materials=["source_analysis_sheet", "sample_sources"], output=["source analysis sheet"]),
                     _l("skills-cartoons", "Political cartoons", "Interpret symbolism, exaggeration and labeling in political cartoons.", "guided_practice", ANY, prereqs=["skills-sources"], standards=["CE.1"], materials=["cartoon_set"], output=["cartoon analysis"]),
                     _l("found-purposes", "Purposes of government", "Explain the purposes of government and the social contract.", "direct_instruction", RT, standards=["CE.2"], materials=["purposes_slides"], output=["guided notes"]),
                     _l("found-principles", "Fundamental political principles", "Define consent of the governed, limited government, rule of law, democracy, representative government.", "direct_instruction", RT, prereqs=["found-purposes"], standards=["CE.2"], materials=["principles_slides", "principles_sort"], output=["principles sort"]),
                     _l("found-principles-practice", "Principles in action", "Match scenarios to political principles.", "independent_practice", ANY, prereqs=["found-principles"], standards=["CE.2"], materials=["scenario_cards"], output=["scenario matching"]),
                     _quiz("found-principles-quiz", "Principles of Government Quiz", ["found-principles-practice"], ["CE.1", "CE.2"], mins=35),
                 ]),
        UnitSpec(slug="founding-documents", title="Founding Documents", quarter=1, planned_days=14, standards=["CE.2"],
                 summary="Charters of the Virginia Company, Virginia Declaration of Rights, Declaration of Independence, Articles of Confederation, Virginia Statute for Religious Freedom, Constitution and Bill of Rights.",
                 lessons=[
                     _l("docs-charters-vdr", "Virginia charters and the Virginia Declaration of Rights", "Explain the influence of the charters and the Virginia Declaration of Rights.", "direct_instruction", RT, standards=["CE.2"], materials=["docs_slides", "docs_chart"], output=["documents chart"]),
                     _l("docs-declaration", "Declaration of Independence", "Analyze the Declaration's key ideas and grievances.", "guided_practice", ANY, prereqs=["docs-charters-vdr"], standards=["CE.2"], materials=["declaration_excerpt", "close_reading_guide"], output=["close reading guide"]),
                     _l("docs-articles", "Articles of Confederation", "Explain the weaknesses of the Articles.", "direct_instruction", RT, prereqs=["docs-declaration"], standards=["CE.2"], materials=["articles_slides"], output=["weaknesses organizer"]),
                     _l("docs-statute", "Virginia Statute for Religious Freedom", "Explain the Statute's influence on the First Amendment.", "direct_instruction", ANY, prereqs=["docs-articles"], standards=["CE.2"], materials=["statute_excerpt"], output=["exit ticket"]),
                     _l("docs-constitution", "The Constitution and the Preamble", "Explain the Preamble and the structure of the Constitution.", "direct_instruction", RT, prereqs=["docs-articles"], standards=["CE.2"], materials=["constitution_slides", "preamble_activity"], output=["preamble activity"]),
                     _l("docs-bill-of-rights", "The Bill of Rights", "Summarize the rights protected by the first ten amendments.", "guided_practice", ANY, prereqs=["docs-constitution"], standards=["CE.2"], materials=["bill_of_rights_stations"], output=["station responses"]),
                     _l("docs-amendments", "Amending the Constitution", "Explain the amendment process and key later amendments.", "direct_instruction", RT, prereqs=["docs-bill-of-rights"], standards=["CE.2"], materials=["amendments_slides"], output=["guided notes"]),
                     _l("docs-review", "Founding documents review", "Review the founding documents.", "review", ANY, prereqs=["docs-amendments"], standards=["CE.2"], materials=["docs_review_packet"], output=["review packet"]),
                     _quiz("docs-test", "Founding Documents Test", ["docs-review"], ["CE.2"]),
                 ]),
        UnitSpec(slug="citizenship", title="Citizenship and Civic Participation", quarter=1, planned_days=10, standards=["CE.3", "CE.4"],
                 summary="How citizenship is attained; First Amendment freedoms; duties, responsibilities and character traits of citizens.",
                 lessons=[
                     _l("cit-attaining", "Becoming a citizen", "Explain citizenship by birth and naturalization.", "direct_instruction", RT, standards=["CE.3"], materials=["citizenship_slides"], output=["guided notes"]),
                     _l("cit-first-amendment", "First Amendment freedoms", "Explain the five First Amendment freedoms with examples.", "guided_practice", ANY, prereqs=["cit-attaining"], standards=["CE.3"], materials=["first_amendment_scenarios"], output=["scenario sort"]),
                     _l("cit-duties-responsibilities", "Duties vs. responsibilities", "Distinguish duties from responsibilities of citizens.", "direct_instruction", ANY, prereqs=["cit-attaining"], standards=["CE.3"], materials=["duties_sort"], output=["duties sort"]),
                     _l("cit-character-traits", "Character traits for civic life", "Identify character traits that support civic life.", "discussion", ANY, standards=["CE.4"], materials=["traits_scenarios"], output=["trait reflection"]),
                     _l("cit-service-project", "Civic participation project plan", "Plan a community service or civic action.", "project", RT, prereqs=["cit-duties-responsibilities"], standards=["CE.3", "CE.4"], materials=["project_planner"], output=["project plan"], priority="recommended"),
                     _quiz("cit-quiz", "Citizenship Quiz", ["cit-first-amendment", "cit-duties-responsibilities"], ["CE.3", "CE.4"], mins=40),
                 ]),
        UnitSpec(slug="political-process", title="The Political Process", quarter=2, planned_days=12, standards=["CE.5"],
                 summary="Political parties, the media, campaign finance, voting and the Electoral College.",
                 lessons=[
                     _l("pol-parties", "Functions of political parties", "Explain the functions of political parties.", "direct_instruction", RT, standards=["CE.5"], materials=["parties_slides"], output=["guided notes"]),
                     _l("pol-media", "The role of the media", "Evaluate how the media influences public opinion.", "guided_practice", ANY, prereqs=["pol-parties"], standards=["CE.5"], materials=["media_examples"], output=["media analysis"]),
                     _l("pol-campaigns", "Campaigns and campaign finance", "Explain campaign costs and finance rules.", "direct_instruction", RT, prereqs=["pol-parties"], standards=["CE.5"], materials=["campaign_slides"], output=["guided notes"]),
                     _l("pol-voting", "Voter registration and participation", "Explain registration requirements and factors in voter turnout.", "direct_instruction", ANY, prereqs=["pol-campaigns"], standards=["CE.5"], materials=["voting_slides", "turnout_data"], output=["data analysis"]),
                     _l("pol-electoral-college", "The Electoral College", "Explain how the Electoral College works.", "guided_practice", RT, prereqs=["pol-voting"], standards=["CE.5"], materials=["electoral_map_activity"], output=["electoral map"]),
                     _quiz("pol-test", "Political Process Test", ["pol-electoral-college"], ["CE.5"]),
                 ]),
        UnitSpec(slug="national-government", title="The National Government", quarter=2, planned_days=16, standards=["CE.6"],
                 summary="Three branches, separation of powers, checks and balances, the lawmaking process.",
                 lessons=[
                     _l("nat-legislative", "The legislative branch", "Describe the structure and powers of Congress.", "direct_instruction", RT, standards=["CE.6"], materials=["legislative_slides"], output=["guided notes"]),
                     _l("nat-lawmaking", "How a bill becomes a law", "Sequence the lawmaking process.", "guided_practice", ANY, prereqs=["nat-legislative"], standards=["CE.6"], materials=["bill_flowchart"], output=["flowchart"]),
                     _l("nat-executive", "The executive branch", "Describe the powers of the President and the executive branch.", "direct_instruction", RT, prereqs=["nat-legislative"], standards=["CE.6"], materials=["executive_slides"], output=["guided notes"]),
                     _l("nat-judicial", "The judicial branch", "Describe the structure and powers of the federal courts.", "direct_instruction", RT, prereqs=["nat-executive"], standards=["CE.6"], materials=["judicial_slides"], output=["guided notes"]),
                     _l("nat-checks-balances", "Checks and balances", "Explain separation of powers and checks and balances with examples.", "guided_practice", ANY, prereqs=["nat-judicial"], standards=["CE.6"], materials=["checks_balances_organizer"], output=["organizer"]),
                     _l("nat-policy", "How the national government makes policy", "Explain the role of agencies and interest groups in policy.", "direct_instruction", RT, prereqs=["nat-checks-balances"], standards=["CE.6"], materials=["policy_slides"], output=["guided notes"]),
                     _l("nat-review", "National government review", "Review the national government.", "review", ANY, prereqs=["nat-policy"], standards=["CE.6"], materials=["nat_review_packet"], output=["review packet"]),
                     _quiz("nat-test", "National Government Test", ["nat-review"], ["CE.6"]),
                 ]),
        UnitSpec(slug="federalism-state-government", title="Federalism and Virginia's Government", quarter=2, planned_days=12, standards=["CE.7"],
                 summary="Division of powers between national and state governments; structure of Virginia's government and lawmaking.",
                 lessons=[
                     _l("federal-state-powers-vocab", "Federalism vocabulary", "Define expressed, implied, reserved and concurrent powers.", "direct_instruction", ANY, standards=["CE.7"], materials=["federalism_vocab_cards"], output=["vocab cards"]),
                     _l("federalism-intro", "Introduction to federalism", "Explain the division of powers between national and state governments.", "direct_instruction", RT, prereqs=["federal-state-powers-vocab"], standards=["CE.7"], materials=["federalism_slides", "guided_notes"], output=["completed guided notes"], mvm=25),
                     _l("federalism-guided-practice", "Federalism guided practice", "Sort powers into national, state and concurrent.", "guided_practice", ANY, prereqs=["federalism-intro"], standards=["CE.7"], materials=["powers_sort"], output=["powers sort"]),
                     _l("federalism-review", "Federalism review", "Review federalism concepts before the quiz.", "review", ANY, prereqs=["federalism-guided-practice"], standards=["CE.7"], materials=["federalism_review"], output=["review sheet"]),
                     _quiz("federalism-quiz", "Federalism Quiz", ["federalism-review"], ["CE.7"], mins=40),
                     _l("va-general-assembly", "Virginia's General Assembly", "Describe the structure and lawmaking process of the General Assembly.", "direct_instruction", RT, prereqs=["federalism-quiz"], standards=["CE.7"], materials=["va_legislature_slides"], output=["guided notes"]),
                     _l("va-executive-judicial", "Virginia's executive and judicial branches", "Describe the Governor's powers and Virginia's courts.", "direct_instruction", RT, prereqs=["va-general-assembly"], standards=["CE.7"], materials=["va_exec_slides"], output=["guided notes"]),
                     _quiz("va-gov-quiz", "Virginia Government Quiz", ["va-executive-judicial"], ["CE.7"], mins=40),
                 ]),
        UnitSpec(slug="local-government-policy", title="Local Government and Public Policy", quarter=3, planned_days=12, standards=["CE.8", "CE.9"],
                 summary="Structure of local government; how public policy is made and influenced.",
                 lessons=[
                     _l("local-structure", "Structure of local government", "Describe counties, cities, towns and their governing bodies.", "direct_instruction", RT, standards=["CE.8"], materials=["local_gov_slides"], output=["guided notes"]),
                     _l("local-services", "Local services and relationship to the state", "Explain local government services and the Dillon Rule.", "guided_practice", ANY, prereqs=["local-structure"], standards=["CE.8"], materials=["local_services_activity"], output=["services chart"]),
                     _l("policy-making", "How public policy is made", "Explain how individuals, interest groups and media shape policy.", "direct_instruction", RT, prereqs=["local-services"], standards=["CE.9"], materials=["policy_slides"], output=["guided notes"]),
                     _l("policy-case-study", "Public policy case study", "Analyze a local policy issue from multiple perspectives.", "discussion", ANY, prereqs=["policy-making"], standards=["CE.9", "CE.1"], materials=["case_study_packet"], output=["perspective chart"]),
                     _quiz("local-policy-test", "Local Government and Policy Test", ["policy-case-study"], ["CE.8", "CE.9"]),
                 ]),
        UnitSpec(slug="judicial-systems", title="The Judicial Systems", quarter=3, planned_days=12, standards=["CE.10"],
                 summary="US and Virginia court systems, jurisdiction, civil and criminal procedure, due process, judicial review.",
                 lessons=[
                     _l("jud-courts", "The court systems", "Describe the organization of the US and Virginia courts.", "direct_instruction", RT, standards=["CE.10"], materials=["courts_slides"], output=["court pyramid"]),
                     _l("jud-civil-criminal", "Civil vs. criminal cases", "Distinguish civil and criminal cases and procedures.", "guided_practice", ANY, prereqs=["jud-courts"], standards=["CE.10"], materials=["case_sort"], output=["case sort"]),
                     _l("jud-due-process", "Due process and rights of the accused", "Explain due process and the rights of the accused.", "direct_instruction", RT, prereqs=["jud-civil-criminal"], standards=["CE.10"], materials=["due_process_slides"], output=["guided notes"]),
                     _l("jud-judicial-review", "Judicial review and landmark cases", "Explain judicial review through Marbury v. Madison.", "direct_instruction", RT, prereqs=["jud-due-process"], standards=["CE.10"], materials=["landmark_cases"], output=["case brief"]),
                     _l("jud-mock-trial", "Mock trial", "Apply courtroom roles and procedures in a mock trial.", "project", RT, prereqs=["jud-judicial-review"], standards=["CE.10"], materials=["mock_trial_packet"], output=["trial roles"], priority="recommended"),
                     _quiz("jud-test", "Judicial Systems Test", ["jud-judicial-review"], ["CE.10"]),
                 ]),
        UnitSpec(slug="economics", title="Economics: Systems, the US Economy and Government's Role", quarter=4, planned_days=20, standards=["CE.11", "CE.12", "CE.13", "CE.14"],
                 summary="Scarcity and choice, economic systems, business organizations, financial institutions, government's role, careers.",
                 lessons=[
                     _l("econ-scarcity", "Scarcity, choice and opportunity cost", "Explain how scarcity forces choices with opportunity costs.", "direct_instruction", RT, standards=["CE.11"], materials=["scarcity_slides"], output=["guided notes"]),
                     _l("econ-supply-demand", "Supply and demand", "Explain how supply and demand determine price.", "guided_practice", ANY, prereqs=["econ-scarcity"], standards=["CE.11"], materials=["supply_demand_activity"], output=["graphs"]),
                     _l("econ-systems", "Economic systems", "Compare traditional, command, free market and mixed economies.", "direct_instruction", RT, prereqs=["econ-scarcity"], standards=["CE.11"], materials=["systems_slides"], output=["comparison chart"]),
                     _l("econ-business", "Business organizations and entrepreneurship", "Compare proprietorships, partnerships and corporations.", "direct_instruction", ANY, prereqs=["econ-systems"], standards=["CE.12"], materials=["business_slides"], output=["guided notes"]),
                     _l("econ-circular-flow", "Circular flow and financial institutions", "Explain the circular flow model and the role of banks.", "guided_practice", ANY, prereqs=["econ-business"], standards=["CE.12"], materials=["circular_flow_activity"], output=["circular flow diagram"]),
                     _l("econ-government-role", "Government's role in the economy", "Explain public goods, regulation and consumer protection.", "direct_instruction", RT, prereqs=["econ-circular-flow"], standards=["CE.13"], materials=["gov_role_slides"], output=["guided notes"]),
                     _l("econ-fed-fiscal", "The Federal Reserve and fiscal policy", "Explain monetary and fiscal policy tools.", "direct_instruction", RT, prereqs=["econ-government-role"], standards=["CE.13"], materials=["fed_slides"], output=["guided notes"]),
                     _l("econ-careers", "Career planning", "Relate education, skills and income to careers.", "independent_practice", IND, standards=["CE.14"], materials=["career_exploration"], output=["career plan"]),
                     _quiz("econ-test", "Economics Test", ["econ-fed-fiscal"], ["CE.11", "CE.12", "CE.13"]),
                 ]),
        UnitSpec(slug="civics-sol-review", title="Civics & Economics SOL Review", quarter=4, planned_days=12, standards=["CE.1", "CE.2", "CE.3", "CE.5", "CE.6", "CE.7", "CE.10", "CE.11"],
                 summary="Spiral review of the full course before the Civics & Economics SOL.",
                 lessons=[
                     _l("sol-review-foundations", "Review: foundations and citizenship", "Review CE.2 through CE.4.", "review", ANY, standards=["CE.2", "CE.3", "CE.4"], materials=["review_set_1"], output=["practice set"]),
                     _l("sol-review-government", "Review: government", "Review CE.6 through CE.8.", "review", ANY, standards=["CE.6", "CE.7", "CE.8"], materials=["review_set_2"], output=["practice set"]),
                     _l("sol-review-politics-courts", "Review: politics and courts", "Review CE.5, CE.9 and CE.10.", "review", ANY, standards=["CE.5", "CE.9", "CE.10"], materials=["review_set_3"], output=["practice set"]),
                     _l("sol-review-economics", "Review: economics", "Review CE.11 through CE.14.", "review", IND, standards=["CE.11", "CE.12", "CE.13", "CE.14"], materials=["review_set_4"], output=["practice set"]),
                     _l("sol-practice-test", "Practice SOL", "Complete a released practice test.", "review", ANY, prereqs=["sol-review-foundations", "sol-review-government", "sol-review-politics-courts", "sol-review-economics"], standards=["CE.1"], materials=["released_test"], output=["practice test"]),
                 ]),
    ]
    return CurriculumSpec(subject="civics", grade=7, units=units, source_notes=["LessonBridge default sequence (inference). Replace with the teacher's pacing guide when available."])


DEFAULT_CURRICULA = {"english": english_7, "civics": civics_7}


def default_replacement_activities() -> list[dict]:
    """Subject fallback activities from the design document."""
    english = [
        ("grammar-spiral-review", "Grammar spiral review", "Students complete a grammar spiral review packet covering sentence structure, punctuation and usage, then check with the answer key.", 45, "skill_maintenance", ["grammar_spiral_packet", "answer_key"], "completed grammar packet"),
        ("independent-reading-response", "Independent reading and response", "Students read their independent reading book for 25 minutes, then write a one-page response using the response menu.", 45, "curriculum_preserving", ["independent_reading_book", "response_menu"], "reading response"),
        ("editing-practice", "Editing practice", "Students correct a flawed paragraph for conventions and rewrite it.", 40, "skill_maintenance", ["editing_practice_sheet"], "corrected paragraph"),
        ("vocabulary-practice", "Vocabulary practice", "Students complete vocabulary practice (context clues, word parts) from the unit list.", 40, "curriculum_preserving", ["unit_vocab_list", "vocab_practice_sheet"], "vocabulary sheet"),
        ("writing-review", "Writing review", "Students review a model essay against the rubric and annotate strengths and areas to improve.", 45, "curriculum_preserving", ["model_essay", "rubric"], "annotated model essay"),
        ("emergency-reading-packet", "Emergency reading packet", "Students read the passage and answer the comprehension questions. Collect at the end of class.", 45, "emergency_filler", ["emergency_reading_packet"], "completed packet"),
    ]
    civics = [
        ("constitution-review", "Constitution review", "Students complete the Constitution review packet (branches, amendments, principles) and check with a partner.", 45, "curriculum_preserving", ["constitution_review_packet"], "completed packet"),
        ("primary-source-analysis", "Primary source analysis", "Students analyze a primary source using the source analysis sheet (source, context, audience, purpose, significance).", 45, "curriculum_preserving", ["primary_source", "source_analysis_sheet"], "source analysis sheet"),
        ("civics-vocabulary-review", "Civics vocabulary review", "Students complete vocabulary review for the current unit and write sentences using each term.", 40, "curriculum_preserving", ["unit_vocab_list"], "vocabulary sentences"),
        ("current-events-analysis", "Current events analysis", "Students read a provided news article and complete the current events analysis form connecting it to a civics standard.", 45, "skill_maintenance", ["news_article", "current_events_form"], "current events form"),
        ("guided-textbook-reading", "Guided textbook reading", "Students read the assigned textbook section and answer the guided reading questions.", 45, "curriculum_preserving", ["textbook", "guided_reading_questions"], "guided reading questions"),
        ("emergency-civics-packet", "Emergency civics packet", "Students complete the emergency civics packet. Collect at the end of class.", 45, "emergency_filler", ["emergency_civics_packet"], "completed packet"),
    ]
    # Keywords that say which units an activity fits, so a substitute day stays close to the current unit (LB-50).
    tags = {
        "grammar-spiral-review": ["grammar", "editing", "writing", "narrative", "argumentative", "conventions", "dialogue"],
        "independent-reading-response": ["reading", "fiction", "novel", "story", "stories", "character", "theme", "plot", "launching"],
        "editing-practice": ["editing", "writing", "narrative", "argumentative", "research", "revision", "drafting", "conventions"],
        "vocabulary-practice": ["vocabulary", "word", "context", "poetry", "figurative", "launching", "nonfiction"],
        "writing-review": ["writing", "argumentative", "narrative", "essay", "claims", "evidence", "research", "thesis"],
        "emergency-reading-packet": ["reading"],
        "constitution-review": ["constitution", "founding", "documents", "amendments", "rights", "federalism", "government", "branches", "principles"],
        "primary-source-analysis": ["primary", "sources", "documents", "declaration", "founding", "court", "judicial", "history", "skills"],
        "civics-vocabulary-review": ["vocabulary", "citizenship", "political", "economics", "government", "local", "policy"],
        "current-events-analysis": ["political", "policy", "media", "economics", "citizenship", "elections", "voting", "local", "public"],
        "guided-textbook-reading": ["government", "economics", "judicial", "courts", "local", "state", "national", "virginia"],
        "emergency-civics-packet": ["civics"],
    }
    out = []
    for subject, rows in (("english", english), ("civics", civics)):
        for slug, title, desc, mins, cat, mats, output in rows:
            out.append({"subject": subject, "slug": slug, "title": title, "description": desc, "duration_minutes": mins, "category": cat, "materials": mats,
                        "student_output": output, "delivery": "any_sub", "tags": [cat] + tags.get(slug, [])})
    return out
