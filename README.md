# Resume tailor

Paste a job description and download a Word resume rewritten for that role. The app rewrites the headline, summary, skills, and experience bullets, and adds the job's skills and tools even when the original resume does not mention them. Employers, job titles, dates, education, certifications, contact details, and numbers stay exactly as they are in the original.

The original file is never changed. Each run writes into its own folder, and the saved Word file is checked against the approved text before it can be downloaded.

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the project folder:

```bash
uv sync
cp .env.example .env
```

Set the API key, base URL, and model in `.env` (see `.env.example`). Any service that speaks the standard Chat Completions format works (OpenRouter, NVIDIA, and others); set the URL and model together when changing providers. Do not commit `.env`.

Attach your Word resume in the app. Uploaded resumes are saved locally so you can select them again for later job descriptions. The browser always uses the resume you explicitly select. For CLI runs, you can also keep your own Word resume in the ignored `resume/` folder.

```bash
uv run python -m resume_tailor --serve
```

Open [the local app](http://127.0.0.1:8787), select or attach a resume, paste the job description, and choose **Tailor resume**. Progress and provider retries appear while the run is active. An optional second pass polishes the first result. Recent jobs retain their own verified download links.

## What the checks do

- **Skills and wording change freely.** Missing job keywords are sent to the model as editing targets. There is no edit budget.
- **Work history is protected.** Changed employers, job titles, dates, named clients/projects, or qualification-section contents fail validation with no download. A new degree claim in editable prose restores that line while retaining other valid edits; employer names containing words such as Education do not change this behavior.
- **Numbers stay with their role.** If an edit adds, changes, or drops a detected quantity (including "5-person" teams and "2-hour" windows), that line keeps its original wording and the run continues. "11+ years" and "over 11 years" count as the same claim.
- **The prompt asks for a believable timeline.** It places the job's main platform (for example AWS) in the most recent role, a comparable alternative (for example Azure) in the role before it, and keeps older roles on their own tools. Platform distribution is model guidance, not a deterministic check.
- **Known tool release years are checked per role.** A line that adds a tool released after its role ended (for example AWS Glue in a job that ended in 2015) keeps its original wording; an added bullet is dropped. Copied bullets and hyphenated wording such as "LangGraph-based" are checked too. A tool already named in that same original role is exempt.
- **Certifications cannot be assembled from unrelated credentials.** A detected new certification claim keeps its original line. Each claimed credential must match one original credential; mentioning a real certification elsewhere, such as in the summary, is fine.
- **Contact details**, including separate LinkedIn, GitHub, and website links, are restored from the original when altered.
- **Polish preserves every matched JD keyword.** A second pass that removes a first-pass match is discarded even if it adds other keywords.
- An unchanged model reply gets one corrective retry. A result that is still unchanged fails with no download.

Claim detection uses text patterns. Outside frozen qualification sections, degree checks recognize common degree levels; they do not compare every possible degree subject or institution in prose. Client/project checks recognize explicit labels, standalone names, and relationship phrases such as "for client Northstar". These checks are not exhaustive entity recognition.

Timeline checks use a maintained list of case-sensitive tool names and calendar years, not exact release dates. Unknown names, current roles, and roles without recognized dates are not checked against release years. Generic words such as "Lambda" can be mistaken for a tool. See [release-year sources and limits](docs/tool-release-years.md).

Two different numbers are reported. **Keyword coverage** is the share of skill-like words from the job description (Python, AWS, Kubernetes, …) that appear in the final resume; the ones still missing are listed. It is a shape-based guess, so a few non-skills, such as city names, can appear in the missing list, and one-letter languages such as R are not detected. The **model estimate** is the model grading its own rewrite and is usually high. Neither is an ATS score or a hiring guarantee.

## Supported Word layouts

Use a single column of body paragraphs with recognizable section headings. Write each job heading as `Company | dates`, followed by a job-title paragraph. Month names, numeric month/year, year-only ranges, and Present/Current/Now are supported.

Tables, columns, text boxes, content in headers or footers, tracked changes, fields, and manual line breaks inside paragraphs are rejected before model work. Word list styles and paragraph numbering are supported. Existing bold/italic run formatting is retained where text survives. Section and job headings stay with their following content to reduce orphaned headings.

A rewritten document may paginate differently from the input. Text verification checks saved content and order; it is not a visual layout proof for every possible template.

## CLI

```bash
uv run python -m resume_tailor --jd path/to/jd.txt
uv run python -m resume_tailor --jd path/to/jd.txt --two-pass
uv run python -m resume_tailor --jd path/to/jd.txt --resume path/to/resume.docx
uv run python -m resume_tailor --jd -
```

Outputs are isolated under `out/runs/<run-id>/`. Each successful run contains a source snapshot, the tailored resume (same file name as the original), a changelog, a diff, the raw model reply, and a digest manifest. `out/latest.json` points to the latest CLI run; failed runs have no resume path. `--out` selects a different output root.

## HTTP API

`POST /api/tailor` accepts JSON with `jd`, optional `two_pass`, and either a new upload (`resume_b64` and `resume_name`) or a saved `resume_id`. It returns 202 with a run-specific status URL and the saved resume ID. Poll `GET /api/status/<run-id>` until `state` is `complete`, then inspect `result.ok` and `result.download`.

`GET /api/library` lists saved resumes and `GET /api/runs` lists recent jobs. Uploads are stored under `out/library/resumes/`; each job has a separate source snapshot under `out/runs/`. `GET /api/download/<run-id>` serves only that run's verified artifact. Its digest is rechecked before download. Failed runs and modified files cannot download. `GET /api/health` reports availability without selecting a default resume. Only one run is active at a time; overlapping submissions receive 409.

Requests must target the local server, and browser requests must use its own origin. Job descriptions are limited to 60,000 characters, request bodies to 8 MB, and attached Word files to 5 MB compressed and 25 MB expanded. Provider attempts share a bounded deadline. A timed-out provider may still finish remotely; late responses cannot publish a result.

## Implementation

- `structure.py` parses Word paragraphs into typed blocks.
- `prompt.py` defines the editing rules; `llm.py` handles provider calls, deadlines, parsing, and the corrective retry.
- `pipeline.py` shares orchestration between CLI and HTTP, measures keyword coverage, checks the result against the original, and publishes verified artifacts.
- `guardrails.py` protects identity, work history, qualifications, and numbers, and produces the before/after evidence.
- `docx_io.py` writes and verifies Word output; `files.py` publishes files atomically.
- `server.py` and `static/index.html` provide the local app.

## Verification and deployment scope

```bash
uv run pytest
uv build --wheel
uv venv out/wheel-check
uv pip install --python out/wheel-check/bin/python dist/*.whl
out/wheel-check/bin/python tests/check_installed.py
```

Tests use synthetic resumes and mocked providers, and never read `.env`. HTTP tests open loopback sockets. The installed-package check verifies that the wheel includes and serves the UI. CI runs the suite and packaging check on Python 3.11.

This is a **local single-user application**. Public deployment needs account authentication, tenant isolation, managed secrets, retention controls, and a deployment stack appropriate for that environment. Per-run storage grows until the operator removes old runs. Automated checks and test doubles do not establish live writing quality; review actual outputs on representative job descriptions before relying on a provider/model combination.

Resumes in `resume/`, generated outputs in `out/`, and credentials in `.env` are excluded from Git.
