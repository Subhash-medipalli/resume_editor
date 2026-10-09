import pytest

from resume_tailor.guardrails import apply_guardrails, extract_facts, unified_diff
from tests.helpers import (
    FIXTURE_RESUME_PATH,
    PIPE_RESUME,
    SAMPLE_RESUME,
    lightly_tailored,
)


def test_extract_facts_from_sample_resume():
    facts = extract_facts(SAMPLE_RESUME)
    assert "Northwind Platform Co. (SAMPLE)" in facts.companies
    assert "Contoso Health Systems (SAMPLE)" in facts.companies
    assert "Placeholder Analytics LLC (SAMPLE)" in facts.companies
    assert "Senior Backend Engineer (Contract)" in facts.titles
    # Date spans are stored canonically (abbreviated month, tight dash, lowercase)
    # so that cosmetic reformatting by the model is not treated as an edit.
    assert "jul 2023–present" in facts.date_spans
    assert any("Placeholder State University" in line for line in facts.education_lines)
    assert any("Solutions Architect" in line for line in facts.cert_lines)
    assert facts.h2_headings == (
        "Summary",
        "Skills",
        "Experience",
        "Education",
        "Certifications",
    )


def test_light_edit_preserves_employers_and_dates():
    tailored = lightly_tailored(SAMPLE_RESUME)
    fixed, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert report.ok, report.violations
    assert report.changed_line_count > 0
    assert report.changed_line_count <= 10
    for company in (
        "Northwind Platform Co. (SAMPLE)",
        "Contoso Health Systems (SAMPLE)",
        "Placeholder Analytics LLC (SAMPLE)",
    ):
        assert company in fixed
    assert "Jul 2023" in fixed and "Present" in fixed
    assert "Mar 2021" in fixed and "Jun 2023" in fixed
    assert "Jan 2019" in fixed and "Feb 2021" in fixed
    assert "not-a-real-person@example.invalid" in fixed
    assert "Placeholder State University (SAMPLE), 2018" in fixed


def test_invented_employer_is_rejected():
    hacked = SAMPLE_RESUME.replace(
        "### Backend Engineer — Placeholder Analytics LLC (SAMPLE)",
        "### Backend Engineer — Placeholder Analytics LLC (SAMPLE)\n\n"
        "### Distinguished Fellow — Invented MegaCorp (FAKE)\n"
        "*Jan 2015 – Dec 2018* · Remote\n",
    )
    _, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert not report.ok
    joined = " ".join(report.violations).lower()
    assert "job" in joined or "employer" in joined or "invent" in joined


def test_contact_block_is_restored():
    hacked = SAMPLE_RESUME.replace(
        "**Alex Placeholder** (SAMPLE)",
        "**Alex Placeholder, PhD, Ninja** (SAMPLE)",
    )
    fixed, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert "**Alex Placeholder** (SAMPLE)" in fixed
    assert "PhD, Ninja" not in fixed
    assert report.restored_contact
    assert report.ok


def test_invented_metric_line_is_put_back_instead_of_failing_the_run():
    original = "Reduced duplicate processing in a batch pipeline by adding idempotency keys and CloudWatch alarms."
    hacked = SAMPLE_RESUME.replace(original, "Reduced duplicate processing by 87% and saved $2M in a batch pipeline.")
    fixed, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert report.ok, report.violations
    assert original in fixed and "87%" not in fixed and "$2M" not in fixed
    assert any("Kept your original line" in w for w in report.warnings)




def test_unified_diff_mentions_changed_summary():
    tailored = lightly_tailored(SAMPLE_RESUME)
    diff = unified_diff(
        SAMPLE_RESUME,
        tailored,
        fromfile="tests/fixtures/synthetic_resume.md",
        tofile="out/resume.md",
    )
    assert "--- tests/fixtures/synthetic_resume.md" in diff
    assert "+++ out/resume.md" in diff
    assert "AWS-hosted platform work" in diff


def test_fixtures_are_synthetic_not_production_resume():
    assert FIXTURE_RESUME_PATH.name == "synthetic_resume.md"
    assert "fixtures" in FIXTURE_RESUME_PATH.parts
    assert "SAMPLE" in SAMPLE_RESUME
    assert "Alex Placeholder" in SAMPLE_RESUME
    assert "SAMPLE" in PIPE_RESUME


