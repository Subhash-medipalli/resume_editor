"""Shared tailoring, evidence, and verified publication for CLI and HTTP runs."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from resume_tailor.files import atomic_output, check_output_paths, write_text
from resume_tailor.guardrails import GuardrailReport, apply_guardrails, review_edits, unified_diff
from resume_tailor.llm import LLMError, has_resume_changes, tailor_resume

# Tool and technology names are shaped unlike ordinary prose: internal capitals
# (GitOps, PySpark), short all-caps acronyms (AWS, EKS), or a digit (S3, EC2).
_TECH_TOKEN_RE = re.compile(
    r"\b(?:[A-Za-z]+[A-Z][A-Za-z0-9+#.]*|[A-Z]{2,8}|[A-Za-z]+[0-9]+[A-Za-z0-9+#]*)\b"
)
# Capitalised JD boilerplate that names no skill: headers, job titles, sentence-
# starting verbs, degrees, work authorization, and US state codes.
_JD_NOISE = frozenset("""
a about all also an and any are as at be benefits bonus but by can candidate candidates
client company compensation contract day days description details duration each employer
employment equal experience for from have hiring hybrid ideal if in interview is it its job
key local location looking must new nice no not note of on onsite only opportunity or our
overview pay please plus position preferred qualifications rate remote required requirements
responsibilities duties role salary seeking should skill skills summary that the their these
this those title to type we what when where who why will with work years you your
senior sr junior jr lead principal staff engineer engineers developer developers architect
analyst consultant specialist administrator manager director scientist tester backend frontend
front end full stack fullstack platform software data cloud team teams databases database
tools technologies languages frameworks environment environments
build design develop implement collaborate maintain support create ensure participate provide
manage drive own write deliver partner mentor help join strong excellent good great solid
orchestrate ingest monitor automate optimize migrate deploy integrate troubleshoot document
coordinate analyze review define establish improve communicate translate
proven deep hands working comfortable familiar familiarity knowledge understanding ability
able responsible
w2 c2c c2h 1099 h1b h1 gc ead usc opt cpt visa citizen citizens usa united states us uk eu
into ci cd phd bs ba ms msc bsc mba cv hr pm sme eod asap faq tbd sop
al ak az ar ca co ct de fl ga hi id il ia ks ky la me md ma mi mn ms mo mt ne nv nh nj nm ny
nc nd oh ok pa ri sc sd tn tx ut vt va wa wv wi wy dc
""".split())


@dataclass
class PassResult:
    tailored: str
    raw: str
    match_score: int | None
    match_line: str
    changelog: list[str]
    changed_by_checks: bool
    report: GuardrailReport


def validate_job_description(text) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Job description must be nonempty text.")
    if len(text) > 60_000:
        raise ValueError("Job description is too long (maximum 60,000 characters).")


def jd_keywords(job_description: str) -> list[str]:
    """Skill-shaped words in the JD: CamelCase, ACRONYMS, CI/CD, C#, Capitalised names.

    ponytail: a shape-based guess, so a few non-skills (city names) can show up as
    missing, and one-letter languages (R, C) are not detected. Swap for a curated
    skills list if the missing lists look noisy.
    """
    words = {token.strip(".") for token in _TECH_TOKEN_RE.findall(job_description)}
    words |= set(re.findall(r"\b[A-Z][a-z][A-Za-z0-9+#.]*[A-Za-z0-9+#]\b", job_description))
    words |= set(re.findall(r"(?<![\w+#/])(?:[A-Z]{2,5}/[A-Z]{2,5}|C\+\+|C#|F#)(?![\w+#/])", job_description))
    return sorted({word for word in words if len(word) > 1 and word.lower() not in _JD_NOISE}, key=str.lower)


def keyword_coverage(keywords: list[str], text: str) -> dict:
    """Which JD keywords appear as whole terms (case-insensitive) in `text`."""
    matched = [k for k in keywords if re.search(rf"(?<![\w+#]){re.escape(k)}(?![\w+#])", text, re.I)]
    missing = [k for k in keywords if k not in matched]
    score = round(100 * len(matched) / len(keywords)) if keywords else None
    return {"score": score, "matched": matched, "missing": missing}


def run_tailoring(
    *,
    job_description: str,
    resume_path: Path,
    out_dir: Path,
    api_key: str,
    base_url: str,
    model: str,
    complete_fn,
    protected_sources=(),
    progress=None,
    two_pass: bool = False,
) -> dict:
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

        keywords = jd_keywords(job_description)
        model_args = dict(job_description=job_description, keywords=keywords, api_key=api_key,
                          base_url=base_url, model=model, complete_fn=complete_fn, progress=progress)
        progress("Preparing the model request")
        first = _run_one_pass(resume_markdown=original, **model_args)
        chosen, polish_error = first, None
        if two_pass and first.report.ok:
            progress("Preparing the model request — polish pass")
            try:
                polished = _run_one_pass(resume_markdown=first.tailored, validation_base=original, **model_args)
                if not polished.report.ok:
                    polish_error = "; ".join(polished.report.violations)
                elif polished.changed_by_checks:
                    polish_error = "The polish required automated corrections."
                elif (set(keyword_coverage(keywords, first.tailored)["matched"])
                      - set(keyword_coverage(keywords, polished.tailored)["matched"])):
                    polish_error = "The polish removed job keywords."
                else:
                    chosen = polished
                if polish_error:
                    write_text(out_dir / "model.positioning.raw.txt", polished.raw, sources=sources)
            except LLMError as exc:
                polish_error = str(exc)
            if polish_error:
                first.report.warnings.append(
                    f"Kept the completed first pass because the optional polish failed: {polish_error}"
                )

        report, tailored = chosen.report, chosen.tailored
        coverage = keyword_coverage(keywords, tailored)
        if coverage["missing"]:
            report.warnings.append(
                "JD keywords not found in the final resume (some may not be skills): "
                + ", ".join(coverage["missing"])
            )
        if not report.ok:
            score, match_line = None, "Model estimate unavailable due to failed checks."
            score_note = "Match estimate withheld because the result failed checks."
        elif chosen.changed_by_checks:
            score, match_line = None, "Model estimate withheld because automated checks changed the draft."
            score_note = ("Match estimate withheld: automated checks changed the model's draft, "
                          "so its assessment no longer describes the final document.")
        else:
            score, match_line = chosen.match_score, chosen.match_line
            score_note = ("Model estimate: the model grading its own rewrite." if score is not None
                          else "Model estimate unavailable.")

        changes = list(chosen.changelog)
        if chosen.changed_by_checks:
            changes.append("Some of these edits were undone by the safety checks; see the warnings.")

        diff_text = unified_diff(original, tailored, fromfile=str(snapshot), tofile="tailored resume")
        write_text(out_dir / "model.raw.txt", chosen.raw, sources=sources)
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
            write_text(out_dir / "resume.rejected.md", tailored, sources=sources)

        changelog = _render_changelog(
            match_line=match_line, match_score=score, score_note=score_note, changes=changes,
            report=report, resume_path=written or resume_path, coverage=coverage,
        )
        write_text(out_dir / "CHANGELOG.md", changelog, sources=sources)
        write_text(out_dir / "resume.diff", diff_text or "(no changes)\n", sources=sources)

        digest = hashlib.sha256(written.read_bytes()).hexdigest() if written else None
        if written:
            write_text(
                out_dir / "result.json",
                json.dumps({"status": "ready", "file": written.name, "sha256": digest,
                            "review_items": report.review_items}),
                sources=sources,
            )

        return {
            "ok": report.ok,
            "changelog": changelog,
            "diff": diff_text,
            "violations": report.violations,
            "warnings": report.warnings,
            "resume_path": str(written or resume_path),
            "match_score": score,
            "match_line": match_line,
            "score_note": score_note,
            "changed_lines": report.changed_line_count,
            "changes": changes,
            "run_id": out_dir.name,
            "review_items": report.review_items,
            "sha256": digest,
            "download": None,
            "coverage": coverage,
            "polish_error": polish_error,
        }
    except (LLMError, OSError, ValueError) as exc:
        paths, _ = _output_paths(resume_path, out_dir)
        for dest in (out_dir / "result.json", paths["text"], paths.get("word")):
            if dest is not None:
                check_output_paths(sources, [dest])
                dest.unlink(missing_ok=True)
        error = f"Run failed: {exc}\nSource resume left unchanged. No result was published.\n"
        write_text(out_dir / "CHANGELOG.md", error, sources=sources)
        return {
            "ok": False,
            "error": str(exc),
            "changelog": error,
            "download": None,
            "run_id": out_dir.name,
        }


def _run_one_pass(
    *,
    resume_markdown: str,
    job_description: str,
    keywords: list[str],
    api_key: str,
    base_url: str,
    model: str,
    complete_fn,
    progress=None,
    validation_base: str | None = None,
) -> PassResult:
    base = validation_base if validation_base is not None else resume_markdown
    result = tailor_resume(
        resume_markdown=resume_markdown,
        job_description=job_description,
        api_key=api_key,
        base_url=base_url,
        model=model,
        complete_fn=complete_fn,
        progress=progress,
        missing_keywords=keyword_coverage(keywords, resume_markdown)["missing"],
        polish=validation_base is not None,
    )
    tailored, report = apply_guardrails(base, result.resume_markdown)
    if not has_resume_changes(base, tailored):
        report.violations.append("No content changes survived validation; no tailored resume was produced.")
        report.ok = False
    return PassResult(
        tailored=tailored,
        raw=result.raw,
        match_score=result.match_score,
        match_line=result.match_line,
        changelog=result.changelog,
        changed_by_checks=bool(review_edits(result.resume_markdown, tailored)),
        report=report,
    )


def _load_source_text(resume_path: Path) -> str:
    # Structured markdown, not a flat dump: the model needs to see bullets and
    # sections, and every guardrail below keys off the heading markers.
    if resume_path.suffix.lower() in {".docx", ".doc"}:
        from resume_tailor.docx_io import extract_markdown
        return extract_markdown(resume_path)
    return resume_path.read_text(encoding="utf-8")


def _output_paths(resume_path: Path, out_dir: Path) -> tuple[dict[str, Path], Path | None]:
    parent = None
    if resume_path.suffix.lower() in {".docx", ".doc"} and resume_path.is_file():
        parent = resume_path
    paths = {key: out_dir / name for key, name in {
        "text": "resume_tailored.md", "raw": "model.raw.txt",
        "changelog": "CHANGELOG.md", "diff": "resume.diff", "rejected": "resume.rejected.md",
    }.items()}
    if parent is not None:
        # Keep the original name so the recruiter never sees "tailored"; a
        # lowercase extension keeps "Resume.DOCX" downloadable.
        paths["word"] = out_dir / f"{parent.stem}.docx"
    return paths, parent


def _write_outputs(*, tailored: str, resume_path: Path, out_dir: Path) -> Path:
    """Validate every destination before writing; publish only verified Word text."""
    from resume_tailor.docx_io import write_tailored_docx

    paths, parent = _output_paths(resume_path, out_dir)
    check_output_paths([resume_path], list(paths.values()))
    if parent is not None:
        write_tailored_docx(parent, tailored, paths["word"])
    write_text(paths["text"], tailored, sources=[resume_path])
    return paths.get("word", paths["text"])


def _render_changelog(
    *,
    match_line: str,
    match_score: int | None,
    score_note: str,
    changes: list[str],
    report: GuardrailReport,
    resume_path: Path,
    coverage: dict,
) -> str:
    lines = ["# Changelog", ""]
    if match_score is not None:
        lines += [f"**Model-estimated match:** {match_score} / 100", ""]
    if score_note:
        lines += [score_note, ""]
    lines += [f"**Match:** {match_line}", ""]
    score = f"{coverage['score']}%" if coverage["score"] is not None else "unavailable"
    lines += ["## Keyword coverage", "", f"- JD keywords found in the final resume: {score}"]
    if coverage["missing"]:
        lines.append(f"- Missing: {', '.join(coverage['missing'])}")
    lines.append("")
    if report.ok:
        lines += [f"Wrote new file `{resume_path}` (parent resume left unchanged).", ""]
    else:
        lines += [f"Left `{resume_path}` unchanged because guardrails failed.", "", "## Guardrail failures", ""]
        lines += [f"- {item}" for item in report.violations]
        lines.append("")
    if report.warnings:
        lines += ["## Warnings", ""]
        lines += [f"- {item}" for item in report.warnings]
        lines.append("")
    if report.review_items:
        lines += ["## Review against the source", ""]
        for item in report.review_items:
            lines += [f"### {item['section']} — source lines {item['source_lines']}", "",
                      "Before:", "", item["before"] or "(No corresponding source passage.)", "",
                      "After:", "", item["after"] or "(Removed.)", ""]
    lines += ["## What changed (model's summary)" if report.ok else "## Proposed changes (not published)", ""]
    lines += [f"- {item}" for item in changes]
    lines += ["", f"_Lines changed (content): {report.changed_line_count}._"]
    if report.changed_line_count == 0:
        lines += ["", "> **The tailored file is identical to the original.** Nothing was changed."]
    lines.append("")
    return "\n".join(lines)
