"""Protected qualifications remain protected when the model repeats them in prose."""

import pytest

from resume_tailor.guardrails import apply_guardrails
from tests.helpers import PIPE_RESUME, SAMPLE_RESUME


@pytest.mark.parametrize("claim", ["Earned PMP certification.", "PMP-certified.", "PMP certification..."])
def test_period_ending_certification_is_repaired(claim):
    draft = PIPE_RESUME.replace("Fictional contractor", f"{claim} Fictional contractor")
    fixed, report = apply_guardrails(PIPE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == PIPE_RESUME
    assert report.warnings


@pytest.mark.parametrize("separator", ["\n", ", ", "; "])
def test_different_credentials_cannot_combine_to_authorize_upgrade(separator):
    base = SAMPLE_RESUME.replace(
        "AWS Certified Solutions Architect – Associate (SAMPLE), 2022",
        "AWS Certified Solutions Architect – Associate" + separator + "Project Management Professional (PMP)",
    )
    draft = base.replace("Contract software engineer", "AWS Certified Solutions Architect – Professional and contract software engineer")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == base
    assert report.warnings


@pytest.mark.parametrize("claim", [
    "AWS-certified.",
    "AWS Certified Solutions Architect – Associate.",
    "AWS Certified Solutions Architect – Associate with Python expertise.",
    "AWS Certified Solutions Architect with Python expertise.",
])
def test_existing_credential_can_be_repeated_in_summary(claim):
    draft = SAMPLE_RESUME.replace("Contract software engineer", f"{claim} Contract software engineer")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == draft
    assert not report.warnings


def test_vendor_at_start_of_certification_is_part_of_its_identity():
    base = SAMPLE_RESUME.replace("AWS Certified Solutions Architect – Associate (SAMPLE), 2022",
                                 "ExampleVendor Certified Cloud Expert")
    draft = base.replace("Contract software engineer", "OtherVendor Certified Cloud Expert and contract software engineer")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == base


def test_existing_credentials_can_be_repeated_as_a_list():
    base = SAMPLE_RESUME + "Project Management Professional (PMP)\n"
    draft = base.replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis\n- Certifications: AWS Certified Solutions Architect – Associate; PMP certification")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == draft


@pytest.mark.parametrize("claim", [
    "PhD-qualified", "Ph.D.-qualified", "Doctorate holder and", "Doctoral degree holder and",
    "Master's degree holder and", "Master of Science graduate and", "M.S.-qualified",
    "Master's in Computer Science graduate and", "M.Sc.-qualified",
    "MSc-qualified", "MBA-qualified", "MS in Computer Science graduate and",
    "Holds an MS and", "Associate degree holder and",
])
def test_new_degree_level_in_summary_keeps_the_original_line(claim):
    draft = SAMPLE_RESUME.replace("Contract software engineer", f"{claim} contract software engineer")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert f"{claim} contract" not in fixed and "Contract software engineer focused" in fixed


def test_new_degree_in_experience_keeps_the_original_line():
    draft = SAMPLE_RESUME.replace("- Built and operated Python services", "- Used PhD research to build Python services")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert "PhD" not in fixed and "- Built and operated Python services" in fixed


@pytest.mark.parametrize("text", ["Served as Scrum Master in sprint planning", "Scrum-Master in sprint planning"])
def test_scrum_master_is_a_role_not_a_degree(text):
    draft = SAMPLE_RESUME.replace("- Built and operated Python services", f"- {text} for Python services")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == draft


@pytest.mark.parametrize("claim", [
    "Received AWS Certified Solutions Architect certification;",
    "Maintained AWS Certified Solutions Architect certification;",
    "AWS Certified Solutions Architect working on Python services;",
])
def test_verbs_around_a_real_certification_are_not_part_of_its_name(claim):
    draft = SAMPLE_RESUME.replace("Contract software engineer", f"{claim} contract software engineer")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok and fixed == draft


@pytest.mark.parametrize("claim", [
    "AWS Certified Advanced Networking – Specialty",
    "Certified Advanced ScrumMaster",
    "Advanced Networking certification",
])
def test_ed_and_ing_words_do_not_hide_new_certification_names(claim):
    draft = SAMPLE_RESUME.replace("Contract software engineer", f"{claim} and contract software engineer")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == SAMPLE_RESUME
    assert report.warnings


def test_learning_does_not_hide_a_different_credential():
    base = SAMPLE_RESUME.replace("AWS Certified Solutions Architect – Associate", "AWS Certified Machine Learning – Specialty")
    draft = base.replace("Contract software engineer", "AWS Certified Machine Learning Engineer – Associate and contract software engineer")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == base
    assert report.warnings


@pytest.mark.parametrize("credential", [
    "AWS Certified Advanced Networking – Specialty",
    "Certified Advanced ScrumMaster",
    "AWS Certified Machine Learning Engineer – Associate",
])
def test_existing_credentials_with_ed_and_ing_words_remain_allowed(credential):
    base = SAMPLE_RESUME.replace("AWS Certified Solutions Architect – Associate", credential)
    draft = base.replace("Contract software engineer", f"Received {credential} and contract software engineer")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == draft
    assert not report.warnings


@pytest.mark.parametrize("claim", ["B.S.-qualified", "Bachelor of Science graduate and", "Bachelor's degree holder and", "BS in Computer Science graduate and"])
def test_degree_already_in_education_can_be_repeated(claim):
    draft = SAMPLE_RESUME.replace("Contract software engineer", f"{claim} contract software engineer")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == draft


@pytest.mark.parametrize("skill", ["master data management", "MS SQL", "MS Office", "associate client accounts"])
def test_skill_names_are_not_degree_claims(skill):
    draft = SAMPLE_RESUME.replace("- Data: PostgreSQL, Redis", f"- Data: PostgreSQL, Redis, {skill}")
    fixed, report = apply_guardrails(SAMPLE_RESUME, draft)
    assert report.ok, report.violations
    assert fixed == draft


def test_degree_claim_outside_education_cannot_be_removed():
    base = SAMPLE_RESUME.replace("Contract software engineer", "PhD-qualified contract software engineer")
    _, report = apply_guardrails(base, SAMPLE_RESUME)
    assert not report.ok
    assert any("degree claim" in violation for violation in report.violations)