def test_pipe_headings_extract_company_not_the_word_present():
    facts = extract_facts(PIPE_RESUME)
    assert "Northstar Fictional Bank, Testland" in facts.companies
    assert "Contoso Clinic, Testland" in facts.companies
    assert "Present" not in facts.companies
    assert "Senior Widget Engineer (SAMPLE)" in facts.titles
    assert "Data Tinkerer (SAMPLE)" in facts.titles
    assert "jan 2024–present" in facts.date_spans


def test_pipe_heading_invented_employer_is_rejected():
    at = PIPE_RESUME.index("## Education")
    hacked = PIPE_RESUME[:at] + (
        "### Spectre Holdings, Moon | Jan 2010 – Dec 2012\n"
        "**Secret Agent (FAKE)**\n\n"
    ) + PIPE_RESUME[at:]
    _, report = apply_guardrails(PIPE_RESUME, hacked)
    assert not report.ok
    joined = " ".join(report.violations).lower()
    assert "job" in joined or "employer" in joined or "invent" in joined


# --- the shape the product actually processed -------------------------------
# Every fixture above is markdown with "## " headings. The pipeline fed the
# guardrails *flat* text with none, which is how the whole-output-discard bug
# survived a green test suite.

FLAT_RESUME = (
    "Alex Placeholder (SAMPLE)\n"
    "not-a-real-person@example.invalid | +1-555-0100\n"
    "PROFESSIONAL SUMMARY:\n"
    "Engineer with 9+ years of widget experience.\n"
    "PROFESSIONAL EXPERIENCE:\n"
    "Northstar Fictional Bank, Testland | Jan 2024 - Present\n"
    "Built widgets.\n"
    "EDUCATION:\n"
    "BSc Widgetry - Placeholder State University (SAMPLE), 2018\n"
)


def test_edit_to_a_heading_less_resume_is_not_discarded():
    """The regression: a headless document made the whole resume the contact block."""
    tailored = FLAT_RESUME.replace("Built widgets.", "Built production widgets.")
    fixed, report = apply_guardrails(FLAT_RESUME, tailored)
    assert "Built production widgets." in fixed
    assert fixed.strip() != FLAT_RESUME.strip(), "the model's edit must survive"
    assert report.changed_line_count == 1
    assert report.ok


def test_contact_details_are_still_protected_without_headings():
    hacked = FLAT_RESUME.replace(
        "not-a-real-person@example.invalid", "attacker@example.invalid"
    )
    fixed, report = apply_guardrails(FLAT_RESUME, hacked)
    assert "not-a-real-person@example.invalid" in fixed
    assert "attacker@example.invalid" not in fixed
    assert report.restored_contact


def test_expanded_employer_name_is_rejected():
    """`hay in needle` used to accept any name that contained a real one."""
    hacked = PIPE_RESUME.replace(
        "Northstar Fictional Bank, Testland",
        "Northstar Fictional Bank Holdings Group Pte Ltd, Testland",
    )
    fixed, report = apply_guardrails(PIPE_RESUME, hacked)
    assert report.ok and fixed == PIPE_RESUME
    assert any("Kept your original job heading" in w for w in report.warnings)


def test_inflated_job_title_is_rejected():
    """Titles had only a forward check, so seniority inflation passed."""
    hacked = PIPE_RESUME.replace(
        "Data Tinkerer (SAMPLE)", "Principal Staff Data Tinkerer (SAMPLE)"
    )
    fixed, report = apply_guardrails(PIPE_RESUME, hacked)
    assert report.ok and fixed == PIPE_RESUME
    assert any("Kept your original job title" in w for w in report.warnings)


def test_inflated_years_of_experience_are_put_back():
    hacked = FLAT_RESUME.replace("9+ years", "15+ years")
    fixed, report = apply_guardrails(FLAT_RESUME, hacked)
    assert report.ok, report.violations
    assert "9+ years" in fixed and "15+ years" not in fixed


def test_rephrased_years_of_experience_are_the_same_claim():
    tailored = FLAT_RESUME.replace("9+ years", "over 9 years")
    fixed, report = apply_guardrails(FLAT_RESUME, tailored)
    assert report.ok, report.violations
    assert fixed == tailored


def test_cosmetic_date_reformatting_is_not_a_violation():
    """Reformatting raised both "missing" and "invented" for the same date."""
    tailored = PIPE_RESUME.replace("Jan 2024 – Present", "Jan 2024–Present")
    _, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations


def test_bulk_deletion_is_rejected():
    stripped = "\n".join(SAMPLE_RESUME.splitlines()[:8]) + "\n"
    _, report = apply_guardrails(SAMPLE_RESUME, stripped)
    assert not report.ok














