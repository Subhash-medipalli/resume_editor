import pytest

from resume_tailor.cli import main
from resume_tailor.llm import TailorResult, parse_model_output
from tests.helpers import SAMPLE_RESUME, lightly_tailored, pack_model_output


def test_missing_api_key_exits_2(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    code = main(["--jd", str(tmp_path / "nope.txt")])
    assert code == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err
    assert "OPENAI_BASE_URL" in err
    assert "OPENAI_MODEL" in err


def test_cli_writes_new_file_and_leaves_source_untouched(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
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
    assert (out / "CHANGELOG.md").is_file()
    assert (out / "resume.diff").is_file()
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME, (
        "the source resume must never be modified"
    )
    written = (out / "resume_tailored.md").read_text(encoding="utf-8")
    assert "Northwind Platform Co. (SAMPLE)" in written
    assert "Jul 2023" in written
    assert "not-a-real-person@example.invalid" in written
    assert "AWS-hosted platform work" in written
    changelog = (out / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Retargeted summary" in changelog
    assert "**Match:**" in changelog
    assert "parent resume left unchanged" in changelog
    diff = (out / "resume.diff").read_text(encoding="utf-8")
    assert "AWS-hosted platform work" in diff
    assert "Retargeted summary" in captured.out
    assert "resume_tailored" in captured.out


def test_cli_reads_jd_from_stdin(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
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
    assert "Light keyword pass" in (out / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "AWS-hosted platform work" in (
        out / "resume_tailored.md"
    ).read_text(encoding="utf-8")
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME


def test_guardrail_failure_does_not_overwrite_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
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
    changelog = (out / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "Guardrail failures" in changelog
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME
    assert (out / "resume.rejected.md").is_file()
    assert (out / "resume.diff").is_file()


def test_first_run_copies_immutable_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resume = resume_dir / "base.md"
    original = resume_dir / "base.original.md"
    jd = tmp_path / "jd.txt"
    out = tmp_path / "out"
    resume.write_text(SAMPLE_RESUME, encoding="utf-8")
    jd.write_text("Python contractor\n", encoding="utf-8")
    tailored = lightly_tailored(SAMPLE_RESUME)
    raw = pack_model_output(
        changelog=["Light keyword pass"],
        match="partial: light alignment",
        resume=tailored,
    )
    monkeypatch.setattr("resume_tailor.cli.complete", lambda *a, **k: raw)

    assert not original.exists()
    code = main(["--jd", str(jd), "--resume", str(resume), "--out", str(out)])
    assert code == 0
    assert original.is_file()
    assert original.read_text(encoding="utf-8") == SAMPLE_RESUME
    assert "AWS-hosted platform work" in (
        out / "resume_tailored.md"
    ).read_text(encoding="utf-8")

    # Second run must not clobber the backup.
    code = main(["--jd", str(jd), "--resume", str(resume), "--out", str(out)])
    assert code == 0
    assert original.read_text(encoding="utf-8") == SAMPLE_RESUME


def test_reset_restores_from_original(monkeypatch, tmp_path, capsys):
    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    resume = resume_dir / "base.md"
    original = resume_dir / "base.original.md"
    original.write_text(SAMPLE_RESUME, encoding="utf-8")
    resume.write_text("tampered\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main(["--reset", "--resume", str(resume)])
    assert code == 0
    assert resume.read_text(encoding="utf-8") == SAMPLE_RESUME
    assert "Restored" in capsys.readouterr().out


def test_reset_without_backup_fails(monkeypatch, tmp_path, capsys):
    resume = tmp_path / "base.md"
    resume.write_text(SAMPLE_RESUME, encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = main(["--reset", "--resume", str(resume)])
    assert code == 1
    assert "No backup" in capsys.readouterr().err


def test_tailor_result_roundtrip_used_by_cli():
    raw = pack_model_output(
        changelog=["x"],
        match="good: y",
        resume=SAMPLE_RESUME,
    )
    result = parse_model_output(raw)
    assert isinstance(result, TailorResult)
    assert result.changelog == ["x"]


def test_resume_resolves_to_the_parent_docx_when_base_md_is_absent(tmp_path, monkeypatch):
    """base.md is a stale copy; deleting it must not break the tool."""
    pytest.importorskip("docx")
    from docx import Document

    from resume_tailor.cli import _resolve_resume

    resume_dir = tmp_path / "resume"
    resume_dir.mkdir()
    document = Document()
    document.add_paragraph("Alex Placeholder (SAMPLE)")
    document.save(str(resume_dir / "Someone_resume.docx"))

    monkeypatch.chdir(tmp_path)
    assert _resolve_resume(None).suffix == ".docx"
