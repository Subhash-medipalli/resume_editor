import pytest

from resume_tailor.guardrails import apply_guardrails, extract_facts
from tests.helpers import PIPE_RESUME


@pytest.mark.parametrize("quantity", ["2-hour", "2‑hour", "2–hour", "5-person"])
def test_hyphenated_metric_changes_restore_the_original_line(quantity):
    base = PIPE_RESUME.replace("Built fictional Python services.", f"Supported a {quantity} Python rollout.")
    altered = base.replace(quantity, "20" + quantity[1:])
    fixed, report = apply_guardrails(base, altered)
    assert report.ok and fixed == base
    assert report.warnings


@pytest.mark.parametrize("before,after", [("2-hour", "2 hours"), ("5-person", "5 people")])
def test_metric_rephrasing_keeps_the_same_quantity(before, after):
    base = PIPE_RESUME.replace("Built fictional Python services.", f"Supported a {before} Python rollout.")
    altered = base.replace(before, after)
    fixed, report = apply_guardrails(base, altered)
    assert report.ok and fixed == altered and not report.warnings


@pytest.mark.parametrize("link", [
    "https://www.linkedin.com/in/sample-placeholder",
    "github.com/sample-placeholder",
    "Portfolio: www.example.invalid/sample-placeholder",
])
@pytest.mark.parametrize("change", ["replace", "delete"])
def test_separate_contact_links_are_restored_without_losing_a_narrowed_headline(link, change):
    base = PIPE_RESUME.replace("pipe-format@example.invalid\n", f"pipe-format@example.invalid\n{link}\nPlatform Engineer | API Security\n")
    replacement = link.replace("sample-placeholder", "another-person") if change == "replace" else ""
    altered = base.replace(link, replacement).replace("Platform Engineer | API Security", "Platform Engineer")
    fixed, report = apply_guardrails(base, altered)
    assert report.ok and report.restored_contact
    assert link in fixed and "another-person" not in fixed
    assert "Platform Engineer" in fixed and "API Security" not in fixed


def test_unmarked_job_has_the_same_protected_facts_as_the_writer_sees():
    unmarked = PIPE_RESUME.replace("### ", "")
    assert extract_facts(unmarked).job_headings == extract_facts(PIPE_RESUME).job_headings
    assert extract_facts(unmarked).titles == extract_facts(PIPE_RESUME).titles
    altered = unmarked.replace("Senior Widget Engineer (SAMPLE)", "Director of Engineering")
    fixed, report = apply_guardrails(unmarked, altered)
    assert report.ok and fixed == unmarked
    assert any("Kept your original job title" in warning for warning in report.warnings)


@pytest.mark.parametrize("label", ["client", "project", "Client:", "project named"])
def test_client_and_project_names_cannot_be_changed(label):
    base = PIPE_RESUME.replace("Built fictional Python services.", f"Built Python services for {label} Northstar using Terraform.")
    altered = base.replace(f"{label} Northstar", f"{label} Contoso")
    fixed, report = apply_guardrails(base, altered)
    assert report.ok and fixed == base
    assert any("client/project name" in warning for warning in report.warnings)


@pytest.mark.parametrize("capability", [
    "client SDKs and REST APIs", "client Python SDKs", "client REST APIs", "client React applications",
    "Project Management workflows", "client OAuth integrations", "project CI/CD pipelines",
])
def test_generic_client_and_project_capabilities_can_be_added(capability):
    altered = PIPE_RESUME.replace("Built fictional Python services.",
                                 f"Built {capability} using Python and Kubernetes.")
    fixed, report = apply_guardrails(PIPE_RESUME, altered)
    assert report.ok and fixed == altered and not report.warnings


def test_named_client_is_protected_even_when_followed_by_a_capability():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Built services for client Northstar APIs.")
    fixed, report = apply_guardrails(base, base.replace("Northstar APIs", "Contoso APIs"))
    assert report.ok and fixed == base


def test_explicit_project_name_is_not_treated_as_a_generic_capability():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Built project named Management APIs.")
    fixed, report = apply_guardrails(base, base.replace("Management APIs", "Management SDKs"))
    assert report.ok and fixed == base


def test_rewriting_the_sentence_after_a_client_does_not_rename_the_client():
    base = PIPE_RESUME.replace("Built fictional Python services.", "Worked for client Northstar. Built Python services.")
    draft = base.replace("Northstar. Built", "Northstar. Delivered")
    _, report = apply_guardrails(base, draft)
    assert report.ok, report.violations


@pytest.mark.parametrize("label", ["Client Northstar", "Project Phoenix Migration"])
def test_standalone_names_are_protected(label):
    base = PIPE_RESUME.replace("Built fictional Python services. No real employer.", label)
    fixed, report = apply_guardrails(base, base.replace(label, label.replace("Northstar", "Contoso").replace("Phoenix", "Falcon")))
    assert report.ok and fixed == base


@pytest.mark.parametrize("phrase", ["Collaborated with Project Managers and QA.", "Worked with Client Stakeholders on reporting."])
def test_ordinary_role_words_read_as_names_only_undo_that_line(phrase):
    altered = PIPE_RESUME.replace("Built fictional Python services.", phrase)
    fixed, report = apply_guardrails(PIPE_RESUME, altered)
    assert report.ok and fixed == PIPE_RESUME


HEADED = "# Jane Sample\nSr. AI Data Engineer\nDallas, TX\njane@example.invalid | +1 555 010 0000\n\n## Summary\n- Builds data pipelines.\n"


@pytest.mark.parametrize("change,restored,headline_kept", [
    (("Sr. AI Data Engineer", "Data Engineer"), False, True),
    (("Sr. AI Data Engineer", "Senior / Lead AWS Data Engineer"), False, False),
    (("Dallas, TX", "Newark, NJ"), True, False),
    (("jane@", "janet@"), True, False),
    (("# Jane Sample", "# Janet Sample"), True, False),
    (("Sr. AI Data Engineer\n", ""), True, False),
])
def test_headline_above_the_contact_line_may_be_narrowed_but_not_retargeted_and_identity_may_not_change(
        change, restored, headline_kept):
    altered = HEADED.replace(*change)
    fixed, report = apply_guardrails(HEADED, altered)
    assert report.ok and report.restored_contact == restored
    expected = altered if headline_kept else HEADED
    assert fixed.split("## ")[0].split() == expected.split("## ")[0].split()