def test_unchanged_resume_raises_no_technology_violations():
    """Precision check: the base must never flag itself."""
    _, report = apply_guardrails(SAMPLE_RESUME, SAMPLE_RESUME)
    assert report.ok
    assert not report.violations










def test_match_first_keeps_new_technology_named_in_the_job_description():
    tailored = SAMPLE_RESUME.replace(
        "- Languages: Python, SQL, Bash",
        "- Languages: Python, SQL, Bash, PySpark, Terraform",
    )
    fixed, report = apply_guardrails(
        SAMPLE_RESUME,
        tailored,
    )
    assert report.ok, report.violations
    assert "PySpark" in fixed and "Terraform" in fixed


def test_match_first_does_not_require_an_exact_source_or_jd_vocabulary_match():
    tailored = SAMPLE_RESUME.replace(
        "- Languages: Python, SQL, Bash",
        "- Languages: Python, SQL, Bash, Terraform",
    )
    fixed, report = apply_guardrails(
        SAMPLE_RESUME,
        tailored,
    )
    assert report.ok, report.violations
    assert fixed == tailored
    assert "Terraform" in fixed
    assert not report.warnings


def test_match_first_restores_employer_date_and_education_changes():
    tailored = (
        SAMPLE_RESUME.replace("Northwind Platform Co. (SAMPLE)", "Invented Corp (FAKE)")
        .replace("Jul 2023 – Present", "Jan 2020 – Present")
        .replace("Placeholder State University (SAMPLE)", "Imaginary University (FAKE)")
    )
    fixed, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert report.ok, report.violations
    assert "Northwind Platform Co. (SAMPLE)" in fixed and "Invented Corp" not in fixed
    assert "Jul 2023 – Present" in fixed and "Placeholder State University (SAMPLE)" in fixed
    assert "Imaginary University" not in fixed




def test_match_first_keeps_concrete_responsibilities_for_new_capabilities():
    tailored = SAMPLE_RESUME.replace(
        "- Built and operated Python services that ingest partner events and expose REST APIs for internal tools.",
        "- Implemented Terraform infrastructure and SSO for internal services.",
    )
    fixed, report = apply_guardrails(
        SAMPLE_RESUME,
        tailored,
    )
    assert report.ok, report.violations
    assert fixed == tailored
    assert "Implemented Terraform infrastructure and SSO" in fixed
    assert not report.warnings


def test_match_first_undoes_a_new_named_project_even_when_the_jd_mentions_it():
    tailored = SAMPLE_RESUME.replace(
        "- Added Redis caching for read-heavy lookup endpoints and documented failure modes.",
        "- Experience with SSO delivery for project named Phoenix Migration.",
    )
    fixed, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert report.ok, report.violations
    assert "Phoenix Migration" not in fixed
    assert "- Added Redis caching for read-heavy lookup endpoints and documented failure modes." in fixed


def test_default_allows_full_resume_rewriting_without_a_line_ceiling():
    base = "# Candidate\na@example.invalid\n\n## Summary\nOriginal summary.\n\n## Skills\n- Tools: Python\n\n## Experience\n### Sample Co | Jan 2020 – Present\n**Engineer**\n"
    base += "\n".join(f"- Developed the original workflow component {chr(65 + i % 26)}." for i in range(160)) + "\n"
    tailored = base.replace("Original summary.", "Backend engineer delivering secure APIs and reliable cloud services.").replace("- Tools: Python", "- Tools: Python, Terraform, OAuth 2.0, SAML 2.0, OpenTelemetry")
    tailored = tailored.replace("Developed the original workflow component", "Implemented Terraform-managed services with SSO and observability for component")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert fixed == tailored
    assert report.changed_line_count > 140


def test_new_product_versions_and_standards_are_not_achievement_metrics():
    tailored = PIPE_RESUME.replace("Built fictional Python services.", "Implemented OAuth 2.0, HTTP 429 retries, TLS 1.3, Python 3.12 and ISO 27001 controls.")
    fixed, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations
    assert fixed == tailored


def test_rewriting_must_preserve_section_order():
    tailored = PIPE_RESUME.replace("## Professional Summary", "## TEMP").replace("## Professional Experience", "## Professional Summary").replace("## TEMP", "## Professional Experience")
    _, report = apply_guardrails(PIPE_RESUME, tailored)
    assert not report.ok
    assert any("Section headings or order" in v for v in report.violations)


