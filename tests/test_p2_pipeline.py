import json
from pathlib import Path

import pytest
from docx import Document

from resume_tailor.cli import main
from resume_tailor.docx_io import extract_markdown, verify_written_docx
from resume_tailor.guardrails import extract_facts
from resume_tailor.llm import LLMError
from resume_tailor.pipeline import run_tailoring
from tests.helpers import SAMPLE_RESUME, lightly_tailored, pack_model_output


def run(tmp_path, raw, *, job_description="Python"):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    return run_tailoring(job_description=job_description, resume_path=source,
                         out_dir=tmp_path / "run", api_key="fake", base_url="https://example.invalid",
                         model="test", complete_fn=lambda *a, **k: raw)


def test_changes_list_is_the_models_summary(tmp_path):
    raw = pack_model_output(changelog=["Retargeted the summary", "Echoed scoped platform work in a bullet"],
                            match="SCORE: 73\npartial: model estimate", resume=lightly_tailored(SAMPLE_RESUME))
    result = run(tmp_path, raw)
    assert result["ok"] and result["changed_lines"] == 2
    assert result["changes"] == ["Retargeted the summary", "Echoed scoped platform work in a bullet"]
    assert "Retargeted the summary" in result["changelog"]
    assert result["match_score"] == 73
    assert "Model estimate" in result["score_note"]


def test_score_and_model_assessment_are_withheld_after_a_line_is_put_back(tmp_path):
    changed = lightly_tailored(SAMPLE_RESUME).replace("AWS-hosted platform work.", "AWS-hosted platform work across 12 services.")
    raw = pack_model_output(changelog=["Added scale to the summary"], match="SCORE: 99\ngood: 12 services at scale", resume=changed)
    result = run(tmp_path, raw)
    assert result["ok"] and result["changed_lines"] > 0, result
    assert result["match_score"] is None
    assert "withheld" in result["score_note"]
    assert "12 services" not in result["match_line"]
    assert "undone by the safety checks" in result["changes"][-1]
    assert "12 services" not in Path(result["resume_path"]).read_text()


def test_cli_failure_points_to_its_own_run_and_preserves_prior_result(tmp_path, monkeypatch):
    source, jd, out = tmp_path / "base.md", tmp_path / "jd.txt", tmp_path / "out"
    source.write_text(SAMPLE_RESUME); jd.write_text("Python")
    raw = pack_model_output(changelog=["Edited summary"], match="good: partial alignment", resume=lightly_tailored(SAMPLE_RESUME))
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr("resume_tailor.cli.complete", lambda *a, **k: raw)
    args = ["--jd", str(jd), "--resume", str(source), "--out", str(out)]
    assert main(args) == 0
    first = json.loads((out / "latest.json").read_text())
    previous = Path(first["resume"])
    before = previous.read_bytes()
    def failure(*a, **k):
        raise LLMError("provider unavailable")
    monkeypatch.setattr("resume_tailor.cli.complete", failure)
    assert main(args) == 1
    latest = json.loads((out / "latest.json").read_text())
    assert latest["status"] == "failed" and latest["resume"] is None
    assert latest["run"] != first["run"]
    assert previous.read_bytes() == before
    assert not (Path(latest["run"]) / "result.json").exists()
    assert "provider unavailable" in (Path(latest["run"]) / "CHANGELOG.md").read_text()
    assert source.read_text() == SAMPLE_RESUME


def test_unsupported_word_layout_is_rejected_before_provider_work(tmp_path):
    document = Document()
    document.add_paragraph("Sample Candidate")
    document.add_table(rows=1, cols=1).cell(0, 0).text = "Experience hidden in a table"
    source = tmp_path / "source.docx"; document.save(source)
    def no_model(*a, **k):
        raise AssertionError("must validate the layout first")
    result = run_tailoring(job_description="Python", resume_path=source, out_dir=tmp_path / "out",
                           api_key="fake", base_url="https://example.invalid", model="test", complete_fn=no_model)
    assert not result["ok"] and "Unsupported Word layout" in result["error"]
    assert not list((tmp_path / "out").glob("*.docx"))


def test_missing_jd_keywords_warn_instead_of_blocking(tmp_path):
    raw = pack_model_output(changelog=["Retargeted summary"], match="SCORE: 20\npoor: requirements remain uncovered",
                            resume=lightly_tailored(SAMPLE_RESUME))
    result = run(tmp_path, raw, job_description="Requirements:\n- Terraform\n- Kubernetes\n- Scala\n- SparkR")
    assert result["ok"], result
    assert not result["violations"]
    assert result["coverage"]["missing"] == ["Kubernetes", "Scala", "SparkR", "Terraform"]
    assert any("JD keywords not found" in warning for warning in result["warnings"])


