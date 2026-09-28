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


def test_adding_a_headline_does_not_restore_the_old_header():
    tailored = PIPE_RESUME.replace("pipe-format@example.invalid\n", "pipe-format@example.invalid\nPlatform Engineer | API Security\n")
    fixed, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations
    assert fixed == tailored
    assert not report.restored_contact


def test_contact_repair_preserves_a_new_headline():
    tailored = PIPE_RESUME.replace("pipe-format@example.invalid\n", "changed@example.invalid\nPlatform Engineer | API Security\n")
    fixed, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations
    assert report.restored_contact
    assert "pipe-format@example.invalid" in fixed
    assert "changed@example.invalid" not in fixed
    assert "Platform Engineer | API Security" in fixed


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
