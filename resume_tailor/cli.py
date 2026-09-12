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
    _load_source_text, _original_path, _output_paths, _write_outputs,
    _render_changelog, run_tailoring,
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
        sys.stderr.write("error: --jd is required (or pass --serve / --reset).\n")
        return 2

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
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
        or os.environ.get("OPENAI_BASE_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    model = args.model or os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL

    batch_mode = bool(args.batch or args.resumes)
    try:
        if batch_mode:
            result = _run_batch(
                job_description=job_description,
                resumes=args.resumes,
                resume=args.resume,
                jd_spec=args.jd,
                out_dir=out_dir,
                api_key=api_key,
                base_url=base_url,
                model=model,
                two_pass=args.two_pass,
            )
            write_text(
                out_dir / "latest.json",
                json.dumps({
                    "batch": str(result["batch_id"]),
                    "status": "batch-complete",
                    "results": result["results"],
                }),
                sources=[],
            )
            for item in result["results"]:
                if item.get("ok"):
                    sys.stdout.write(f"Ranked run: {item['resume']} score={item.get('coverage_score')} match={item.get('match_score')}\n")
                else:
                    sys.stdout.write(f"Run failed: {item['resume']}\n")
            return 0 if any(item.get("ok") for item in result["results"]) else 1

        resume_path = _resolve_resume(args.resume)
        _run_single_preflight(resume_path, out_dir, args)
        from resume_tailor.files import new_run_dir
        run_dir = new_run_dir(out_dir)

        legacy = [p for p in _output_paths(resume_path, out_dir)[0].values() if p.exists() or p.is_symlink()]
        if legacy:
            archive = run_dir / "previous-output"
            archive.mkdir()
            for path in legacy:
                path.rename(archive / path.name)

        sys.stdout.write(f"Run files: {run_dir}\n")
        write_text(out_dir / "latest.json", json.dumps({"run": str(run_dir), "status": "working"}), sources=[])
        result = run_tailoring(
            job_description=job_description, resume_path=resume_path, out_dir=run_dir,
            api_key=api_key, base_url=base_url, model=model,
            complete_fn=complete,
            protected_sources=[resume_path],
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
                    "coverage_score": result.get("coverage_score"),
                    "passes": result.get("passes"),
                }
            ),
            sources=[resume_path],
        )
        if result["ok"]:
            sys.stdout.write(f"Wrote draft for factual review: {result['resume_path']}\n")
        else:
            sys.stderr.write(f"Run failed: {result.get('error') or '; '.join(result['violations'])}\n")
        sys.stdout.write(result.get("changelog", ""))
        return 0 if result["ok"] else 1
    except OSError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1


def _run_single_preflight(resume_path: Path, out_dir: Path, args: argparse.Namespace) -> None:
    paths, parent_docx = _output_paths(resume_path, out_dir)
    protected = [resume_path, _original_path(resume_path)]
    if parent_docx is not None:
        protected.append(parent_docx)
    if args.jd != "-":
        protected.append(Path(args.jd))
    check_output_paths(protected, [*paths.values(), out_dir / "latest.json"])
    _ensure_original_backup(resume_path)


def _run_batch(
    *,
    job_description: str,
    resumes: list[str],
    resume: str | None,
    jd_spec: str | None,
    out_dir: Path,
    api_key: str,
    base_url: str,
    model: str,
    two_pass: bool,
) -> dict:
    from resume_tailor.files import new_run_dir

    resume_paths = []
    if resume:
        resume_paths.append(_resolve_resume(resume))
    for path in resumes:
        resume_paths.append(_resolve_resume(path))
    if not resume_paths:
        raise ValueError("No resumes provided for batch mode.")

    seen = set()
    unique = []
    for path in resume_paths:
        real = path.resolve()
        if real in seen:
            continue
        seen.add(real)
        unique.append(path)

    batch_root = new_run_dir(out_dir)
    results = []
    for idx, resume_path in enumerate(unique, start=1):
        _ensure_original_backup(resume_path)
        run_dir = batch_root / f"{idx:02d}-{resume_path.stem}"
        run_dir.mkdir()
        _run_single_preflight(
            resume_path=resume_path,
            out_dir=run_dir,
            args=argparse.Namespace(
                jd=jd_spec if jd_spec is not None else "-",
                resume=str(resume_path),
            ),
        )
        protected = [resume_path, _original_path(resume_path)]
        try:
            result = run_tailoring(
                job_description=job_description,
                resume_path=resume_path,
                out_dir=run_dir,
                api_key=api_key,
                base_url=base_url,
                model=model,
                complete_fn=complete,
                protected_sources=protected,
                progress=lambda message: print(message, file=sys.stderr),
                two_pass=two_pass,
            )
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "error": str(exc), "run_id": run_dir.name}

        result = dict(result)
        result.setdefault("resume", str(resume_path))
        result.setdefault("run_id", run_dir.name)
        result["batch_position"] = idx
        results.append(result)

    ranked = sorted(
        results,
        key=lambda row: (
            row.get("coverage_score", -1),
            row.get("match_score", -1) if row.get("match_score") is not None else -1,
            bool(row.get("ok")),
        ),
        reverse=True,
    )
    batch_summary = {
        "batch_id": str(batch_root),
        "results": ranked,
        "job_description_length": len(job_description),
        "resume_count": len(ranked),
    }
    write_text(batch_root / "batch-summary.json", json.dumps(batch_summary, indent=2), sources=[])
    return {
        "batch_id": batch_root,
        "results": ranked,
    }


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
        "--resumes",
        action="append",
        default=[],
        help="Additional resume paths for batch mode. Use with --batch.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="Run all resumes in a batch and rank the outputs.",
    )
    parser.add_argument(
        "--two-pass",
        action="store_true",
        help="Run a factual pass then a positioning polish pass.",
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


if __name__ == "__main__":
    raise SystemExit(main())