def test_rewriting_must_not_deliver_job_headings_with_no_bullets():
    tailored = "\n".join(line for line in PIPE_RESUME.splitlines() if not line.startswith("- ")) + "\n"
    _, report = apply_guardrails(PIPE_RESUME, tailored)
    assert not report.ok
    assert any("All bullets" in v for v in report.violations)




def test_changed_or_duplicated_metric_keeps_the_original_line():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Reduced latency by 30% for Python services.")
    for tailored in (base.replace("30%", "50%"), base.replace("More fiction.", "Reduced latency by 30%.")):
        fixed, report = apply_guardrails(base, tailored)
        assert report.ok, report.violations
        assert fixed == base
        assert any("Kept your original line" in w for w in report.warnings)


def test_dropped_or_transferred_metric_keeps_the_original_line():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Reduced latency by 30% for Python services.")
    dropped = base.replace("Reduced latency by 30%", "Reduced latency")
    for tailored in (dropped, dropped.replace("More fiction.", "Reduced latency by 30%.")):
        fixed, report = apply_guardrails(base, tailored)
        assert report.ok, report.violations
        assert fixed == base
        assert any("Kept your original line" in w for w in report.warnings)


def test_currency_scale_change_is_undone():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Saved $1 million with Python services.")
    fixed, report = apply_guardrails(base, base.replace("$1 million", "$1 billion"))
    assert report.ok, report.violations
    assert fixed == base


@pytest.mark.parametrize("claim,is_real", [
    ("Certified Kubernetes Administrator and", False),
    ("Certified kubernetes administrator and", False),
    ("PMP certification holder and", False),
    ("Certified in Kubernetes and", False),
    ("AWS Certified Solutions Architect – Professional and", False),
    ("AWS Certified Solutions Architect with Python expertise and", True),
    ("AWS Certified Solutions Architect – Associate and", True),
    ("AWS-certified", True),
])
def test_invented_certifications_are_undone_but_real_ones_can_be_mentioned(claim, is_real):
    summary = "Contract software engineer focused"
    tailored = SAMPLE_RESUME.replace(summary, f"{claim} contract software engineer focused")
    fixed, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert report.ok, report.violations
    assert (fixed == tailored) is is_real
    assert summary in fixed or is_real


def test_certification_lists_and_uncertified_resumes_are_checked():
    listed = SAMPLE_RESUME.replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis\n- Certifications: AWS Solutions Architect, CKA")
    fixed, report = apply_guardrails(SAMPLE_RESUME, listed)
    assert report.ok and "CKA" not in fixed
    lowercase = SAMPLE_RESUME.replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis\n- pmp certification")
    fixed, report = apply_guardrails(SAMPLE_RESUME, lowercase)
    assert report.ok and "pmp" not in fixed
    fixed, report = apply_guardrails(PIPE_RESUME, PIPE_RESUME.replace("Fictional contractor", "AWS Certified fictional contractor"))
    assert report.ok and "AWS Certified" not in fixed


def test_rewritten_summary_with_invented_certification_keeps_the_original_summary():
    original = SAMPLE_RESUME.split("## Summary\n", 1)[1].split("\n", 1)[0]
    tailored = SAMPLE_RESUME.replace(original, "Certified Kubernetes Administrator delivering platform migrations.")
    fixed, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert report.ok, report.violations
    assert original in fixed


def test_new_bullet_next_to_a_rewrite_that_drops_a_number_is_kept():
    base = PIPE_RESUME.replace("- Built fictional Python services. No real employer.", "- Reduced latency by 30% for Python services.")
    tailored = base.replace("- Reduced latency by 30% for Python services.",
                            "- Built a Grafana dashboard for service health.\n- Reduced latency for Python services using caching.")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert "Grafana dashboard" in fixed and "Reduced latency by 30%" in fixed
    assert fixed.count("Reduced latency") == 1


def test_lowercase_text_after_project_is_not_a_named_project():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Led the project: migrated fictional services.")
    tailored = base.replace("migrated fictional services", "modernized fictional Python services on AWS")
    _, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations


CONTACT = "jane@example.invalid | +1 555 010 0000"
HEADLINE = "Sr. AI/ML Engineer / Data Scientist"
# The headline above the contact line, below it, and in bold.
LAYOUTS = [pytest.param("{h}\n" + CONTACT, id="above"), pytest.param(CONTACT + "\n{h}", id="below"),
           pytest.param("**{h}**\n" + CONTACT, id="bold-above"), pytest.param(CONTACT + "\n**{h}**", id="bold-below")]