def test_match_first_writes_verified_docx_with_adjacent_capabilities(tmp_path):
    document = Document()
    document.add_paragraph("Sample Data Candidate")
    document.add_paragraph("sample@example.invalid | +1-555-0110")
    document.add_paragraph("Professional Summary", "Heading 1")
    document.add_paragraph("Data scientist focused on Python machine-learning models.")
    document.add_paragraph("Skills", "Heading 1")
    document.add_paragraph("Python, SQL, XGBoost", "List Bullet")
    document.add_paragraph("Professional Experience", "Heading 1")
    document.add_paragraph("Example Analytics LLC | Jan 2022 – Present")
    document.add_paragraph("Data Scientist")
    document.add_paragraph("Developed machine-learning models for product analytics.", "List Bullet")
    document.add_paragraph("Education", "Heading 1")
    document.add_paragraph("M.S. Data Science — Example University, 2021")
    source = tmp_path / "base.docx"
    document.save(source)
    source_bytes = source.read_bytes()
    base = extract_markdown(source)
    tailored = (
        base.replace(
            "- Python, SQL, XGBoost",
            "- Python, SQL, XGBoost, REST APIs, SSO, OIDC, Rate Limiting, Terraform",
        )
        .replace(
            "- Developed machine-learning models for product analytics.",
            "- Experience with ML-backed REST APIs, SSO/OIDC integration, rate limiting, and Terraform workflows.",
        )
    )
    jd = """Title: Data Scientist
Requirements:
- Python
- SQL
- XGBoost
- REST APIs
- SSO
- OIDC
- Rate Limiting
- Terraform
Responsibilities:
- Support ML systems and adjacent software integrations"""
    raw = pack_model_output(
        changelog=["Added adjacent software capabilities requested by the role"],
        match="SCORE: 90\ngood: broad data-science and software coverage",
        resume=tailored,
    )
    result = run_tailoring(
        job_description=jd,
        resume_path=source,
        out_dir=tmp_path / "run",
        api_key="fake",
        base_url="https://example.invalid",
        model="test",
        complete_fn=lambda *a, **k: raw,
    )
    output = Path(result["resume_path"])
    approved = extract_markdown(output)
    assert result["ok"], result
    assert output.name == "base.docx" and output.is_file() and output != source
    assert all(term in approved for term in ("REST APIs", "SSO", "OIDC", "Rate Limiting", "Terraform"))
    assert result["coverage"]["score"] == 100
    assert extract_facts(approved).companies == extract_facts(base).companies
    assert extract_facts(approved).titles == extract_facts(base).titles
    assert extract_facts(approved).date_spans == extract_facts(base).date_spans
    assert verify_written_docx(output, tailored) == []
    assert source.read_bytes() == source_bytes


def test_unchanged_model_output_is_not_published(tmp_path):
    raw = pack_model_output(changelog=["No changes"], match="SCORE: 100\ngood: already aligned", resume=SAMPLE_RESUME)
    result = run(tmp_path, raw)
    assert not result["ok"]
    assert "without any content changes" in result["error"]
    assert result["download"] is None
    assert not (tmp_path / "run/result.json").exists()
    assert not (tmp_path / "run/resume_tailored.md").exists()


def test_identity_only_changes_cannot_pass_as_tailoring(tmp_path):
    raw = pack_model_output(changelog=["Updated identity"], match="good: aligned",
                            resume=SAMPLE_RESUME.replace("not-a-real-person@example.invalid", "changed@example.invalid"))
    result = run(tmp_path, raw)
    assert not result["ok"]
    assert any("No content changes survived" in text for text in result["violations"])
    assert not (tmp_path / "run/result.json").exists()


