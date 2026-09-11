# Resume tailor

Paste a job description. Review the proposed edits, then download a **new Word resume**, plus a **model-estimated match score**. Tailoring leaves the original resume unchanged.

This is a surgical editor for contract-to-contract tailoring. It aims for a few edits that a human can review in about 1–5 minutes. Automated checks catch many unsupported facts, but cannot establish the truth of every reworded claim. Every substantive edited browser result requires factual review before download.


## Attach a base resume

In the UI, **change** beside **Base resume** accepts an optional `.docx`. Each run saves its own input under `out/runs/<run-id>/source/`, so uploads with the same name stay separate. If you leave it empty, the app snapshots `resume/Sravya_base.docx` for the run.

After a run, **What changed** shows the original and edited passages with their source sections. Check that every change describes the candidate's real experience, select the review checkbox, and click **Confirm review**. This enables **Download Word resume** for that exact file. A failed run has no download.

Supported inputs use a single column of body paragraphs, clear section headings,
and job headings written as `Company | dates`, with the job title in the next
paragraph. Headings can use Word's Heading styles or recognizable all-caps
labels. Word list styles and paragraph numbering are recognized. Dates can use
month names, numeric month/year, or years alone; `Present`, `Current`, and `Now`
are recognized. Tables, multiple columns, text boxes, resume content in headers
or footers, tracked changes, content controls, fields, and manual line breaks
inside paragraphs are rejected before model work with an explanation. The
supplied base passes these checks. The text extractor reads tables in document
order, but tailoring table layouts is deliberately unsupported.

## How the site is built

There is no React app, no Node server, and no database. One small Python package does everything:

1. **Browser UI** — a single static page (`resume_tailor/static/index.html`) served by Python’s stdlib `http.server`. You paste a JD and click **Tailor resume**.
2. **API** — `POST /api/tailor` with `{ "jd": "..." }` returns **202** and a `status` URL. Poll that URL (`GET /api/status/<run-id>`) for progress; once `state` is `complete`, inspect its `result`. Only one run is active at a time; another request receives **409** with a busy message. Edited results include source evidence, a SHA-256 file digest, and `/api/review/<run-id>`; posting `{ "reviewed": true, "sha256": "<returned digest>" }` records review. Download is `GET /api/download/<run-id>`. Pending review returns 409; missing or changed output returns 404. Health check is `GET /api/health`.
3. **One LLM call** — OpenAI-compatible Chat Completions (`resume_tailor/llm.py`). Today that is NVIDIA NIM (Nemotron 3 Ultra) via `https://integrate.api.nvidia.com/v1`. Any compatible provider works by changing `.env`.
4. **Guardrails** — each recognized job keeps its employer, title, and dates; qualifications are frozen; changed passages are checked for altered quantities and unknown terms. Other changes require human review. Rejections save diagnostic text in that run's directory.
5. **Word output** — the source `.docx` is cloned, edited, and checked in a temporary file before publication. Source/output aliases, including symlinks and hard links, are rejected. Browser and CLI results stay in separate `out/runs/<run-id>/` folders. Unchanged characters retain their original runs, preserving bold and italic formatting around edits. Review the resulting layout before use.

```
Browser (127.0.0.1:8787)
        │  paste JD
        ▼
resume_tailor/server.py
        │
        ▼
structure.py  ── parent .docx → typed blocks → markdown
        │  one Chat Completions call
        ▼
NVIDIA NIM / OpenAI-compatible model
        │  changelog + score + tailored markdown
        ▼
guardrails  ── protected facts + rewrite limits + source evidence
        │
        ▼
docx_io  ── clone snapshot → edit → verify → save within this run
        │
        ▼
human factual review → download this run's verified document
```

If the tailored resume comes back identical to the original, the changelog says
so in bold and the run is marked failed when the model claimed otherwise. A
silently-unchanged download is the one failure this tool is built to make loud.

The match score is a model estimate against the JD, not a measured certification.
Missing, malformed, or out-of-range scores stay unknown. When guardrails change
the model's draft or the result fails verification, its score and assessment
are withheld. The displayed changelog is derived from final before/after
evidence; the original model explanations remain only in `model.raw.txt` for
diagnosis. The browser and CLI share this processing in `pipeline.py`.