def jane(header):
    return f"# Jane Doe\n{header}\n\n## Summary\n- Builds pipelines.\n"


def test_a_headline_the_original_lacks_is_removed_without_flagging_the_contact():
    base = jane(CONTACT)
    fixed, report = apply_guardrails(base, jane(CONTACT + "\nPlatform Engineer | API Security"))
    assert report.ok, report.violations
    assert fixed == base
    assert not report.restored_contact
    assert report.warnings == ["Removed a headline the edit added; your resume has none."]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_resume_without_a_headline_does_not_gain_one(layout):
    base = jane(CONTACT)
    fixed, report = apply_guardrails(base, jane(layout.format(h="Sr. AI/ML Engineer")))
    assert report.ok and fixed == base
    assert report.warnings[-1] == "Removed a headline the edit added; your resume has none."


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("retargeted", [
    "AI/ML Engineer / Data Scientist – AI Strategic Plan & Prototype Development",
    "Sr. Agentic AI & Infrastructure Automation Engineer",
    "Sr. AI/ML Engineer / Data Scientist – GenAI",
    "Senior AI/ML Engineer / Data Scientist",
])
def test_a_headline_retargeted_to_the_job_gets_the_original_back(layout, retargeted):
    base = jane(layout.format(h=HEADLINE))
    fixed, report = apply_guardrails(base, jane(layout.format(h=retargeted)))
    assert report.ok, report.violations
    assert fixed == base and not report.restored_contact
    assert report.warnings == [f"Kept your original headline: “{HEADLINE}”"]


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("narrowed", [
    "ML Engineer", "AI Engineer", "Data Scientist", "Sr. ML Engineer", "Data Scientist / AI/ML Engineer",
    "AI and ML Engineer", "SR. AI/ML ENGINEER",
])
def test_a_narrowed_or_reordered_headline_is_kept(layout, narrowed):
    base = jane(layout.format(h=HEADLINE))
    tailored = jane(layout.format(h=narrowed))
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert fixed == tailored and not report.warnings and not report.restored_contact


def test_a_headline_that_loses_its_bold_is_not_an_edit():
    base = jane(CONTACT + f"\n**{HEADLINE}**")
    tailored = jane(CONTACT + "\nML Engineer")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok and fixed == tailored and not report.warnings


def test_one_new_word_on_any_header_line_restores_every_header_line():
    base = jane(f"Sr. AI/ML Engineer\n{CONTACT}\nData Scientist")
    narrowed = base.replace("Sr. AI/ML Engineer", "ML Engineer")
    fixed, report = apply_guardrails(base, narrowed)
    assert fixed == narrowed and not report.warnings
    fixed, report = apply_guardrails(base, narrowed.replace("Data Scientist", "GenAI Data Scientist"))
    assert report.ok and fixed == base and len(report.warnings) == 1


