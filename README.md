# Resume tailor

Paste a job description. Get a **new Word resume** in the same layout as the parent file, plus a **0–100 match score**. The original resume is never overwritten.

This is a surgical editor for contract-to-contract tailoring. A human should be able to review the result in about 1–5 minutes. It is not a rewrite tool and it will not invent jobs, dates, tools, or metrics.


## Attach a base resume

In the UI, step **0. Base resume** accepts an optional `.docx`. If you attach one, that file is the base for the run (saved under `out/uploads/`, never overwriting your original). If you leave it empty, the in-repo `resume/*.docx` is used.

After a run, **Changes (diff)** shows the exact line edits so you can review before downloading.

## How the site is built

There is no React app, no Node server, and no database. One small Python package does everything:

1. **Browser UI** — a single static page (`resume_tailor/static/index.html`) served by Python’s stdlib `http.server`. You paste a JD and click **Tailor resume**.
2. **API** — `POST /api/tailor` with `{ "jd": "..." }`. Health check is `GET /api/health`. Download is `GET /api/download`.
3. **One LLM call** — OpenAI-compatible Chat Completions (`resume_tailor/llm.py`). Today that is NVIDIA NIM (Nemotron 3 Ultra) via `https://integrate.api.nvidia.com/v1`. Any compatible provider works by changing `.env`.
4. **Guardrails** — code rejects invented employers, dates, metrics, extra jobs, and oversized rewrites. If the model goes too far, the parent file stays untouched and `out/resume.rejected.md` is written instead.
5. **Word output** — the parent `.docx` is cloned. Edited lines are mapped onto existing paragraphs so fonts, spacing, and layout stay the same. The new file lands in `out/` (for this resume: `out/Sravya_M_resume_tailored.docx`).

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
guardrails  ── facts preserved? changelog honest? size sane?
        │
        ▼
docx_io  ── ordered block alignment → clone parent → out/*_tailored.docx
```

If the tailored resume comes back identical to the original, the changelog says
so in bold and the run is marked failed when the model claimed otherwise. A
silently-unchanged download is the one failure this tool is built to make loud.

The match score is the model’s honest 0–100 against the JD must-haves (years, required tools, domain). A low score usually means the JD does not match the resume, not that the site is broken.

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — installs Python 3.11+ if needed
- An API key for an OpenAI-compatible chat model (NVIDIA `nvapi-…` or OpenAI `sk-…`)
- The parent resume as a Word file in `resume/` (this repo ships `resume/Sravya_M_resume.docx`)

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

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). Paste a job description. Click the blue **Tailor resume** button. Wait (NVIDIA Ultra with thinking can take a couple of minutes). Download the Word file from the page, or grab `out/Sravya_M_resume_tailored.docx`.

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

## What it will change

- A few JD keywords that are already true
- Light retarget of the summary
- Reorder / surface skills already evidenced
- Tweak 1–3 bullets on the most relevant recent roles

## What it will not change

- Employers, dates, titles, education, certifications, or metrics
- Jobs that were not on the parent resume
- Tools the resume does not already list
- The parent Word file (`resume/Sravya_M_resume.docx`)

These are enforced in code, not just asked for in the prompt. A run that adds a
tool or skill term absent from the base resume fails and writes
`out/resume.rejected.md` for review instead of a Word file. That is working as
intended — read the violation, and either drop the term or add the real
experience to the parent `.docx`.

A poor-match JD is not a reason to overhaul the resume. The score should say so.

## Layout

```
resume/Sravya_M_resume.docx   parent Word resume — the source of truth, never overwritten
resume/base.md                stale markdown copy; NOT read by the tool (see below)
resume_tailor/                Python package
  server.py                   localhost UI + /api/tailor
  static/index.html           the website
  structure.py                .docx → typed blocks → markdown, and back
  llm.py                      OpenAI-compatible client, retry + truncation check
  prompt.py                   surgical-edit + scoring instructions
  guardrails.py               reject invented facts, bulk edits, dishonest changelogs
  docx_io.py                  ordered alignment, run-preserving Word writer
out/                          gitignored outputs (Word, changelog, diff)
.env.example                  env template
```

`resume/base.md` is a leftover text copy. The tool reads the `.docx` directly and
derives its markdown from it, so editing `base.md` has no effect. Update the Word
file instead.

## Tests

```bash
uv run pytest
```

Tests mock the LLM and use fake fixtures only. No network. No production resume
content — `tests/test_docx_io.py` builds its own synthetic `.docx` at runtime.

Coverage deliberately includes the shapes that used to be untested: a
heading-less (flat) document through the guardrails, and the Word writer itself,
including that an unchanged input produces a byte-identical document.

## Updating the parent resume

Replace `resume/Sravya_M_resume.docx` with the new Word file. The tailor reads paragraphs from that file. Keep a markdown extract in `resume/base.md` if you want a readable copy in git.

This repo contains a real resume (name, email, phone). Keep the GitHub repository
**private**. Note that as it stands this folder is not a Git repository at all —
there is no `.git`, no remote, and no history, so nothing here is recoverable if
it is lost. `.env` holds a live API key; rotate it before the folder goes anywhere.

`examples/sample_jd.txt` is written to mirror this resume closely, so it will
always score well. Use a real posting when you want to judge the tool.
