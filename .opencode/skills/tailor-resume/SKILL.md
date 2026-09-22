---
name: Tailor resume
description: Use when the user pastes a job description or asks to tailor a resume. Runs the local resume-tailor backend against an explicit .docx with surgical edits only.
---

# Tailor resume

The workspace is the resume-tailor project. `main` has no built-in resume.
The user must supply a `.docx` (`--resume PATH`, or an upload / saved base in
the UI). The previous personal snapshot lives only on the `sravya` branch.
Do not rewrite the resume. Do not invent employers, dates, titles, education,
or metrics.

When the user pastes a job description:

1. Save it to `out/jd.txt`.
2. From the project root, run:
   `uv run python -m resume_tailor --jd out/jd.txt --resume path/to/resume.docx`
3. Read `out/latest.json` for this run's state and directory. Show that directory's
   `CHANGELOG.md` (and `resume.diff` if useful).
4. On success, the source resume is never modified; the result is a draft at
   `out/runs/<run-id>/<stem>_tailored.docx`. Review each before/after passage in the
   changelog against the candidate's actual experience before using it.

An existing file in `out/` is not proof that the latest run succeeded. Check the
CLI exit status, `latest.json`, and this run's changelog before handing over a
draft. Both entry points use separate `out/runs/<run-id>/` directories. Browser
runs require factual review before their download links become available.
Scores are estimates; do not invent a missing score or reuse one that was
withheld after checks changed the model's draft. Display the final diff-derived
summary, not the model's raw explanation.

Check `_Lines changed (content)_` in the changelog. If it is 0 while the
changelog lists edits, the run failed — say so and do not hand over the file.

If `OPENAI_API_KEY` is missing, tell them to put it in `.env` and do not invent a
tailored resume yourself.
