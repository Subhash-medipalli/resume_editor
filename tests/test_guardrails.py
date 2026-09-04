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


def test_invented_metric_is_rejected():
    hacked = SAMPLE_RESUME.replace(
        "Reduced duplicate processing in a batch pipeline by adding idempotency keys and CloudWatch alarms.",
        "Reduced duplicate processing by 87% and saved $2M in a batch pipeline.",
    )
    _, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert not report.ok
    joined = " ".join(report.violations)
    assert "metric" in joined.lower()


def test_rewrite_is_rejected():
    rewrite = "# totally different\n\n" + "\n".join(f"line {i}" for i in range(40))
    _, report = apply_guardrails(SAMPLE_RESUME, rewrite)
    assert not report.ok
    assert any("rewrite" in v.lower() or "lines changed" in v.lower() for v in report.violations)


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
    assert "Sravya" not in SAMPLE_RESUME
    assert "mksravya6" not in SAMPLE_RESUME.lower()
    assert "253-200-7287" not in SAMPLE_RESUME
    assert "SAMPLE" in SAMPLE_RESUME
    assert "Alex Placeholder" in SAMPLE_RESUME
    assert "Sravya" not in PIPE_RESUME
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
    hacked = PIPE_RESUME + (
        "\n### Spectre Holdings, Moon | Jan 2010 – Dec 2012\n"
        "**Secret Agent (FAKE)**\n"
    )
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
    _, report = apply_guardrails(PIPE_RESUME, hacked)
    assert not report.ok
    assert any("employer" in v.lower() for v in report.violations)


def test_inflated_job_title_is_rejected():
    """Titles had only a forward check, so seniority inflation passed."""
    hacked = PIPE_RESUME.replace(
        "Data Tinkerer (SAMPLE)", "Principal Staff Data Tinkerer (SAMPLE)"
    )
    _, report = apply_guardrails(PIPE_RESUME, hacked)
    assert not report.ok
    assert any("title" in v.lower() for v in report.violations)


def test_inflated_years_of_experience_is_rejected():
    """METRIC_RE matched only currency and percentages, so "9+ years" was free."""
    hacked = FLAT_RESUME.replace("9+ years", "15+ years")
    _, report = apply_guardrails(FLAT_RESUME, hacked)
    assert not report.ok
    assert any("metric" in v.lower() for v in report.violations)


def test_cosmetic_date_reformatting_is_not_a_violation():
    """Reformatting raised both "missing" and "invented" for the same date."""
    tailored = PIPE_RESUME.replace("Jan 2024 – Present", "Jan 2024–Present")
    _, report = apply_guardrails(PIPE_RESUME, tailored)
    assert report.ok, report.violations


def test_bulk_deletion_is_rejected():
    stripped = "\n".join(SAMPLE_RESUME.splitlines()[:8]) + "\n"
    _, report = apply_guardrails(SAMPLE_RESUME, stripped)
    assert not report.ok


def test_changelog_claiming_edits_with_no_diff_is_flagged():
    """The exact signature of the shipped bug: 5 claimed edits, 0 lines changed."""
    from resume_tailor.guardrails import check_changelog_matches_diff

    claims = ["Updated the summary", "Reordered Technical Skills"]
    assert check_changelog_matches_diff(claims, 0)
    assert not check_changelog_matches_diff(claims, 4)
    # A model that honestly reports making no changes must not be flagged.
    assert not check_changelog_matches_diff(
        ["No relevant experience to surface; left the resume unchanged"], 0
    )


def test_excessive_additions_are_rejected():
    """Additions reach the Word file now, so they need a ceiling of their own."""
    padded = SAMPLE_RESUME.rstrip("\n") + "\n" + "\n".join(
        f"- Invented extra achievement {i}" for i in range(12)
    )
    _, report = apply_guardrails(SAMPLE_RESUME, padded)
    assert not report.ok
    assert any("added" in v.lower() for v in report.violations)


def test_a_few_additions_are_allowed_but_warned():
    padded = SAMPLE_RESUME.rstrip("\n") + "\n- Surfaced an existing Python skill\n"
    _, report = apply_guardrails(SAMPLE_RESUME, padded)
    assert report.ok, report.violations
    assert any("added" in w.lower() for w in report.warnings)


def test_invented_technology_is_rejected():
    """Prompt rule 4 forbids inventing tools; nothing in code enforced it."""
    hacked = SAMPLE_RESUME.replace(
        "- Languages: Python, SQL, Bash", "- Languages: Python, SQL, Bash, GitOps, PySpark"
    )
    _, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert not report.ok
    joined = " ".join(report.violations)
    assert "GitOps" in joined or "PySpark" in joined


def test_a_tool_added_to_a_skills_line_is_rejected_even_if_it_looks_ordinary():
    """"Terraform" and "Runbooks" are shaped like plain words but are new claims."""
    hacked = SAMPLE_RESUME.replace(
        "- Languages: Python, SQL, Bash", "- Languages: Python, SQL, Bash, Terraform"
    )
    _, report = apply_guardrails(SAMPLE_RESUME, hacked)
    assert not report.ok
    assert any("Terraform" in v for v in report.violations)


def test_resurfacing_an_existing_tool_on_a_skills_line_is_allowed():
    """The tool's whole purpose is surfacing evidence that is already there."""
    facts_source = SAMPLE_RESUME
    assert "CloudWatch" in facts_source
    tailored = facts_source.replace(
        "- Languages: Python, SQL, Bash", "- Languages: Python, SQL, Bash, CloudWatch"
    )
    _, report = apply_guardrails(facts_source, tailored)
    assert report.ok, report.violations


def test_unchanged_resume_raises_no_technology_violations():
    """Precision check: the base must never flag itself."""
    _, report = apply_guardrails(SAMPLE_RESUME, SAMPLE_RESUME)
    assert report.ok
    assert not report.violations


def test_changelog_claiming_many_edits_for_one_changed_line_is_flagged():
    """A model listed four edits and had deleted one unrelated skill."""
    from resume_tailor.guardrails import check_changelog_matches_diff

    assert check_changelog_matches_diff(["Reworded summary", "Reordered skills", "Tweaked bullet"], 1)
    assert not check_changelog_matches_diff(["Tweaked one bullet"], 1)


def test_dropping_a_true_skill_is_warned():
    tailored = SAMPLE_RESUME.replace("- Languages: Python, SQL, Bash", "- Languages: Python, SQL")
    _, report = apply_guardrails(SAMPLE_RESUME, tailored)
    assert any("Bash" in w for w in report.warnings)


def test_prose_vouches_for_capitalised_skill_terms():
    """"human-in-the-loop" in a bullet is evidence for "Human-in-the-Loop" on a skills line."""
    base = "# A\na@b.com\n\n## Summary\n\n- Built human-in-the-loop review with structured outputs and prompt evaluation.\n\n## Skills\n\n- Tools: Python\n"
    tailored = base.replace("- Tools: Python", "- Tools: Python, Human-in-the-Loop Review, Structured Outputs, Evals")
    _, report = apply_guardrails(base, tailored)
    assert report.ok, report.violations