@pytest.mark.parametrize("layout", LAYOUTS)
def test_contact_tampering_and_a_retargeted_headline_both_come_back(layout):
    base = jane(layout.format(h=HEADLINE))
    tailored = jane(layout.format(h="Sr. Agentic AI Engineer").replace("jane@", "changed@"))
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert fixed == base and report.restored_contact
    assert report.warnings[0].startswith("Name or contact details were altered")
    assert report.warnings[1:] == [f"Kept your original headline: “{HEADLINE}”"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_contact_repair_preserves_a_narrowed_headline(layout):
    base = jane(layout.format(h=HEADLINE))
    fixed, report = apply_guardrails(base, jane(layout.format(h="ML Engineer").replace("jane@", "changed@")))
    assert report.ok, report.violations
    assert report.restored_contact and report.warnings == [
        "Name or contact details were altered by the model; restored from the base resume."]
    assert "jane@example.invalid" in fixed and "changed@" not in fixed
    assert fixed == jane(layout.format(h="ML Engineer"))


def test_a_headline_is_guarded_in_a_document_with_no_section_headings():
    base = f"# Jane Doe\n{HEADLINE}\n{CONTACT}\n- Builds pipelines.\n"
    tailored = base.replace(HEADLINE, "Sr. Agentic AI Engineer").replace("Builds pipelines", "Builds ML pipelines")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert fixed == base.replace("Builds pipelines", "Builds ML pipelines")
    assert len(report.warnings) == 1 and "original headline" in report.warnings[0]


# Header text that is not a job title: a summary paragraph, a skills strip or an
# unrecognised label with bullets (Word styles that parse as a tagline or bullet above
# the first heading). Tailoring it is the whole point, so it is never held to the headline rule.
SUMMARY = "Data engineer with eight years building pipelines on AWS and Spark."
HEADER_TEXT = [
    pytest.param(f"**{SUMMARY}**", f"**{SUMMARY[:-1]} and Kafka streaming.**", id="unlabeled-summary"),
    pytest.param(f"**Summary**\n{SUMMARY}", f"**Summary**\n{SUMMARY[:-1]} and Kafka streaming.", id="summary-label"),
    pytest.param("**Python | SQL | AWS**", "**Python | SQL | AWS | Kafka | Snowflake**", id="skills-strip"),
    pytest.param("**Technical Skills**\n- Languages: Python, SQL\n- Cloud: AWS",
                 "**Technical Skills**\n- Languages: Python, SQL, Scala\n- Cloud: AWS, Snowflake", id="skills-label"),
]


@pytest.mark.parametrize("before,after", HEADER_TEXT)
def test_header_text_that_is_not_a_job_title_is_tailored_as_usual(before, after):
    tailored = jane(CONTACT + "\n" + after)
    fixed, report = apply_guardrails(jane(CONTACT + "\n" + before), tailored)
    assert report.ok, report.violations
    assert fixed == tailored and not report.warnings


@pytest.mark.parametrize("before,after", HEADER_TEXT)
def test_a_retargeted_headline_is_reverted_without_undoing_the_text_beside_it(before, after):
    base = jane(f"{CONTACT}\n**{HEADLINE}**\n{before}")
    tailored = jane(f"{CONTACT}\n**Sr. Agentic AI Engineer**\n{after}")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
    assert fixed == jane(f"{CONTACT}\n**{HEADLINE}**\n{after}")
    assert report.warnings == [f"Kept your original headline: “{HEADLINE}”"]


@pytest.mark.parametrize("contact,headline,retargeted", [
    ("jane@example.invalid | linkedin.com/in/jane-doe-data-engineer", "Sr. Software Developer", "Data Engineer"),
    ("jane.python@example.invalid | +1 555 010 0000", "Sr. Developer", "Sr. Python Developer"),
])
def test_words_in_the_contact_line_do_not_license_a_retargeted_headline(contact, headline, retargeted):
    base = jane(f"{headline}\n{contact}")
    fixed, report = apply_guardrails(base, jane(f"{retargeted}\n{contact}"))
    assert report.ok and fixed == base
    assert report.warnings == [f"Kept your original headline: “{headline}”"]


def test_every_original_headline_line_comes_back_when_the_model_drops_one():
    base = jane(f"Sr. AI/ML Engineer\n{CONTACT}\nData Scientist")
    fixed, report = apply_guardrails(base, jane(f"GenAI Engineer\n{CONTACT}"))
    assert report.ok and "GenAI" not in fixed
    assert "Sr. AI/ML Engineer" in fixed and "Data Scientist" in fixed and CONTACT in fixed


def test_rewriting_cannot_leave_a_summary_heading_with_no_content():
    tailored = PIPE_RESUME.replace("Fictional contractor used only in unit tests. SAMPLE / REPLACE ME.", "")
    _, report = apply_guardrails(PIPE_RESUME, tailored)
    assert not report.ok
    assert any("content was emptied" in v for v in report.violations)


def test_tool_released_after_a_role_ended_keeps_the_original_line():
    tailored = PIPE_RESUME.replace("- More fiction. SAMPLE only.", "- Built LangGraph agents for claims triage.")
    fixed, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations
    assert "LangGraph" not in fixed and "- More fiction. SAMPLE only." in fixed
    assert any("released after that role ended" in warning for warning in report.warnings)


def test_new_tool_in_the_current_role_is_kept():
    tailored = PIPE_RESUME.replace("- Built fictional Python services. No real employer.",
                                   "- Built LangGraph agents with fictional Python services.")
    fixed, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok and fixed == tailored


def test_tool_the_original_role_already_names_is_kept():
    base = PIPE_RESUME.replace("- More fiction. SAMPLE only.", "- Prototyped LangGraph agents.")
    tailored = base.replace("- Prototyped LangGraph agents.", "- Prototyped LangGraph agents for claims triage.")
    fixed, report = apply_guardrails(base, tailored)
    assert report.ok and fixed == tailored
