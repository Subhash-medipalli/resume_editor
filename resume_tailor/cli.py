"""CLI: tailor the in-repo markdown resume against a job description."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from resume_tailor.guardrails import (
    apply_guardrails,
    check_changelog_matches_diff,
    unified_diff,
)
from resume_tailor.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMError,
    complete,
    load_dotenv,
    tailor_resume,
)

_MISSING_KEY = """\
OPENAI_API_KEY is not set.

This tool calls an OpenAI-compatible Chat Completions API.

  export OPENAI_API_KEY=sk-...
  # optional:
  export OPENAI_BASE_URL=https://api.openai.com/v1
  export OPENAI_MODEL=gpt-4o-mini

You can also put those in a .env file in the current directory
(see .env.example). Never commit .env.
"""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(Path(".env"))

    if getattr(args, "serve", False):
        from resume_tailor.server import serve
        serve(port=args.port)
        return 0

    if args.reset:
        return _reset(args)

    if not args.jd:
        sys.stderr.write("error: --jd is required (or pass --reset / --serve).\n")
        return 2

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(_MISSING_KEY)
        return 2

    try:
        job_description = _read_jd(args.jd)
        resume_path = _resolve_resume(args.resume)
        _ensure_original_backup(resume_path)
        resume_markdown = _load_source_text(resume_path)
    except OSError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if not resume_markdown.strip():
        sys.stderr.write(f"Resume is empty: {resume_path}\n")
        return 1

    base_url = (
        args.base_url
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = args.model or os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    out_dir = Path(args.out)

    try:
        result = tailor_resume(
            resume_markdown=resume_markdown,
            job_description=job_description,
            api_key=api_key,
            base_url=base_url,
            model=model,
            complete_fn=complete,
        )
    except LLMError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    tailored, report = apply_guardrails(resume_markdown, result.resume_markdown)
    contradictions = check_changelog_matches_diff(
        result.changelog, report.changed_line_count
    )
    if contradictions:
        report.violations.extend(contradictions)
        report.ok = False
    diff_text = unified_diff(
        resume_markdown,
        tailored,
        fromfile=str(resume_path),
        tofile=str(resume_path),
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    # The untouched model reply: the only way to tell a model that echoed the
    # resume from a pipeline that dropped its edits.
    (out_dir / "model.raw.txt").write_text(result.raw, encoding="utf-8")
    written_path = None
    if report.ok:
        written_path = _write_outputs(
            tailored=tailored,
            resume_path=resume_path,
            out_dir=out_dir,
        )
        sys.stdout.write(f"Wrote {written_path}\n")
        drift = verify_output(written_path, tailored)
        if drift:
            report.violations.extend(drift)
            report.ok = False
    else:
        rejected = out_dir / "resume.rejected.md"
        rejected.write_text(tailored, encoding="utf-8")
        sys.stdout.write(f"Left source resume unchanged (guardrails failed)\n")
        sys.stdout.write(f"Wrote {rejected}\n")

    changelog_body = _render_changelog(
        match_line=result.match_line,
        match_score=result.match_score,
        changelog=result.changelog,
        report=report,
        changed_line_count=report.changed_line_count,
        resume_path=written_path or resume_path,
        wrote_in_place=False,
        score_inferred=result.score_inferred,
    )

    changelog_out = out_dir / "CHANGELOG.md"
    diff_out = out_dir / "resume.diff"
    changelog_out.write_text(changelog_body, encoding="utf-8")
    diff_out.write_text(diff_text if diff_text else "(no changes)\n", encoding="utf-8")

    sys.stdout.write(f"Wrote {changelog_out}\n")
    sys.stdout.write(f"Wrote {diff_out}\n")
    sys.stdout.write("\n")
    sys.stdout.write(changelog_body)
    if not diff_text:
        sys.stdout.write("\n(no textual diff — resume identical to base)\n")

    if not report.ok:
        sys.stderr.write(
            "\nGuardrails failed. Changelog and diff were written for review; "
            "the source resume was not overwritten. Do not send this resume as-is.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m resume_tailor",
        description=(
            "Make surgical edits to the parent Word resume for a job "
            "description. Writes a NEW tailored .docx plus a changelog and "
            "unified diff to out/. The parent resume is never modified."
        ),
    )
    parser.add_argument(
        "--jd",
        default=None,
        help="Path to a job-description text file, or - to read stdin. Required unless --reset.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Path to the source resume (default: the parent .docx). Never modified.",
    )
    parser.add_argument(
        "--out",
        default="out",
        help="Directory for changelog and unified diff (default: out/).",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Open a localhost UI to paste a job description (default http://127.0.0.1:8787).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port for --serve (default: 8787).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Restore resume/base.md from the immutable backup resume/base.original.md.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Model name (default: OPENAI_MODEL or {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"OpenAI-compatible API base URL (default: OPENAI_BASE_URL or {DEFAULT_BASE_URL}).",
    )
    return parser.parse_args(argv)


def _read_jd(spec: str) -> str:
    if spec == "-":
        isatty = getattr(sys.stdin, "isatty", None)
        if callable(isatty) and isatty():
            sys.stderr.write("Paste the job description, then Ctrl-D.\n")
        text = sys.stdin.read()
    else:
        text = Path(spec).read_text(encoding="utf-8")
    text = text.strip()
    if not text:
        raise ValueError("Job description is empty.")
    return text


def _resolve_resume(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ValueError(f"Resume not found: {path}")
        return path
    # base.md is optional — the parent .docx is the real source of truth, and
    # deleting the stale markdown copy must not break the tool. Each root is
    # resolved fully before falling back to the next, so the working directory
    # always wins over the installed package location.
    from resume_tailor.docx_io import find_parent_docx

    for root in (Path.cwd(), Path(__file__).resolve().parent.parent):
        path = root / "resume" / "base.md"
        if path.is_file():
            return path
        parent = find_parent_docx(root)
        if parent is not None:
            return parent
    raise ValueError(
        "Could not find a resume in resume/ (expected a .docx, or base.md). "
        "Pass --resume PATH or run from the repo root."
    )


def _original_path(resume_path: Path) -> Path:
    if resume_path.name == "base.md":
        return resume_path.with_name("base.original.md")
    return resume_path.with_name(resume_path.name + ".original")


def _ensure_original_backup(resume_path: Path) -> None:
    """On first run, snapshot the resume next to it as an immutable backup."""
    original = _original_path(resume_path)
    if original.exists():
        return
    shutil.copyfile(resume_path, original)


def _reset(args: argparse.Namespace) -> int:
    try:
        resume_path = _resolve_resume(args.resume)
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    original = _original_path(resume_path)
    if not original.is_file():
        sys.stderr.write(
            f"No backup found at {original}. "
            "Run the tailor once first (it copies the current resume on first run).\n"
        )
        return 1
    shutil.copyfile(original, resume_path)
    sys.stdout.write(f"Restored {resume_path} from {original}\n")
    return 0


def _load_source_text(resume_path: Path) -> str:
    # Structured markdown, not a flat dump: the model needs to see bullets and
    # sections, and every guardrail below keys off the heading markers.
    if resume_path.suffix.lower() in {".docx", ".doc"}:
        from resume_tailor.docx_io import extract_markdown
        return extract_markdown(resume_path)
    if _in_project_resume_dir(resume_path):
        from resume_tailor.docx_io import extract_markdown, find_parent_docx
        parent = find_parent_docx(Path(__file__).resolve().parent.parent)
        if parent is not None:
            return extract_markdown(parent)
    return resume_path.read_text(encoding="utf-8")


def _in_project_resume_dir(resume_path: Path) -> bool:
    root = Path(__file__).resolve().parent.parent / "resume"
    try:
        resume_path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _write_outputs(*, tailored: str, resume_path: Path, out_dir: Path) -> Path:
    """Write a NEW file. Never overwrite the parent Word resume or source markdown."""
    text_out = out_dir / "resume_tailored.md"
    text_out.write_text(tailored, encoding="utf-8")
    try:
        from resume_tailor.docx_io import find_parent_docx, write_tailored_docx
    except ImportError:
        return text_out
    root = Path(__file__).resolve().parent.parent
    parent_docx = None
    if resume_path.suffix.lower() in {".docx", ".doc"} and resume_path.is_file():
        parent_docx = resume_path
    elif _in_project_resume_dir(resume_path):
        parent_docx = find_parent_docx(root)
    if parent_docx is not None:
        dest = out_dir / f"{parent_docx.stem}_tailored.docx"
        write_tailored_docx(parent_docx, tailored, dest)
        return dest
    return text_out


def verify_output(written_path, tailored: str) -> list[str]:
    """Check the delivered .docx really contains the approved text."""
    if written_path is None or written_path.suffix.lower() != ".docx":
        return []
    from resume_tailor.docx_io import verify_written_docx

    return verify_written_docx(written_path, tailored)


def _render_changelog(
    *,
    match_line,
    changelog,
    report,
    changed_line_count: int,
    resume_path: Path,
    wrote_in_place: bool,
    match_score=None,
    score_inferred: bool = False,
) -> str:
    lines = ["# Changelog", ""]
    if match_score is not None:
        note = " _(inferred from wording — the model gave no score)_" if score_inferred else ""
        lines.append(f"**Match score:** {match_score} / 100{note}")
        lines.append("")
    lines.append(f"**Match:** {match_line}")
    lines.append("")
    if report.ok:
        lines.append(f"Wrote new file `{resume_path}` (parent resume left unchanged).")
        lines.append("")
    else:
        lines.append(f"Left `{resume_path}` unchanged because guardrails failed.")
        lines.append("")
    if not report.ok:
        lines.extend(["## Guardrail failures", ""])
        lines.extend(f"- {item}" for item in report.violations)
        lines.append("")
    if report.warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {item}" for item in report.warnings)
        lines.append("")
    lines.extend(["## What changed", ""])
    lines.extend(f"- {item}" for item in changelog)
    lines.append("")
    lines.append(f"_Lines changed (content): {changed_line_count}._")
    if changed_line_count == 0:
        lines.append("")
        lines.append(
            "> **The tailored file is identical to the original.** Nothing was changed."
        )
    lines.append("")
    return "\n".join(lines)
