import pytest

from resume_tailor.guardrails import apply_guardrails
from tests.helpers import PIPE_RESUME


@pytest.mark.parametrize("original,replacement", [
    ("Jan 2024 – Present", "Mar 2020 – Dec 2023"),
    ("Senior Widget Engineer (SAMPLE)", "Data Tinkerer (SAMPLE)"),
])
def test_facts_swapped_between_jobs_go_back_to_their_own_job(original, replacement):
    changed = PIPE_RESUME.replace(original, "TEMP_SWAP").replace(replacement, original).replace("TEMP_SWAP", replacement)
    fixed, report = apply_guardrails(PIPE_RESUME, changed)
    assert report.ok and fixed == PIPE_RESUME
    assert any("Kept your original" in item for item in report.warnings)


def test_reordered_jobs_are_not_relabelled():
    first = PIPE_RESUME.index("### ")
    second = PIPE_RESUME.index("### ", first + 1)
    end = PIPE_RESUME.index("## ", second + 4)
    changed = PIPE_RESUME[:first] + PIPE_RESUME[second:end] + PIPE_RESUME[first:second] + PIPE_RESUME[end:]
    _, report = apply_guardrails(PIPE_RESUME, changed)
    assert not report.ok
    assert any("facts together" in item for item in report.violations)


@pytest.mark.parametrize("heading", ["Education", "Academic Qualifications", "Certifications", "Professional Licenses"])
def test_qualifications_are_protected_in_both_directions(heading):
    base = PIPE_RESUME.replace("## Education", f"## {heading}")
    changed = base + "- Master of Science in Computing\n"
    fixed, report = apply_guardrails(base, changed)
    assert report.ok and fixed == base
    assert any(f"Kept your {heading} section" in item for item in report.warnings)


@pytest.mark.parametrize("claim", ["with latency of 7 ms", "processing 20 document types"])
def test_unsupported_claim_cannot_be_delivered_as_approved(claim):
    changed = PIPE_RESUME.replace("Built fictional Python services.", f"Built fictional Python services {claim}.")
    fixed, report = apply_guardrails(PIPE_RESUME, changed)
    assert claim not in fixed or not report.ok


def test_existing_number_elsewhere_does_not_vouch_for_new_metric():
    base = PIPE_RESUME.replace("More fiction.", "Processed 7 documents.")
    changed = base.replace("Built fictional Python services.", "Built fictional Python services for 7 documents.")
    fixed, report = apply_guardrails(base, changed)
    assert report.ok, report.violations
    assert "for 7 documents" not in fixed and "Processed 7 documents." in fixed


def test_semantic_edits_have_scoped_source_evidence_and_require_review():
    changed = PIPE_RESUME.replace("Built fictional Python services.", "Led delivery of fictional Python services.")
    _, report = apply_guardrails(PIPE_RESUME, changed)
    assert report.ok
    assert len(report.review_items) == 1
    item = report.review_items[0]
    assert "northstar" in item["section"]
    assert "Built fictional" in item["before"]
    assert "Led delivery" in item["after"]
    assert item["source_lines"]


def test_no_change_or_cosmetic_dates_do_not_require_review():
    _, report = apply_guardrails(PIPE_RESUME, PIPE_RESUME.replace("Jan 2024 – Present", "Jan 2024–Present"))
    assert report.ok
    assert not report.review_items
