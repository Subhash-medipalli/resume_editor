---
name: Tailor resume
description: Use when the user pastes a job description or asks to tailor the resume. Runs the local resume-tailor backend against the parent Word resume with surgical edits only.
---

# Tailor resume

The workspace is the resume-tailor project. The source of truth is the parent
Word file `resume/Sravya_M_resume.docx` — not `resume/base.md`, which is a stale
copy the tool does not read. Do not rewrite the resume. Do not invent employers,
dates, titles, education, or metrics.

When the user pastes a job description:

1. Save it to `out/jd.txt`.
2. From the project root, run:
   `.venv/bin/python -m resume_tailor --jd out/jd.txt`
3. Show `out/CHANGELOG.md` (and `out/resume.diff` if useful).
4. The parent resume is never modified; the result is a new file at
   `out/Sravya_M_resume_tailored.docx`.

Check `_Lines changed (content)_` in the changelog. If it is 0 while the
changelog lists edits, the run failed — say so and do not hand over the file.

If `OPENAI_API_KEY` is missing, tell them to put it in `.env` and do not invent a
tailored resume yourself.