## Which model, and why it is slow

The following historical tests used two job descriptions on 4 Sep 2026, before
the updated base and P1 repairs. They are not a fresh quality benchmark. The
recorded outcomes were:

| Model | Time | Good-fit JD | Poor-fit JD |
|---|---|---|---|
| **nemotron-3-ultra, thinking on** (default) | 2–5 min | 85, passed, real edits | 35, blocked GitOps/IRSA |
| nemotron-3-ultra, thinking off | 107–224 s | 78, passed | 55, **13 fabricated tools + a metric** |
| nemotron-3-super-120b, thinking on | 96 s | 50, passed, no real tailoring | — |
| nemotron-3-super-120b, thinking off | 27–77 s | returned the resume **unchanged** while listing 3 edits | 30 |
| nemotron-3.5-lightning-30b | 95 s | — | 68, listed 4 edits, changed 1 unrelated line |
| nemotron-3-nano-omni-30b | 46 s | — | 65, fabricated 9 tools |
| gpt-oss-20b | 135 s | — | 60, rewrote 52 lines |

The default remains Ultra with thinking based on these past tests. Thinking
does not guarantee factual accuracy, and faster replies still need the same
checks and human review.

Provider attempts and retries share `OPENAI_TOTAL_TIMEOUT` (default **360
seconds**). `OPENAI_TIMEOUT` controls each attempt's socket timeout, capped by
the remaining total. The browser shows the current processing stage and retry
message; the CLI prints the same progress. When the total wait expires, the run
fails and late replies are discarded. The provider may still finish remotely;
the app does not claim to cancel provider work. Job descriptions are limited
to 60,000 characters and attached Word files to 5 MB.

## Why the score is not 80–90 on every job

The score estimates how well the resume fits the job. Tailoring should surface
relevant experience already supported by the base, and leave genuine gaps
visible. A higher number is not a reason to invent experience.

The lever that works is the base resume itself. It is the ceiling. If she has
set SLOs, done GitOps, handled HIPAA data, add it to `resume/Sravya_base.docx`
once; every future run can then surface it. Ten minutes making the base
complete does more for scores than any model or prompt change, and it is true.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — installs Python 3.11+ if needed
- An API key for an OpenAI-compatible chat model (NVIDIA `nvapi-…` or OpenAI `sk-…`)
- The parent resume as a Word file in `resume/` (this repo ships `resume/Sravya_base.docx`)

Install uv once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Run it (someone new, without the original builder)

From the repo root:

```bash
uv sync
cp .env.example .env
```

Edit `.env`. **Never commit `.env`.**

NVIDIA (current setup on the Mac mini):

```
OPENAI_API_KEY=nvapi-your-key
OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1
OPENAI_MODEL=nvidia/nemotron-3-ultra-550b-a55b
OPENAI_MAX_TOKENS=16384
OPENAI_TIMEOUT=300
OPENAI_TOTAL_TIMEOUT=360
NVIDIA_ENABLE_THINKING=1
```

