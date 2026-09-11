"""CLI: create a resume draft for review against a job description."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from resume_tailor.files import atomic_output, check_output_paths, write_text

from resume_tailor.pipeline import (
    _original_path, _load_source_text, _output_paths, _write_outputs,
    _render_changelog, verify_output, run_tailoring,
)
from resume_tailor.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    complete,
    load_dotenv,
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
        out_dir = Path(args.out)
        paths, parent_docx = _output_paths(resume_path, out_dir)
        protected = [resume_path, _original_path(resume_path)]
        if parent_docx is not None:
            protected.append(parent_docx)
        if args.jd != "-":
            protected.append(Path(args.jd))
        check_output_paths(protected, [*paths.values(), out_dir / "latest.json"])
        _ensure_original_backup(resume_path)
    except OSError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    base_url = (
        args.base_url
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = args.model or os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    out_dir = Path(args.out)

    from resume_tailor.files import new_run_dir
    run_dir = new_run_dir(out_dir)
    legacy = [p for p in paths.values() if p.exists() or p.is_symlink()]
    if legacy:
        archive = run_dir / "previous-output"
        archive.mkdir()
        for path in legacy:
            path.rename(archive / path.name)
    sys.stdout.write(f"Run files: {run_dir}\n")
    write_text(out_dir / "latest.json", json.dumps({"run": str(run_dir), "status": "working"}), sources=protected)
    result = run_tailoring(
        job_description=job_description, resume_path=resume_path, out_dir=run_dir,
        api_key=api_key, base_url=base_url, model=model, complete_fn=complete,
        protected_sources=protected, progress=lambda message: print(message, file=sys.stderr),
    )
    write_text(out_dir / "latest.json", json.dumps({"run": str(run_dir), "status": "draft" if result["ok"] else "failed", "resume": result.get("resume_path") if result["ok"] else None}), sources=protected)
    if result["ok"]:
        sys.stdout.write(f"Wrote draft for factual review: {result['resume_path']}\n")
    else:
        sys.stderr.write(f"Run failed: {result.get('error') or '; '.join(result['violations'])}\n")
    sys.stdout.write(result.get("changelog", ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m resume_tailor",
        description=(
            "Make surgical edits to the parent Word resume for a job "
            "description. Writes a NEW tailored .docx plus a changelog and "
            "unified diff to out/runs/<run-id>/. The parent resume is never modified."
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
        help="Root for separate run folders and latest.json (default: out/).",
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
        help="Restore the selected source resume from its first-run backup.",
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
    # Prefer the source Word file to a legacy markdown copy. Resolve each root
    # fully so a resume in the working directory wins over the package location.
    from resume_tailor.docx_io import find_parent_docx

    for root in (Path.cwd(), Path(__file__).resolve().parent.parent):
        parent = find_parent_docx(root)
        if parent is not None:
            return parent
        path = root / "resume" / "base.md"
        if path.is_file():
            return path
    raise ValueError(
        "Could not find a resume in resume/ (expected a .docx, or base.md). "
        "Pass --resume PATH or run from the repo root."
    )


def _ensure_original_backup(resume_path: Path) -> None:
    """On first run, snapshot the resume next to it as an immutable backup."""
    original = _original_path(resume_path)
    if original.exists():
        return
    with atomic_output(original, sources=[resume_path]) as staging:
        shutil.copyfile(resume_path, staging)


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
    with atomic_output(resume_path, sources=[original]) as staging:
        shutil.copyfile(original, staging)
    sys.stdout.write(f"Restored {resume_path} from {original}\n")
    return 0
