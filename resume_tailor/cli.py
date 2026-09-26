"""CLI: create a tailored resume draft for a job description."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from resume_tailor.files import check_output_paths, new_run_dir, write_text
from resume_tailor.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    complete,
    load_dotenv,
)
from resume_tailor.pipeline import run_tailoring

_MISSING_KEY = """\
LLM_API_KEY is not set.

Put your model settings in a .env file in this folder (see .env.example):

  LLM_API_KEY=<your OpenRouter key>
  LLM_BASE_URL=https://openrouter.ai/api/v1
  LLM_MODEL=google/gemini-3.8-flash

Never commit .env.
"""


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv(Path(".env"))

    if args.serve:
        from resume_tailor.server import serve
        serve(port=args.port)
        return 0

    if not args.jd:
        sys.stderr.write("error: --jd is required (or pass --serve).\n")
        return 2

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        sys.stderr.write(_MISSING_KEY)
        return 2

    try:
        job_description = _read_jd(args.jd)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    out_dir = Path(args.out)
    base_url = (
        args.base_url
        or os.environ.get("LLM_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = args.model or os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL

    try:
        resume_path = _resolve_resume(args.resume)
        protected = [resume_path] + ([Path(args.jd)] if args.jd != "-" else [])
        check_output_paths(protected, [out_dir / "latest.json"])
        run_dir = new_run_dir(out_dir)
        sys.stdout.write(f"Run files: {run_dir}\n")
        write_text(out_dir / "latest.json", json.dumps({"run": str(run_dir), "status": "working"}), sources=protected)
        result = run_tailoring(
            job_description=job_description, resume_path=resume_path, out_dir=run_dir,
            api_key=api_key, base_url=base_url, model=model,
            complete_fn=complete,
            protected_sources=protected,
            progress=lambda message: print(message, file=sys.stderr),
            two_pass=args.two_pass,
        )
        write_text(
            out_dir / "latest.json",
            json.dumps(
                {
                    "run": str(run_dir),
                    "status": "draft" if result["ok"] else "failed",
                    "resume": result.get("resume_path") if result["ok"] else None,
                    "coverage": result.get("coverage"),
                    "polish_error": result.get("polish_error"),
                }
            ),
            sources=protected,
        )
        if result["ok"]:
            sys.stdout.write(f"Wrote tailored resume: {result['resume_path']}\n")
        else:
            sys.stderr.write(f"Run failed: {result.get('error') or '; '.join(result['violations'])}\n")
        sys.stdout.write(result.get("changelog", ""))
        return 0 if result["ok"] else 1
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m resume_tailor",
        description=(
            "Tailor a Word resume for a job description. Writes a NEW tailored "
            ".docx plus a changelog and unified diff to out/runs/<run-id>/. "
            "The source resume is never modified."
        ),
    )
    parser.add_argument(
        "--jd",
        default=None,
        help="Path to a job-description text file, or - to read stdin.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        help="Path to the source resume (default: the first .docx in resume/). Never modified.",
    )
    parser.add_argument(
        "--two-pass",
        action="store_true",
        help="Run the tailoring pass, then a polish pass.",
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
        "--model",
        default=None,
        help=f"Model name (default: LLM_MODEL or {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Chat Completions API base URL (default: LLM_BASE_URL or {DEFAULT_BASE_URL}).",
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
    # Resolve each root fully so a resume in the working directory wins over
    # the package location.
    from resume_tailor.docx_io import find_parent_docx

    for root in (Path.cwd(), Path(__file__).resolve().parent.parent):
        parent = find_parent_docx(root)
        if parent is not None:
            return parent
    raise ValueError(
        "No resume found. Put your Word resume (.docx) in the resume/ folder, "
        "attach it in the app, or pass --resume PATH."
    )


if __name__ == "__main__":
    raise SystemExit(main())