Get a key at [build.nvidia.com](https://build.nvidia.com). Do not paste keys into chat.

OpenAI instead:

```
OPENAI_API_KEY=sk-your-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5.6-terra
```

Start the UI:

```bash
uv run python -m resume_tailor --serve
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Paste a job description. Click **Tailor resume**. Wait (NVIDIA Ultra with thinking can take a couple of minutes), review the changed claims, then download the Word file from the page.

Safari: hard-refresh with **Cmd-Shift-R** if the page looks stale. The UI is a simple light form on purpose so Reader mode does not eat the submit button.

Another port:

```bash
uv run python -m resume_tailor --serve --port 8787
```

CLI (same backend, no browser):

```bash
uv run python -m resume_tailor --jd path/to/jd.txt
uv run python -m resume_tailor --jd -                 # paste JD, then Ctrl-D
```

CLI success writes a **draft for factual review** and before/after evidence in
the new run's `CHANGELOG.md`. The terminal prints the exact folder and draft
path. `out/latest.json` records the CLI run and its state; a failed run has no
resume path. Earlier results stay in their own folders. Matching legacy files
directly under `out/` are moved into `previous-output/` within the first new
CLI run. `--out <directory>` changes the root for these run folders.

## What it will change

- A few JD keywords that are already true
- Light retarget of the summary
- Reorder / surface skills already evidenced
- Tweak 1–3 bullets on the most relevant recent roles

## Facts that must remain unchanged

- Employers, dates, titles, education, certifications, or metrics
- Jobs that were not on the parent resume
- Tools the resume does not already list
- The parent Word file (`resume/Sravya_base.docx`)

Job and qualification checks depend on recognized document structure. Unknown
terms may be reverted or rejected; changed quantities are blocked. Other
semantic changes can pass these checks, so the browser holds edited results
for factual review. Review is an acknowledgment by the operator, not independent
proof of employment. CLI output is a draft with the same review evidence.

A poor-match JD is not a reason to overhaul the resume. The score should say so.

## Layout

```
resume/Sravya_base.docx   parent Word resume — the source of truth, never overwritten
resume_tailor/                Python package
  server.py                   localhost UI + /api/tailor
  static/index.html           the website
  structure.py                .docx → typed blocks → markdown, and back
  llm.py                      OpenAI-compatible client, retry + truncation check
  pipeline.py                 shared validation, tailoring, evidence, and output
  prompt.py                   surgical-edit + scoring instructions
  guardrails.py               protected facts, rewrite limits, source review evidence
  docx_io.py                  ordered alignment, run-preserving Word writer
  files.py                    source path checks and atomic output publication
out/                          gitignored outputs (Word, changelog, diff)
  runs/<run-id>/              source snapshot, outputs, report, review/progress status
  latest.json                CLI run state and exact output path
.env.example                  env template
```

`resume/Sravya_base.docx` is the current base, imported unchanged from the supplied
`Sravya_base.docx` on 10 Sep 2026. The app and CLI read this Word file directly.
The old Word and markdown copies were removed from `resume/` to avoid ambiguity;
they remain recoverable from Git history. Tailored files belong in `out/`.

## Tests

```bash
uv run pytest
```

Tests mock the LLM and use fake fixtures only. No network. No production resume
content — `tests/test_docx_io.py` builds its own synthetic `.docx` at runtime.

Coverage deliberately includes the shapes that used to be untested: a
heading-less (flat) document through the guardrails, and the Word writer itself,
including that an unchanged input produces a byte-identical document.
HTTP regressions cover review gating, stable download URLs, concurrent uploads,
source snapshots, provider and verification failures, and changed output files.
Source alias tests cover exact paths, symlinks, hard links, and diagnostics.
Provider tests cover retries, authentication failures, malformed responses,
truncation, and the total waiting limit. Layout and formatting tests cover the
supported input boundary and preserved bold/italic runs.

The CI workflow runs tests and builds/installs a wheel, then requests the UI
from that installed package. It uses [checkout](https://github.com/actions/checkout)
and [setup-uv](https://github.com/astral-sh/setup-uv). To verify packaging locally:

```bash
uv build --wheel
uv venv out/wheel-check
uv pip install --python out/wheel-check/bin/python dist/*.whl
out/wheel-check/bin/python tests/check_installed.py
```

Run the installed app from the folder containing `resume/` and `.env`; its
outputs belong to that working folder, while the UI ships inside the package.

## Updating the parent resume

Replace `resume/Sravya_base.docx` with the new Word file, keeping that filename.
The resolver prefers it over other Word files or legacy markdown in `resume/`.
Keep old versions outside `resume/`, so they cannot become an accidental base.
The CLI also supports an explicit `--resume PATH`.

`--reset` restores the selected source from its first-run `.original` backup;
it does not undo a tailoring run. After an intentional base update, remove or
refresh that backup before using reset so it cannot restore an older version.

The repository intentionally includes the resume. Keep `.env`, credentials,
and generated outputs out of commits.

`examples/sample_jd.txt` is written to mirror this resume closely, so it will
always score well. Use a real posting when you want to judge the tool.