def test_two_pass_diff_and_counts_use_original_resume(tmp_path):
    from resume_tailor.guardrails import apply_guardrails
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    first = lightly_tailored(SAMPLE_RESUME)
    second = first.replace("Python REST APIs", "reliable Python REST APIs")
    replies = iter([pack_model_output(changelog=["First changes"], match="good: first", resume=first),
                    pack_model_output(changelog=["Polished summary"], match="good: polish", resume=second)])
    result = run_tailoring(job_description="Python API reliability", resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    _, expected = apply_guardrails(SAMPLE_RESUME, second)
    assert result["ok"], result
    assert result["changed_lines"] == expected.changed_line_count == 2
    assert result["review_items"] == expected.review_items
    manifest = json.loads((tmp_path / "run/result.json").read_text())
    assert manifest["status"] == "ready"


def test_optional_polish_reverting_all_edits_keeps_first_pass(tmp_path):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    first = lightly_tailored(SAMPLE_RESUME)
    replies = iter([pack_model_output(changelog=["First changes"], match="good: first", resume=first),
                    pack_model_output(changelog=["Polished"], match="good: polish", resume=SAMPLE_RESUME)])
    result = run_tailoring(job_description="Python APIs", resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    assert result["ok"], result
    assert Path(result["resume_path"]).read_text() == first
    assert result["polish_error"]
    assert any("Kept the completed first pass" in text for text in result["warnings"])


def test_optional_polish_provider_failure_keeps_first_pass(tmp_path):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    calls = []
    def complete(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise LLMError("provider unavailable during polish")
        return pack_model_output(changelog=["Updated summary"], match="good: first", resume=lightly_tailored(SAMPLE_RESUME))
    result = run_tailoring(job_description="Python APIs", resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=complete, two_pass=True)
    assert result["ok"] and "provider unavailable" in result["polish_error"]


def test_polish_losing_one_of_many_keywords_keeps_the_first_pass(tmp_path):
    """199/200 and 200/200 both round to 100%; every matched term must remain."""
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    tools = [f"Tool{n}" for n in range(1, 201)]
    first = lightly_tailored(SAMPLE_RESUME).replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis\n- Tools: " + ", ".join(tools))
    polished = first.replace(", Tool200", "").replace("Python REST APIs", "reliable Python REST APIs")
    replies = iter([pack_model_output(changelog=["Added tools"], match="good: first", resume=first),
                    pack_model_output(changelog=["Polished"], match="good: polish", resume=polished)])
    result = run_tailoring(job_description="Required: " + ", ".join(tools), resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    assert result["ok"] and result["polish_error"] == "The polish removed job keywords."
    assert "Tool200" in Path(result["resume_path"]).read_text()


def test_coverage_requires_whole_terms_and_unknown_is_not_zero():
    from resume_tailor.pipeline import jd_keywords, keyword_coverage
    text = "# Test\nJavaScript engineer with C++, React, and AWS."
    coverage = keyword_coverage(["Java", "C++", "React Native", "AWS"], text)
    assert coverage["matched"] == ["C++", "AWS"]
    assert coverage["missing"] == ["Java", "React Native"]
    assert keyword_coverage(jd_keywords("friendly team"), text)["score"] is None


def test_sample_jd_keywords_are_skills_not_boilerplate():
    """The old parser found no must-haves in this JD and scored a matching resume 0."""
    from resume_tailor.pipeline import jd_keywords
    keywords = set(jd_keywords((Path(__file__).resolve().parent.parent / "examples/sample_jd.txt").read_text()))
    assert {"Python", "Java", "Docker", "Kubernetes", "LangChain", "Pinecone", "FAISS", "SageMaker"} <= keywords
    assert not {"Engineer", "Must-haves", "Title"} & keywords
    assert {"C#", "C++", "NET"} <= set(jd_keywords("Required: C#, .NET Core, and C++ (Dallas, TX; W2 only)"))
    assert {"CI/CD", "APIs", "AI"} <= set(jd_keywords("Required: Python, CI/CD, APIs, AI"))


def test_polish_that_drops_job_keywords_keeps_the_first_pass(tmp_path):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    first = lightly_tailored(SAMPLE_RESUME).replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis, Kafka")
    polished = first.replace("Redis, Kafka", "Redis").replace("Python REST APIs", "reliable Python REST APIs")
    replies = iter([pack_model_output(changelog=["Added Kafka"], match="good: first", resume=first),
                    pack_model_output(changelog=["Polished"], match="good: polish", resume=polished)])
    result = run_tailoring(job_description="Required: Python, Kafka", resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    assert result["ok"] and result["polish_error"] == "The polish removed job keywords."
    assert result["coverage"]["score"] == 100 and "Kafka" in Path(result["resume_path"]).read_text()


@pytest.mark.parametrize("replacement", ["RabbitMQ", "RabbitMQ, Pulsar"],
                         ids=["same-count-swap", "higher-count-with-loss"])
def test_polish_cannot_trade_a_matched_keyword_for_new_keywords(tmp_path, replacement):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    first = lightly_tailored(SAMPLE_RESUME).replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis, Kafka")
    polished = first.replace("Redis, Kafka", f"Redis, {replacement}")
    replies = iter([pack_model_output(changelog=["Added Kafka"], match="good: first", resume=first),
                    pack_model_output(changelog=["Polished skills"], match="good: polish", resume=polished)])
    result = run_tailoring(job_description="Required: Python, Kafka, RabbitMQ, Pulsar",
                           resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    assert result["ok"] and result["polish_error"] == "The polish removed job keywords."
    assert Path(result["resume_path"]).read_text() == first
    assert result["coverage"]["matched"] == ["Kafka", "Python"]


def test_polish_can_retain_keywords_with_different_case_and_add_more(tmp_path):
    source = tmp_path / "base.md"
    source.write_text(SAMPLE_RESUME)
    first = lightly_tailored(SAMPLE_RESUME).replace("- Data: PostgreSQL, Redis", "- Data: PostgreSQL, Redis, Kafka")
    polished = first.replace("Redis, Kafka", "Redis, kafka, RabbitMQ")
    replies = iter([pack_model_output(changelog=["Added Kafka"], match="good: first", resume=first),
                    pack_model_output(changelog=["Added RabbitMQ"], match="good: polish", resume=polished)])
    result = run_tailoring(job_description="Required: Python, Kafka, RabbitMQ",
                           resume_path=source, out_dir=tmp_path / "run",
                           api_key="fake", base_url="https://example.invalid", model="test",
                           complete_fn=lambda *a, **k: next(replies), two_pass=True)
    assert result["ok"] and result["polish_error"] is None
    assert Path(result["resume_path"]).read_text() == polished
    assert result["coverage"]["score"] == 100
