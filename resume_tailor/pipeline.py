"""Shared tailoring, evidence, and verified publication for CLI and HTTP runs."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from resume_tailor.files import atomic_output, check_output_paths, write_text
from resume_tailor.guardrails import apply_guardrails, review_edits, unified_diff
from resume_tailor.jd_profile import JDProfile, parse_jd_profile
from resume_tailor.llm import LLMError, tailor_resume
from resume_tailor.resume_profile import ResumeProfile, extract_resume_profile
from resume_tailor.role_profiles import RoleProfile, select_role_profile

_SECTION_BUDGET_LABEL = "default"


@dataclass
class PassResult:
    pass_type: str
    result: object
    tailored: str
    raw: str
    match_score: int | None
    match_line: str
    changelog: list[str]
    changed_by_checks: bool
    report: object


def validate_job_description(text) -> None:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Job description must be nonempty text.")
    if len(text) > 60_000:
        raise ValueError("Job description is too long (maximum 60,000 characters).")


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
    role_profile: RoleProfile | None = None,
    two_pass: bool = False,
    jd_profile: JDProfile | None = None,
) -> dict:
    """One isolated run; failure diagnostics never masquerade as a result."""
    progress = progress or (lambda message: None)
    sources = [resume_path, *protected_sources]
    out_dir = out_dir
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

        parsed_jd = jd_profile or parse_jd_profile(job_description)
        resume_profile = extract_resume_profile(original)
        profile = role_profile or select_role_profile(parsed_jd)
        coverage = _coverage_plan(parsed_jd, resume_profile)

        progress("Preparing the model request — factual pass")
        factual = _run_one_pass(
            resume_markdown=original,
            job_description=job_description,
            job_profile=parsed_jd,
            resume_profile=resume_profile,
            role_profile=profile,
            api_key=api_key,
            base_url=base_url,
            model=model,
            complete_fn=complete_fn,
            coverage=coverage,
            pass_type="factual",
            progress=progress,
        )

        chosen = factual
        if two_pass and factual.report.ok:
            progress("Preparing the model request — positioning pass")
            positioning = _run_one_pass(
                resume_markdown=factual.tailored,
                job_description=job_description,
                job_profile=parsed_jd,
                resume_profile=resume_profile,
                role_profile=profile,
                api_key=api_key,
                base_url=base_url,
                model=model,
                complete_fn=complete_fn,
                coverage=coverage,
                pass_type="positioning",
                progress=progress,
            )
            if positioning.report.ok and not positioning.changed_by_checks:
                chosen = positioning
            else:
                write_text(out_dir / "model.positioning.raw.txt", positioning.raw, sources=sources)

        report = chosen.report
        changed_by_checks = bool(chosen.changed_by_checks)
        score = chosen.match_score if report.ok and not changed_by_checks else None
        if chosen.pass_type != "factual":
            # Model's two-pass pass is intended as polish; keep score provenance clear.
            if report.ok:
                score_note = "Match score from the most recent successful pass."
            else:
                score_note = "Match estimate withheld because the final pass failed checks."
        else:
            score_note = "Model estimate from the factual pass." if score is not None else "Model estimate unavailable."

        coverage_violations, coverage_warnings = _evaluate_coverage_gaps(coverage, parsed_jd)
        budget_violations, budget_warnings = _evaluate_section_budget(
            review_items=report.review_items,
            section_budget=profile.section_budget_map,
        )
        report.violations.extend(coverage_violations)
        report.warnings.extend(coverage_warnings)
        report.warnings.extend(budget_warnings)

        if changed_by_checks:
            score_note = (
                "Match estimate withheld: automated checks changed the model's draft. "
                "Its assessment no longer describes the final document."
            )
            score = None
            final_match_line = (
                "Model estimate withheld because edited claims from this run could not be verified without review."
            )
        elif not report.ok:
            score_note = "Match estimate withheld because the result failed checks."
            final_match_line = "Model estimate unavailable due to failed checks."
            score = None
        else:
            final_match_line = chosen.match_line

        changes = []
        for item in report.review_items:
            action = "Added a passage" if not item["before"] else "Removed a passage" if not item["after"] else "Edited a passage"
            changes.append(f"{action} in {item['section']}.")
        if not changes:
            changes = ["No content changes from the base resume."]

        tailored = chosen.tailored
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
                score_note = match_line = final_match_line = "Match estimate withheld because document verification failed."

        if not report.ok:
            # Only this new run's outputs are removed; older runs stay intact.
            run_paths, _ = _output_paths(snapshot, out_dir)
            for key in ("word", "text"):
                if key in run_paths:
                    run_paths[key].unlink(missing_ok=True)
            written = None
            write_text(out_dir / "resume.rejected.md", tailored, sources=sources)
            if changed_by_checks:
                report.warnings.append("Model draft was adjusted by guardrails, so result may be weaker than claimed.")

        coverage_report = {
            "score": coverage.get("score", 0),
            "must_have_hits": list(coverage.get("must_have_hits", [])),
            "must_have_gaps": list(coverage.get("must_have_gaps", [])),
            "preferred_hits": list(coverage.get("preferred_hits", [])),
            "preferred_gaps": list(coverage.get("preferred_gaps", [])),
            "pass_type": chosen.pass_type,
            "matched_keywords": coverage.get("matched_keywords", []),
            "unmatched_keywords": coverage.get("unmatched_keywords", []),
        }

        changelog = _render_changelog(
            match_line=final_match_line,
            match_score=score,
            score_note=score_note,
            changelog=changes,
            report=report,
            changed_line_count=report.changed_line_count,
            resume_path=written or resume_path,
            wrote_in_place=False,
            coverage=coverage_report,
        )
        write_text(out_dir / "CHANGELOG.md", changelog, sources=sources)
        write_text(out_dir / "resume.diff", diff_text or "(no changes)\n", sources=sources)

        digest = hashlib.sha256(written.read_bytes()).hexdigest() if written else None
        if written:
            write_text(
                out_dir / "result.json",
                json.dumps(
                    {
                        "status": "review" if report.review_required else "ready",
                        "file": written.name,
                        "sha256": digest,
                        "review_items": report.review_items,
                    }
                ),
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
            "match_line": final_match_line,
            "score_note": score_note,
            "changed_lines": report.changed_line_count,
            "changes": changes,
            "run_id": out_dir.name,
            "review_required": report.review_required,
            "review_items": report.review_items,
            "sha256": digest,
            "download": None,
            "coverage": coverage_report,
            "coverage_score": coverage.get("score", 0),
            "passes": {
                "factual": {
                    "changed_by_checks": factual.changed_by_checks,
                    "ok": bool(factual.report.ok),
                },
                "positioning": None if not two_pass else {
                    "changed_by_checks": locals().get("positioning", chosen).changed_by_checks if report is not None else False,
                    "ok": locals().get("positioning", chosen).report.ok if report is not None else False,
                },
            },
            "role_profile": profile.key,
            "jd_profile": {
                "title_hints": list(parsed_jd.title_hints),
                "must_haves": list(parsed_jd.must_haves),
                "preferred": list(parsed_jd.preferred),
            },
            "resume_profile": {
                "technologies": list(resume_profile.technologies),
                "skills": list(resume_profile.skills),
                "sections": list(resume_profile.section_headings),
                "section_counts": dict(resume_profile.section_counts),
            },
        }
    except (LLMError, OSError, ValueError) as exc:
        for name in ("result.json", "resume_tailored.md", f"{resume_path.stem}_tailored.docx"):
            dest = out_dir / name
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
    job_profile: JDProfile,
    resume_profile: ResumeProfile,
    role_profile: RoleProfile,
    api_key: str,
    base_url: str,
    model: str,
    complete_fn,
    coverage: dict,
    pass_type: str,
    progress=None,
) -> PassResult:
    result = tailor_resume(
        resume_markdown=resume_markdown,
        job_description=job_description,
        api_key=api_key,
        base_url=base_url,
        model=model,
        complete_fn=complete_fn,
        progress=progress,
        jd_profile=job_profile,
        resume_profile=resume_profile,
        role_profile=role_profile,
        pass_type=pass_type,
        include_coverage=True,
        coverage=coverage,
    )
    tailored, report = apply_guardrails(resume_markdown, result.resume_markdown)
    changed_by_checks = bool(review_edits(result.resume_markdown, tailored))
    return PassResult(
        pass_type=pass_type,
        result=result,
        tailored=tailored,
        raw=result.raw,
        match_score=result.match_score,
        match_line=result.match_line,
        changelog=result.changelog if hasattr(result, "changelog") else ["No changelog provided"],
        changed_by_checks=changed_by_checks,
        report=report,
    )


def _coverage_plan(jd: JDProfile, profile: ResumeProfile) -> dict:
    lower_tech = set(_norm_term(t) for t in profile.technologies)
    lower_skills = set(_norm_term(s) for s in profile.skills)
    lower_sections = {sec.lower() for sec in profile.section_headings}
    lower_titles = {t.lower() for t in profile.titles}
    lower_text = set(_norm_term(w) for w in (*profile.business_terms, *profile.technologies, *profile.skills, *profile.titles))

    def matched(items: list[str]) -> list[str]:
        hits: list[str] = []
        misses: list[str] = []
        for term in items:
            cleaned = _norm_term(term)
            if not cleaned:
                continue
            if _matches_term(cleaned, lower_tech, lower_skills, lower_sections, lower_titles, lower_text):
                hits.append(term)
            else:
                misses.append(term)
        return hits, misses

    must_hits, must_gaps = matched(list(jd.must_haves))
    pref_hits, pref_gaps = matched(list(jd.preferred))
    score = 0
    if jd.must_haves or jd.preferred:
        denom = max(1, (len(must_hits) + len(must_gaps)) * 2 + (len(pref_hits) + len(pref_gaps)))
        raw_score = (2 * len(must_hits)) + len(pref_hits)
        score = min(100, int(round((100 * raw_score) / denom)))
    return {
        "must_have_hits": must_hits,
        "must_have_gaps": must_gaps,
        "preferred_hits": pref_hits,
        "preferred_gaps": pref_gaps,
        "score": score,
        "matched_keywords": must_hits + pref_hits,
        "unmatched_keywords": must_gaps + pref_gaps,
    }


def _evaluate_coverage_gaps(coverage: dict, jd: JDProfile) -> tuple[list[str], list[str]]:
    misses = list(coverage.get("must_have_gaps", []))
    warnings: list[str] = []
    violations = []
    if misses:
        warnings.append(
            "Must-have terms missing from profile-matched evidence: " + ", ".join(misses)
        )
        if len(misses) > 3:
            violations.append("Too many must-have requirement gaps for a reliable fit claim.")
    hits, gaps = coverage.get("preferred_hits", []), coverage.get("preferred_gaps", [])
    if gaps and len(gaps) > 8:
        warnings.append(f"Many preferred terms are missing: {len(gaps)}.")
    return violations, warnings


def _evaluate_section_budget(review_items: list[dict], section_budget: dict[str, int]) -> tuple[list[str], list[str]]:
    counts: dict[str, int] = {}
    sections_touched = set()
    for item in review_items:
        section = _normalize_section(item.get("section", "").strip() or "Other")
        before = _count_non_empty_lines(item.get("before", ""))
        after = _count_non_empty_lines(item.get("after", ""))
        changed = max(before, after) if (before or after) else 1
        counts[section] = counts.get(section, 0) + changed
        sections_touched.add(section)
    violations: list[str] = []
    warnings: list[str] = []
    for section, count in counts.items():
        max_allowed = section_budget.get(section, section_budget.get(_SECTION_BUDGET_LABEL, 2))
        if count > max_allowed:
            violations.append(
                f"Section budget exceeded for {section}: {count} lines changed but limit is {max_allowed}. "
                "Reduce flattening and focus on fewer high-signal lines."
            )
    if len(sections_touched) > 5:
        warnings.append(
            f"Changes touched {len(sections_touched)} sections; keep edits concentrated to avoid flattening."
        )
    if violations:
        warnings.extend([
            f"Section-level budget check: {section}={count}" for section, count in sorted(counts.items())
        ])
    return violations, warnings


def _normalize_section(section: str) -> str:
    return re.sub(r"\s+", " ", section).strip() or "Other"


def _count_non_empty_lines(text: str) -> int:
    return len([line for line in text.splitlines() if line.strip()])


def _matches_term(
    term: str,
    technologies: set[str],
    skills: set[str],
    sections: set[str],
    titles: set[str],
    all_text: set[str],
) -> bool:
    n = _norm_term(term)
    if n in technologies or n in skills or n in titles:
        return True
    if any(part in sections for part in n.split()):
        return True
    if n in all_text:
        return True
    for text in all_text:
        if n in text or text in n:
            return True
    return False


def _norm_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


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
    parent = None
    if resume_path.suffix.lower() in {".docx", ".doc"} and resume_path.is_file():
        parent = resume_path
    elif _in_project_resume_dir(resume_path):
        parent = find_parent_docx(root)
    if parent is not None:
        word_name = f"{parent.stem}_tailored.docx"
    paths = {key: out_dir / name for key, name in {
        "text": "resume_tailored.md", "raw": "model.raw.txt",
        "changelog": "CHANGELOG.md", "diff": "resume.diff", "rejected": "resume.rejected.md",
    }.items()}
    if parent is not None:
        paths["word"] = out_dir / word_name
    return paths, parent


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
    coverage: dict | None = None,
) -> str:
    lines = ["# Changelog", ""]
    if match_score is not None:
        lines.append(f"**Model-estimated match:** {match_score} / 100")
        lines.append("")
    if score_note:
        lines.extend([score_note, ""])
    lines.append(f"**Match:** {match_line}")
    lines.append("")
    if coverage:
        lines.append("## Coverage")
        lines.append(f"- Coverage score: {coverage.get('score', 0)}")
        if coverage.get("must_have_hits"):
            lines.append(f"- Must-have hits: {', '.join(coverage['must_have_hits'])}")
        if coverage.get("must_have_gaps"):
            lines.append(f"- Must-have gaps: {', '.join(coverage['must_have_gaps'])}")
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
        lines.append("> **The tailored file is identical to the original.** Nothing was changed.")
    lines.append("")
    return "\n".join(lines)
