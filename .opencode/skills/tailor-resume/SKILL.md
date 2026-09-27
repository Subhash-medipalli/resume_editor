---
name: Tailor resume
description: Use when the user pastes a job description or asks to tailor the resume. Runs the local resume-tailor backend against the Word resume in resume/.
---

# Tailor resume

The workspace is the resume-tailor project. The source resume is the first
Word file in `resume/` (or the path the user gives). Do not rewrite the resume
yourself, and do not invent employers, dates, titles, education, or metrics.

When the user pastes a job description:

1. Save it to `out/jd.txt`.
2. From the project root, run:
   `uv run python -m resume_tailor --jd out/jd.txt`
3. Read `out/latest.json` for the run folder, then show its `CHANGELOG.md`
   (and `resume.diff` if useful).
4. The source resume is never modified; the tailored Word file is written into
   that run folder under the original file name.

If the run failed, say so and show the "Guardrail failures" section; do not hand
over a file.

If `LLM_API_KEY` is missing, tell them to put it in `.env` and do not invent a
tailored resume yourself.
