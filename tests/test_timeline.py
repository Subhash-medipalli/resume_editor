"""Timeline repairs preserve valid edits all the way to the published Word file."""

from pathlib import Path

from docx import Document
import pytest

from resume_tailor.docx_io import extract_markdown
from resume_tailor.guardrails import apply_guardrails
from resume_tailor.pipeline import run_tailoring
from tests.helpers import PIPE_RESUME, pack_model_output


OLD_HEADING = "### Contoso Clinic, Testland | Mar 2020 – Dec 2023"
OLD_BULLET = "- More fiction. SAMPLE only."
CURRENT_BULLET = "- Built fictional Python services. No real employer."


@pytest.mark.parametrize("tool,end_year,kept", [("Spark", 2011, True), ("Pinecone", 2020, False), ("Pinecone", 2021, True)])
def test_public_release_years_allow_early_spark_but_not_prelaunch_pinecone(tool, end_year, kept):
    base = PIPE_RESUME.replace("Mar 2020 – Dec 2023", f"Mar 2010 – Dec {end_year}")
    draft = base.replace(OLD_BULLET, f"- Built {tool} services for claims triage.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == (draft if kept else base)
    assert bool(report.warnings) is not kept


@pytest.mark.parametrize("tool,end_year", [("LangGraph-based", 2023), ("Lambda-powered", 2013)])
def test_hyphenated_tools_cannot_be_added_before_release(tool, end_year):
    base = PIPE_RESUME.replace("Mar 2020 – Dec 2023", f"Mar 2010 – Dec {end_year}")
    draft = base.replace(OLD_BULLET, f"- Built {tool} services for claims triage.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert fixed == base
    assert report.warnings


@pytest.mark.parametrize("word", ["LangGraphical", "PreLangGraph", "Sparkling"])
def test_tool_name_substrings_do_not_trigger_timeline_repairs(word):
    base = PIPE_RESUME.replace("Mar 2020 – Dec 2023", "Mar 2008 – Dec 2010")
    draft = base.replace(OLD_BULLET, f"- Maintained {word} workflows.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed == draft
    assert not report.warnings


@pytest.mark.parametrize("heading", [
    OLD_HEADING,
    "### Contoso Clinic, Testland\n*Mar 2020 – Dec 2023*",
    "Contoso Clinic, Testland | Mar 2020 – Dec 2023",
])
def test_role_timeline_is_checked_for_supported_heading_shapes(heading):
    base = PIPE_RESUME.replace(OLD_HEADING, heading)
    fixed, report = apply_guardrails(base, base.replace(OLD_BULLET, "- Built LangGraph agents."))
    assert report.ok, report.violations
    assert fixed == base
    assert report.warnings


def test_dates_in_a_non_job_section_do_not_restrict_its_tools():
    base = PIPE_RESUME + "\n## Skills 2020 – 2023\n- Python services.\n"
    draft = base.replace("- Python services.", "- Python and LangGraph services.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed == draft
    assert not report.warnings


def test_original_tool_exemption_belongs_to_its_own_role():
    base = PIPE_RESUME.replace(CURRENT_BULLET, "- Built LangGraph agents.")
    fixed, report = apply_guardrails(base, base.replace(OLD_BULLET, "- Built LangGraph services."))
    assert report.ok, report.violations
    assert fixed == base
    own_role_base = PIPE_RESUME.replace(OLD_BULLET, "- Built LangGraph agents.")
    draft = own_role_base.replace("- Built LangGraph agents.", "- Built LangGraph agents for claims triage.")
    fixed, report = apply_guardrails(own_role_base, draft)
    assert report.ok and fixed == draft
    assert not report.warnings


def test_repeated_job_headings_keep_separate_timeline_and_tool_exemptions():
    heading = "### Data Engineer — Example Systems"
    base = (PIPE_RESUME
            .replace("### Northstar Fictional Bank, Testland | Jan 2024 – Present",
                     heading + "\n*Jan 2024 – Present*")
            .replace(OLD_HEADING, heading + "\n*Jan 2010 – Dec 2015*")
            .replace(CURRENT_BULLET, "- Built LangGraph agents."))
    fixed, report = apply_guardrails(base, base.replace(OLD_BULLET, "- Built LangGraph services."))
    assert report.ok, report.violations
    assert fixed == base
    assert report.warnings


@pytest.mark.parametrize("dates", ["Mar 2020 – Present", "Mar 2020 – Current", "Mar 2020 – Now", ""])
def test_current_or_undated_roles_allow_new_tools(dates):
    heading = "### Contoso Clinic, Testland" + (f" | {dates}" if dates else "")
    base = PIPE_RESUME.replace(OLD_HEADING, heading)
    draft = base.replace(OLD_BULLET, "- Built LangGraph agents.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed == draft
    assert not report.warnings


def test_date_range_inside_prose_does_not_date_an_undated_role():
    base = (PIPE_RESUME.replace(OLD_HEADING, "### Contoso Clinic, Testland")
            .replace(OLD_BULLET,
                     "Documented the legacy platform operated from Jan 2010 – Dec 2013.\n" + OLD_BULLET))
    draft = base.replace(OLD_BULLET, "- Built LangGraph agents.")
    fixed, report = apply_guardrails(base, draft)
    assert report.ok and fixed == draft
    assert not report.warnings


@pytest.mark.parametrize("move", [False, True])
def test_unchanged_bullets_copied_or_moved_into_an_older_role_are_checked(move):
    # A long moved block is classified as "equal" by SequenceMatcher.
    bullets = "\n".join(f"- Built LangGraph agents for {domain} workflows." for domain in (
        "billing", "claims", "support", "sales", "search", "catalog", "onboarding",
        "analytics", "reporting", "marketing",
    ))
    base = PIPE_RESUME.replace(CURRENT_BULLET, bullets)
    draft = base.replace(bullets, "- Built Python services.") if move else base
    draft = draft.replace(OLD_BULLET, OLD_BULLET + "\n" + bullets)
    fixed, report = apply_guardrails(base, draft)
    assert report.ok, report.violations
    assert "LangGraph" not in fixed.split(OLD_HEADING)[1]
    assert ("- Built Python services." if move else bullets) in fixed
    assert OLD_BULLET in fixed
    assert report.warnings


@pytest.mark.parametrize("polish", [False, True])
def test_published_docx_rejects_timeline_edit_and_keeps_independent_change(tmp_path, polish):
    source = tmp_path / "candidate.docx"
    document = Document()
    for text, style in [
        ("Synthetic Candidate", None),
        ("candidate@example.invalid", None),
        ("Professional Summary", "Heading 1"),
        ("Python data engineer.", None),
        ("Professional Experience", "Heading 1"),
        ("Example Systems | Jan 2020 – Dec 2023", None),
        ("Data Engineer", None),
        ("Built Python services.", "List Bullet"),
    ]:
        document.add_paragraph(text, style)
    document.save(source)
    original_bytes = source.read_bytes()
    original = extract_markdown(source)
    good = original.replace("Python data engineer.", "Python data engineer supporting analytics.")
    bad = good.replace("Built Python services.", "Built LangGraph-based Python services.")
    drafts = [good, bad] if polish else [bad]
    replies = iter(pack_model_output(changelog=["Tailored summary and experience"],
                                    match="SCORE: 95\ngood: relevant skills", resume=draft)
                   for draft in drafts)
    result = run_tailoring(
        job_description="Python analytics", resume_path=source, out_dir=tmp_path / "run",
        api_key="fake", base_url="https://example.invalid", model="test",
        complete_fn=lambda *a, **k: next(replies), two_pass=polish,
    )
    assert result["ok"], result
    output = Path(result["resume_path"])
    assert output.suffix == ".docx" and output != source
    assert extract_markdown(output) == good
    assert source.read_bytes() == original_bytes
    assert result["warnings"]
    if polish:
        assert result["polish_error"] == "The polish required automated corrections."
        assert result["match_score"] == 95
    else:
        assert result["match_score"] is None
