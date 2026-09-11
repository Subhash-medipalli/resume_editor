"""Shared tailoring, evidence, and verified publication for CLI and HTTP runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from resume_tailor.files import atomic_output, check_output_paths, write_text
from resume_tailor.guardrails import apply_guardrails, check_changelog_matches_diff, review_edits, unified_diff
from resume_tailor.llm import LLMError, tailor_resume


def validate_job_description(text) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Job description must be nonempty text.")
    if len(text) > 60_000:
        raise ValueError("Job description is too long (maximum 60,000 characters).")


def run_tailoring(*, job_description: str, resume_path: Path, out_dir: Path,
                 api_key: str, base_url: str, model: str, complete_fn,
                 protected_sources=(), progress=None) -> dict:
    """One isolated run; failure diagnostics never masquerade as a result."""
    progress = progress or (lambda message: None)
    sources = [resume_path, *protected_sources]
    try:
        validate_job_description(job_description)
        paths, parent = _output_paths(resume_path, out_dir)
        source = parent or resume_path
        sources.append(source)
        if source.suffix.lower() == ".doc":
            raise ValueError("Legacy .doc files are not supported. Save the resume as a Word .docx file.")
        snapshot = out_dir / "source" / source.name
        check_output_paths(sources, [*paths.values(), out_dir / "result.json"])
        if snapshot.resolve() != source.resolve():
            with atomic_output(snapshot, sources=sources) as staging:
                staging.write_bytes(source.read_bytes())
        progress("Checking the resume layout")
        if snapshot.suffix.lower() == ".docx":
            from resume_tailor.docx_io import validate_layout
            validate_layout(snapshot)
        original = _load_source_text(snapshot)
        if not original.strip():
            raise ValueError("The base resume is empty.")
        progress("Preparing the model request")
        result = tailor_resume(resume_markdown=original, job_description=job_description,
                               api_key=api_key, base_url=base_url, model=model,
                               complete_fn=complete_fn, progress=progress)
        progress("Checking edits against the source")
        tailored, report = apply_guardrails(original, result.resume_markdown)
        # Only retain the lost-edit check. The published summary is derived from
        # final evidence, so unsupported model explanations never reach it.
        if report.changed_line_count == 0:
            report.violations.extend(check_changelog_matches_diff(result.changelog, 0))
            report.ok = not report.violations
        changed_by_checks = bool(review_edits(result.resume_markdown, tailored))
        score = result.match_score if report.ok and not changed_by_checks else None
        if changed_by_checks:
            score_note = "Match estimate withheld: automated checks changed the model's draft. Its assessment no longer describes the final document."
        elif not report.ok:
            score_note = "Match estimate withheld because the result failed checks."
        elif score is None:
            score_note = "Match estimate unavailable: the model did not supply a valid numeric score."
        else:
            score_note = "Model estimate only; this is not a measured or independently verified match."
        match_line = result.match_line if report.ok and not changed_by_checks else score_note
        changes = []
        for item in report.review_items:
            action = "Added a passage" if not item["before"] else "Removed a passage" if not item["after"] else "Edited a passage"
            changes.append(f"{action} in {item['section']}.")
        if not changes:
            changes = ["No content changes from the base resume."]
        diff_text = unified_diff(original, tailored, fromfile=str(snapshot), tofile="tailored resume")
        write_text(out_dir / "model.raw.txt", result.raw, sources=sources)
        written = None
        if report.ok:
            progress("Writing and verifying the Word document")
            try:
                written = _write_outputs(tailored=tailored, resume_path=snapshot, out_dir=out_dir)
            except (OSError, ValueError) as exc:
                report.violations.append(str(exc))
                report.ok = False
                score = None
                score_note = match_line = "Match estimate withheld because document verification failed."
        if not report.ok:
            # Only this new run's outputs are removed; older runs stay intact.
            run_paths, _ = _output_paths(snapshot, out_dir)
            for key in ("word", "text"):
                if key in run_paths:
                    run_paths[key].unlink(missing_ok=True)
            written = None
            write_text(out_dir / "resume.rejected.md", tailored, sources=sources)
        changelog = _render_changelog(match_line=match_line, match_score=score,
                                     score_note=score_note, changelog=changes, report=report,
                                     changed_line_count=report.changed_line_count,
                                     resume_path=written or resume_path, wrote_in_place=False)
        write_text(out_dir / "CHANGELOG.md", changelog, sources=sources)
        write_text(out_dir / "resume.diff", diff_text or "(no changes)\n", sources=sources)
        digest = hashlib.sha256(written.read_bytes()).hexdigest() if written else None
        if written:
            write_text(out_dir / "result.json", json.dumps({
                "status": "review" if report.review_required else "ready",
                "file": written.name, "sha256": digest, "review_items": report.review_items,
            }), sources=sources)
        return {"ok": report.ok, "changelog": changelog, "diff": diff_text,
                "violations": report.violations, "warnings": report.warnings,
                "resume_path": str(written or resume_path), "match_score": score,
                "match_line": match_line, "score_note": score_note,
                "changed_lines": report.changed_line_count, "changes": changes,
                "run_id": out_dir.name, "review_required": report.review_required,
                "review_items": report.review_items, "sha256": digest, "download": None}
    except (LLMError, OSError, ValueError) as exc:
        for name in ("result.json", "resume_tailored.md", f"{resume_path.stem}_tailored.docx"):
            dest = out_dir / name
            check_output_paths(sources, [dest])
            dest.unlink(missing_ok=True)
        error = f"Run failed: {exc}\nSource resume left unchanged. No result was published.\n"
        write_text(out_dir / "CHANGELOG.md", error, sources=sources)
        return {"ok": False, "error": str(exc), "changelog": error, "download": None,
                "run_id": out_dir.name}


def _original_path(resume_path: Path) -> Path:
    if resume_path.name == "base.md":
        return resume_path.with_name("base.original.md")
    return resume_path.with_name(resume_path.name + ".original")


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


def _output_paths(resume_path: Path, out_dir: Path) -> tuple[dict[str, Path], Path | None]:
    from resume_tailor.docx_io import find_parent_docx

    root = Path(__file__).resolve().parent.parent
    parent_docx = None
    if resume_path.suffix.lower() in {".docx", ".doc"} and resume_path.is_file():
        parent_docx = resume_path
    elif _in_project_resume_dir(resume_path):
        parent_docx = find_parent_docx(root)
    if parent_docx is not None:
        word_name = f"{parent_docx.stem}_tailored.docx"
    paths = {key: out_dir / name for key, name in {
        "text": "resume_tailored.md", "raw": "model.raw.txt",
        "changelog": "CHANGELOG.md", "diff": "resume.diff", "rejected": "resume.rejected.md",
    }.items()}
    if parent_docx is not None:
        paths["word"] = out_dir / word_name
    return paths, parent_docx


def _write_outputs(*, tailored: str, resume_path: Path, out_dir: Path) -> Path:
    """Validate every destination before writing; publish only verified Word text."""
    from resume_tailor.docx_io import write_tailored_docx

    paths, parent = _output_paths(resume_path, out_dir)
    sources = [resume_path, _original_path(resume_path)]
    if parent is not None:
        sources.append(parent)
    check_output_paths(sources, list(paths.values()))
    if parent is not None:
        write_tailored_docx(parent, tailored, paths["word"])
    write_text(paths["text"], tailored, sources=sources)
    return paths.get("word", paths["text"])


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
    score_note: str = "",
) -> str:
    lines = ["# Changelog", ""]
    if match_score is not None:
        lines.append(f"**Model-estimated match:** {match_score} / 100")
        lines.append("")
    if score_note:
        lines.extend([score_note, ""])
    lines.append(f"**Match:** {match_line}")
    lines.append("")
    if report.ok:
        lines.append(f"Wrote new file `{resume_path}` (parent resume left unchanged).")
        lines.append("")
        if report.review_required:
            lines.extend(["**Draft — factual review required before use.** Automated checks cannot prove that reworded claims are true.", ""])
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
    if report.review_items:
        lines.extend(["## Review against the source", ""])
        for item in report.review_items:
            lines.extend([f"### {item['section']} — source lines {item['source_lines']}", "",
                          "Before:", "", item["before"] or "(No corresponding source passage.)", "",
                          "After:", "", item["after"] or "(Removed.)", ""])
    lines.extend(["## What changed" if report.ok else "## Proposed changes (not published)", ""])
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
