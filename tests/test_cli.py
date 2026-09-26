import json
from pathlib import Path

import pytest

from resume_tailor.cli import main
from resume_tailor.llm import TailorResult, parse_model_output
from tests.helpers import SAMPLE_RESUME, lightly_tailored, pack_model_output


def run_output(out, name):
    return Path(json.loads((out / "latest.json").read_text())["run"]) / name


def test_missing_api_key_exits_2(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["--jd", str(tmp_path / "nope.txt")])
    assert code == 2
    err = capsys.readouterr().err
    assert "LLM_API_KEY" in err
    assert "LLM_BASE_URL" in err
    assert "LLM_MODEL" in err


def test_cli_writes_new_file_and_leaves_source_untouched(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-real")
    resume = tmp_path / "resume.md"
    jd = tmp_path / "jd.txt"
    out = tmp_path / "out"
    resume.write_text(SAMPLE_RESUME, encoding="utf-8")
    jd.write_text("Need a Python/AWS contractor for REST APIs on EC2.\n", encoding="utf-8")

    tailored = lightly_tailored(SAMPLE_RESUME)
    raw = pack_model_output(
        changelog=[
            "Retargeted summary toward AWS-hosted Python REST API work already on the resume",
            "Tweaked the most recent Northwind bullet to echo scoped platform language from the JD",
        ],
        match="good: JD matches Python/AWS contracting already evidenced",
        resume=tailored,
    )

    def fake_complete(messages, **kwargs):
        assert messages[0]["role"] == "system"
        assert "MATCH-FIRST RULES" in messages[0]["content"]
        assert "Never invent employers" in messages[0]["content"]
        assert "Python/AWS contractor" in messages[1]["content"]
        assert "Northwind Platform Co." in messages[1]["content"]
        return raw

    monkeypatch.setattr("resume_tailor.cli.complete", fake_complete)

    code = main(
        [
            "--jd",
            str(jd),
            "--resume",
            str(resume),
            "--out",
            str(out),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert not (out / "resume.md").exists()
    assert run_output(out, "CHANGELOG.md").is_file()
    assert run_output(out, "resume.diff").is_file()
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME, (
        "the source resume must never be modified"
    )
    written = run_output(out, "resume_tailored.md").read_text(encoding="utf-8")
    assert "Northwind Platform Co. (SAMPLE)" in written
    assert "Jul 2023" in written
    assert "not-a-real-person@example.invalid" in written
    assert "AWS-hosted platform work" in written
    changelog = run_output(out, "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Retargeted summary" in changelog
    assert "**Match:**" in changelog
    assert "parent resume left unchanged" in changelog
    diff = run_output(out, "resume.diff").read_text(encoding="utf-8")
    assert "AWS-hosted platform work" in diff
    assert "Retargeted summary" in captured.out
    assert "resume_tailored" in captured.out


def test_cli_reads_jd_from_stdin(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-real")
    resume = tmp_path / "resume.md"
    resume.write_text(SAMPLE_RESUME, encoding="utf-8")
    out = tmp_path / "out"
    tailored = lightly_tailored(SAMPLE_RESUME)
    raw = pack_model_output(
        changelog=["Light keyword pass"],
        match="partial: only light alignment",
        resume=tailored,
    )
    monkeypatch.setattr("resume_tailor.cli.complete", lambda *a, **k: raw)
    monkeypatch.setattr("resume_tailor.cli.sys.stdin", type("S", (), {"isatty": lambda self: False, "read": lambda self: "Python contractor\n"})())

    code = main(["--jd", "-", "--resume", str(resume), "--out", str(out)])
    assert code == 0
    assert "Light keyword pass" in run_output(out, "CHANGELOG.md").read_text(encoding="utf-8")
    assert "AWS-hosted platform work" in (
        run_output(out, "resume_tailored.md")
    ).read_text(encoding="utf-8")
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME


def test_guardrail_failure_does_not_overwrite_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-not-real")
    resume = tmp_path / "resume.md"
    jd = tmp_path / "jd.txt"
    out = tmp_path / "out"
    resume.write_text(SAMPLE_RESUME, encoding="utf-8")
    jd.write_text("anything\n", encoding="utf-8")

    invented = SAMPLE_RESUME + (
        "\n### Secret Agent — Spectre Holdings (FAKE)\n"
        "*Jan 2010 – Dec 2012* · Moon\n"
    )
    raw = pack_model_output(
        changelog=["Added a prior job"],
        match="poor: forced fit",
        resume=invented,
    )
    monkeypatch.setattr("resume_tailor.cli.complete", lambda *a, **k: raw)

    code = main(["--jd", str(jd), "--resume", str(resume), "--out", str(out)])
    assert code == 1
    changelog = run_output(out, "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Guardrail failures" in changelog
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME
    assert run_output(out, "resume.rejected.md").is_file()
    assert run_output(out, "resume.diff").is_file()








def test_tailor_result_roundtrip_used_by_cli():
    raw = pack_model_output(
        changelog=["x"],
        match="good: y",
        resume=SAMPLE_RESUME,
    )
    result = parse_model_output(raw)
    assert isinstance(result, TailorResult)
    assert result.changelog == ["x"]




def test_default_resume_is_the_first_word_file_not_markdown(tmp_path, monkeypatch):
    from docx import Document

    from resume_tailor.cli import _resolve_resume
    from resume_tailor.pipeline import _load_source_text

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    for name, identity in (
        ("a_candidate.docx", "First Candidate (SAMPLE)"),
        ("b_candidate.docx", "Second Candidate (SAMPLE)"),
    ):
        document = Document()
        document.add_paragraph(identity)
        document.save(resume_dir / name)
    (resume_dir / "base.md").write_text("Outdated markdown", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    source = _resolve_resume(None)
    assert source == resume_dir / "a_candidate.docx"
    assert "First Candidate (SAMPLE)" in _load_source_text(source)


def test_source_named_like_latest_json_is_protected(tmp_path, monkeypatch, capsys):
    source, jd = tmp_path / "latest.json", tmp_path / "jd.txt"
    source.write_text(SAMPLE_RESUME)
    jd.write_text("Python APIs")
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setattr("resume_tailor.cli.complete", lambda *a, **k: pytest.fail("must protect source before calling model"))
    code = main(["--resume", str(source), "--jd", str(jd), "--out", str(tmp_path)])
    assert code == 1 and "protected source" in capsys.readouterr().err
    assert source.read_text() == SAMPLE_RESUME




def test_explicit_project_markdown_is_not_replaced_by_default_word(tmp_path, monkeypatch):
    from resume_tailor import pipeline
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    source = resume_dir / "custom.md"
    source.write_text(SAMPLE_RESUME)
    monkeypatch.setattr(pipeline, "__file__", str(tmp_path / "resume_tailor/pipeline.py"))
    assert pipeline._load_source_text(source) == SAMPLE_RESUME
    paths, parent = pipeline._output_paths(source, tmp_path / "out")
    assert parent is None and "word" not in paths
