# Resume tailor review

Historical note: the personal base described below is the `sravya` branch snapshot. `main` does not include that file; upload a `.docx` or pass `--resume`.

Reviewed and repaired on 10 September 2026. The updated base is installed and verified. **All four P1 findings are addressed:** edited claims require review, each browser run has separate source/output files and a stable download URL, and output paths cannot alias the source. Automated checks still cannot prove every claim; factual review is part of the workflow. **All six P2 findings are now addressed**, with the supported-layout boundary and remaining operational limits documented below.

## P1 repair results

1. **Unsupported claims:** dates and titles stay with their jobs; qualifications cannot be added or rewritten; changed passages are checked for quantities and unknown terms. Each remaining substantive edit carries its original wording and source location. Browser downloads require an explicit review of that exact document; CLI outputs are labeled drafts and include the same evidence. This closes automatic approval of unchecked semantic edits, not the broader problem of independently establishing employment facts.
2. **Wrong downloads:** each browser run has its own directory, manifest, file digest, and download URL. Later runs and failures cannot change an earlier link's document. Changed files cannot be downloaded or approved.
3. **Overlapping uploads:** uploads and the default base are snapshotted before model execution inside each run's unique directory. Two simultaneous uploads called `resume.docx` retain their own contents.
4. **Source loss:** all planned output and diagnostic paths are checked before model work. Exact aliases, symbolic links, and hard links to protected sources are rejected. The Word writer verifies a temporary file before replacing the output directory entry.

**Validation:** 94 tests passed, including new HTTP, concurrency, fact-checking, review, source-alias, and failed-publication regressions. A browser test using the current base showed no Download before review and a run-specific link afterward. The downloaded Word file contained two controlled edits, retained style definitions, numbering, and links, and rendered to four pages without new visible layout damage. The base stayed byte-for-byte identical to the supplied document. No live model call was made.

## P2 repair results

5. **Word layout:** Heading styles, paragraph numbering, and month-name/numeric/year date ranges are recognized. Text extraction follows paragraph/table order, including nested cells. Tailoring explicitly rejects tables, columns, headers/footers with content, text boxes, tracked changes, fields, content controls, manual line breaks, and unrecognized job structures before model work. This repairs silent misinterpretation by defining and enforcing supported inputs; it does not claim universal template support.
6. **Formatting:** Unchanged characters keep their original Word runs when nearby wording changes. The regression preserves a bold `Python` and italic `banking` after replacing an earlier word, including the original style properties. A rendered example was visually checked.
7. **Result accuracy:** Summaries now come from the final before/after evidence. Missing or invalid scores remain unknown. A score and assessment are withheld when guardrails change the model draft or document verification fails. Model explanations are kept only in diagnostic raw output.
8. **Failure files:** CLI runs now use separate directories too. `out/latest.json` identifies the CLI run and its state, with no resume path after failure; previous drafts remain in their own directories. Matching legacy outputs are archived under `previous-output/` within a new run. Failed drafts have no result manifest, and rejected changes are labeled as unpublished.
9. **Slow runs:** The API returns 202 with a progress URL and processes the run in the background. Another submission receives a clear 409 busy response. Both entry points show stages/retries. Provider waits share a default 360-second total deadline, and late replies are discarded. This ends the app's wait; the provider may still finish remotely. Input size/type checks run before work.
10. **Installation and coverage:** The wheel includes the HTML page and uses the working folder for local data. CLI and browser use `pipeline.py` for validation, tailoring, summaries, and publication. Added provider, layout, formatting, CLI failure, and progress regressions, plus an installed-wheel HTTP check and GitHub Actions workflow. Local validation passed; the remote CI workflow has not run because these changes have not been pushed.

**P2 validation:** All 122 tests passed on both Python 3.14 and the minimum supported Python 3.11. Built and installed the final wheel in a clean Python 3.11 environment and confirmed that its own packaged UI returns HTTP 200. Browser checks verified the result/review/download flow and withholding of an invalidated score. The downloaded document matched its expected two edits, preserved the base bytes and shared style/numbering/link definitions, and rendered correctly across all four pages. All model responses were controlled test replies.

## Completed in this pass

- Copied the supplied document unchanged to [resume/Sravya_base.docx](/Users/tuttu/Desktop/resume-tailor/resume/Sravya_base.docx). This is the default base for the app and CLI.
- Made the resolver prefer that Word file over other Word files and legacy markdown. Added a regression check with conflicting old and new sources.
- Removed the obsolete Word and markdown copies from the active `resume/` directory. Local originals are preserved in [previous-base](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/previous-base); the previously tracked versions are also in Git history.
- Updated the README, tailoring instructions, and reset help to describe the current source and workflow. Corrected the obsolete README statement that there was no Git repository.
- Kept the supplied resume's wording, document structure, and formatting intact. The P1 and P2 repair status is summarized above; historical reproduction evidence is retained below.

## What is good

1. **The project is small enough to maintain.** One Python package and one HTML page handle the task. There is no database or frontend build system to operate. `uv.lock` supports repeatable setup.
2. **The editor is designed around real Word output.** It clones the source document, applies edits, then reads the saved file back to compare against the approved text. This is a much stronger foundation than merely displaying a model response.
3. **Structure survives the model boundary for this base.** Name, contact, sections, jobs, titles, environments, and bullets are represented explicitly. The new resume produces 115 nonempty blocks, including four jobs and four main sections.
4. **There are useful defensive checks.** Contact restoration, many employer/title/date checks, rewrite limits, known invention reversions, truncated-response rejection, and retries already exist. They catch real mistakes, although they are incomplete.
5. **The UI makes review practical.** Enter, working, and result states are straightforward; before/after text is shown, and warnings and failures have visible places. Model text is inserted with `textContent`, which avoids interpreting that text as HTML.
6. **Tests cover past document-writing mistakes.** Synthetic fixtures exercise real DOCX generation, hyperlink preservation, added/deleted bullets, insertion order, and output verification without putting a real candidate into the fixtures.
7. **Local operation is appropriately simple.** The normal server binds to loopback, checks request hosts/origins and content types, and excludes `.env` and generated output from Git.

## Confirmed flaws and repair priorities

P1 means repair first because the outcome can contain wrong facts, deliver the wrong document, or lose source data. P2 means repair next for reliability and usability. These are local reproductions and code findings, not estimates of how often the model triggers them.

### 1. P1 — Addressed: fact checks and required factual review

**Status:** The demonstrated date/title swaps, added qualifications, tool, and metric cases are blocked or reverted. Other substantive changes are held for human factual review before browser download. See [guardrails](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py), [review evidence](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py), and [regressions](/Users/tuttu/Desktop/resume-tailor/tests/test_p1_guardrails.py).

**Original impact:** A successful result can still contain an invented tool, qualification, number, or association between a role and its dates.

**Original evidence:** Synthetic probes added `Terraform` inside an experience bullet and `7 ms` as a latency claim; both passed with no warning. An added master's degree passed with only a generic added-line warning. Swapping two employers' date ranges also passed with no warning. Tool recognition depends on spelling and capitalization, metric recognition covers selected units, education checks preserve existing lines but do not prohibit additional degrees, and dates/titles are compared across the whole resume instead of within each job.

**Repair direction:** Protect each job's employer/title/dates as one unit and compare education/certifications in both directions. For edited claims, retain a source reference and require review where evidence cannot be established. Expand deterministic regression cases, but do not describe a larger keyword list as complete factual verification.

Sources: [guardrails.py fact checks](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py), [metric patterns](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py), [tool patterns](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py).

### 2. P1 — Fixed: downloads stay with their own run

**Status:** Replaced process-wide download state and shared browser output paths with per-run manifests, file digests, and URLs. Review and download handlers both verify the file's identity. See [result validation](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py) and [HTTP regressions](/Users/tuttu/Desktop/resume-tailor/tests/test_server.py).

**Original impact:** A result in an older tab can download the file from a later run. After a provider error, the endpoint can still serve the previous successful file.

**Original evidence:** Two successful synthetic HTTP runs returned the same `/api/download` URL; requesting the first result's link after the second run returned different bytes. A simulated provider failure returned HTTP 400, but the download endpoint still returned HTTP 200 with the previous document. `_LAST_DOWNLOAD` is process-wide, output paths are reused, and early error returns do not clear it. The UI timestamp query does not identify a run.

**Repair direction:** Give every run its own output directory and immutable download URL. Publish the link only after verification. Keep the displayed diff and report with that same run. A database is unnecessary for this local workflow.

Sources: [server.py download state](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [publication](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [download handler](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py).

### 3. P1 — Fixed: inputs remain stable during a run

**Status:** Uploads have exclusive files in separate run directories; default-source runs also snapshot the input before waiting for model execution. A concurrent HTTP regression downloads the correct candidate from each of two same-name uploads. See [source snapshots](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py).

**Original impact:** Two tabs uploading `resume.docx` can cause a run to read or clone the other upload. Verification may catch differences and block delivery, but the input is not stable.

**Original evidence:** Uploads are saved under `out/uploads/<sanitized-name>` before the tailoring lock is taken. A probe replaced an earlier upload while that lock was held. The writer later opens the path again, so locking only model execution does not protect the source snapshot.

**Repair direction:** Save uploads into the run's unique directory before processing and keep the source immutable for that run. Include a concurrent upload/download regression in the fix for finding 2.

Sources: [server.py upload storage](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [request ordering](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [writer rereads source](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py).

### 4. P1 — Fixed: source/output aliases are rejected

**Status:** Source, backup, effective Word input, job description, and diagnostic collisions are validated before CLI processing. Direct Word-writer calls also reject source aliases and publish only verified temporary output. See [file protection](/Users/tuttu/Desktop/resume-tailor/resume_tailor/files.py) and [source regressions](/Users/tuttu/Desktop/resume-tailor/tests/test_p1_files.py).

**Original impact:** Some explicitly chosen source/output paths can overwrite a source. The normal new-base path used in this pass is safe and was verified unchanged.

**Original evidence:** Selecting `resume_tailored.md` as the markdown input and its own directory as `--out` overwrites it. A temporary DOCX probe also demonstrated that an existing output symlink pointing to the source is followed and overwrites the source. `_write_outputs` writes before comparing paths, and the shared Word writer has no source/destination identity check.

**Repair direction:** Validate all resolved output paths before writing; reject source aliases, including symlinks and existing hard links. Write to a temporary file, verify it, then publish it atomically.

Sources: [cli.py output writing](/Users/tuttu/Desktop/resume-tailor/resume_tailor/cli.py), [docx_io.py saving](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py).

### 5. P2 — Addressed: supported Word layouts are enforced

**Status:** Layout validation now runs before model work. Word Heading styles, numbering, and common date ranges are recognized; table extraction uses document order. Unsupported layouts are rejected clearly. See [layout checks](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py:85) and [regressions](/Users/tuttu/Desktop/resume-tailor/tests/test_p2_docx.py).

**Original impact:** A different resume template can lose meaningful structure before the model receives it. That also weakens checks that depend on recognized job and section boundaries.

**Original evidence:** A title-case `Professional Experience` paragraph styled `Heading 1` was classified as a tagline. Jobs require a pipe separator and a month/year date pattern. A document containing paragraph → table → paragraph was extracted as paragraph → paragraph → table. Headers, footers, nested tables, and Word numbering outside the expected paragraph style are not fully handled by this traversal/classifier.

**Repair direction:** First validate and clearly reject unsupported layouts. Then add document-order traversal and deliberate support for heading styles, numbering, and common date formats. The supplied base works with the current parser; this finding primarily affects other uploads.

Sources: [structure.py classification](/Users/tuttu/Desktop/resume-tailor/resume_tailor/structure.py), [job recognition](/Users/tuttu/Desktop/resume-tailor/resume_tailor/structure.py), [paragraph traversal](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py).

### 6. P2 — Fixed: unchanged run formatting survives nearby edits

**Status:** The writer maps unchanged characters to their existing styled runs instead of putting the whole suffix into one run. Bold/italic and paragraph-property regressions pass, with visual confirmation. See [writer](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py:186).

**Original impact:** An early word change can remove bold or italic styling later in the same paragraph. A document can pass text verification while its presentation changes.

**Original evidence:** Replacing `Built` with `Delivered` in a paragraph containing a separately bold `Python` run erased that bold formatting. The writer puts the changed suffix in one run and empties subsequent runs. The verification function compares text, not styles, numbering, or rendered layout.

**Repair direction:** Preserve unchanged suffix runs as well as prefixes. Add one mixed-format regression and a small visual check for representative DOCX edits. Keep the current text verification.

Sources: [docx_io.py run rewriting](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py), [text verification](/Users/tuttu/Desktop/resume-tailor/resume_tailor/docx_io.py).

### 7. P2 — Fixed: summaries and scores describe the accepted result

**Status:** Final summaries are derived from source evidence. Unknown scores are not inferred; scores and assessments are withheld after guardrail changes or failed verification. See [shared pipeline](/Users/tuttu/Desktop/resume-tailor/resume_tailor/pipeline.py:20) and [result regressions](/Users/tuttu/Desktop/resume-tailor/tests/test_p2_pipeline.py).

**Original impact:** The score can overstate the accepted document's coverage, and the summary of changes can claim edits that did not survive guardrails.

**Original evidence:** The changelog checker accepts any number of unrelated claims once two or more lines changed. Server and CLI reuse the model's original score and changelog after reverting unsupported lines. If the model supplies only `good`, the parser assigns 85; it does label this as inferred, but that number is not a measured assessment. No requirement-by-requirement calibration is implemented.

**Repair direction:** Generate the change summary from the final diff. Label scores as model estimates, retain unknown scores as unknown, and explain any invalidation after reversions. If structured scoring is added, tie each requirement to evidence in the final document.

Sources: [guardrails.py changelog check](/Users/tuttu/Desktop/resume-tailor/resume_tailor/guardrails.py), [llm.py score inference](/Users/tuttu/Desktop/resume-tailor/resume_tailor/llm.py), [server response](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py).

### 8. P2 — Fixed: failures have their own run state and files

**Status:** CLI and browser now isolate run artifacts. The CLI state file points to the current run and marks failures without a resume path; earlier outputs remain separate. Legacy output files are archived and failed outputs are not published. See [CLI](/Users/tuttu/Desktop/resume-tailor/resume_tailor/cli.py) and [failure regression](/Users/tuttu/Desktop/resume-tailor/tests/test_p2_pipeline.py).


**Original impact:** Someone using the output folder can mistake an old or failed document for the latest approved result, even when the UI withholds its link.

**Original evidence:** A successful file remains in the shared directory after a later guardrail rejection. If DOCX verification fails, the newly written file is left there. The UI says “Nothing was saved,” although rejected markdown, diagnostic output, and sometimes a DOCX have been saved. The CLI announces “Wrote” before verification completes. These paths are visible in the code; they were not all triggered through the browser in this pass.

**Repair direction:** Stage and verify each run before publication, quarantine rejected artifacts, and make the wording distinguish “not approved for download” from “not saved.” The per-run storage repair can cover most of this.

Sources: [cli.py verification order](/Users/tuttu/Desktop/resume-tailor/resume_tailor/cli.py), [server.py write/verify](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [UI blocked wording](/Users/tuttu/Desktop/resume-tailor/resume_tailor/static/index.html).

### 9. P2 — Fixed: progress, busy handling, and bounded provider waits

**Status:** A background run returns a progress URL. The UI shows processing and retry messages; overlapping submissions receive 409. Provider waits share a total deadline and late replies cannot publish output. See [provider deadline](/Users/tuttu/Desktop/resume-tailor/resume_tailor/llm.py:190) and [HTTP progress](/Users/tuttu/Desktop/resume-tailor/tests/test_server.py).


**Original impact:** The user sees a timer while work or retries continue, and another request waits behind the same global lock. Some malformed requests become internal errors instead of clear validation messages.

**Original evidence:** Three attempts at the configured NVIDIA timeout of 300 seconds plus 5/20-second backoffs can consume about 15 minutes 25 seconds before other overhead; this is a code-derived scenario, not a measured run. The frontend has no cancel/status endpoint or explicit in-flight guard. A JSON array sent to `/api/tailor` returned HTTP 500 because the handler assumes an object. Invalid UTF-8 is outside the JSON error handler.

**Repair direction:** Validate payload types and text/file bounds before processing. Return a clear busy response or explicit queued status, enforce a total deadline, and show retry progress. Add cancellation only if it can actually stop or discard the server-side work.

Sources: [llm.py retry configuration](/Users/tuttu/Desktop/resume-tailor/resume_tailor/llm.py), [server.py validation](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [UI request](/Users/tuttu/Desktop/resume-tailor/resume_tailor/static/index.html).

### 10. P2 — Fixed locally: installed app, shared pipeline, and regression coverage

**Status:** Static package data is included, a clean installed wheel serves the UI, the two entry points share orchestration, and provider/HTTP regressions pass. CI is configured for Python 3.11 and remains pending its first push. See [workflow](/Users/tuttu/Desktop/resume-tailor/.github/workflows/tests.yml) and [installed check](/Users/tuttu/Desktop/resume-tailor/tests/check_installed.py).


**Original impact:** The repository works locally, but an installed wheel lacks the page required for `--serve`. Green tests do not currently cover the most consequential browser/API scenarios above.

**Original evidence:** `uv build --wheel` succeeded, but the wheel contained no `resume_tailor/static/index.html`. The repository has no tracked CI workflow or server test module. The existing tests cover guardrails, parsing, the CLI, and DOCX writing; HTTP lifecycle and provider retry behavior are absent. CLI and server also duplicate the orchestration sequence, making fixes easy to apply to only one entry point.

**Repair direction:** Include static package data and test an installed wheel. Add focused HTTP success/failure/concurrency checks, provider error checks, and CI. Move the shared tailoring sequence into one function when making those repairs; a framework rewrite is unnecessary.

Sources: [pyproject.toml packaging](/Users/tuttu/Desktop/resume-tailor/pyproject.toml), [server static path](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py), [CLI orchestration](/Users/tuttu/Desktop/resume-tailor/resume_tailor/cli.py), [server orchestration](/Users/tuttu/Desktop/resume-tailor/resume_tailor/server.py).

## Other negatives and tradeoffs

- **The model regenerates the entire resume for a few edits.** This increases response size and the opportunity for unrelated changes. A later patch-based output could help, but first secure the existing file and fact checks.
- **Automated checks have limits.** README and working-screen wording now explain those limits and required factual review. The historical model comparison is explicitly labeled as an older test, not a fresh benchmark against the new base. Unknown or invalidated scores are withheld, and change summaries now describe the final evidence.
- **The app is a local single-user tool.** Per-run files and review status persist on disk. There is no account/session system, history browser, or automatic retention cleanup. Disk use grows with saved runs.
- **The supplied resume is dense.** It renders to four pages, with the SoFi and Health First headings separated from their first bullets by page breaks. The base was retained exactly as supplied. A later formatting pass can apply “keep with next” without rewriting career facts.
- **A base resume is an asserted source, not independent proof of employment.** This review checked import fidelity and code behavior. It did not independently verify the candidate's experience or product chronology.

## Remaining limits and optional work

The ten audit findings are addressed within the supported local workflow. Factual review is still required; scores are estimates. Complex Word layouts must be simplified before tailoring. The original base retains its supplied four-page layout and the two headings separated from their first bullets. A visual cleanup of that base, broader template support, retention cleanup, a history browser, or provider-supported cancellation would be separate improvements. The new CI workflow still needs its first remote run after a push.

## Verification and scope

- Read every tracked application module, the complete frontend, test files and fixtures, project configuration, README, shell launcher, and bundled tailoring instructions. This was a review of project-owned code, not a dependency-source or penetration audit.
- Baseline: **62 tests passed**. After the base-selection regression: **63 tests passed**. After P1 repairs: **94 tests passed**. After P2 repairs: **122 tests passed**. `git diff --check` passed.
- The new base is byte-for-byte identical to the supplied file. SHA-256: `64b39ed5a8bc2d22e32db1b6276bc050bafe6ca892cfa0f54dcb86dbaf72c275`.
- An isolated real HTTP flow and browser review check returned a verified result, HTTP 409 for download before review, then HTTP 200 after review. A fixed model reply produced two edits; the downloaded DOCX matched the expected text, while source bytes, style definitions, numbering, and hyperlink relationships stayed intact.
- Rendered and visually inspected all four source pages and all four downloaded test pages. The source's existing page-break issues were preserved; no new visible layout damage was observed in the controlled edit.
- Temporary verification servers were stopped. Port 8787 was checked and is free; the normal app was not restarted.
- All model responses in this pass were test doubles. No paid model call, live quality benchmark, job application, commit, or push was performed.

Local evidence: [audit probes](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/audit_probes.py), [probe results](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/audit-probe-results.json), [base verification](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/base-verification.json). These files are in ignored `out/`; the review itself is kept in the repository workspace.

P1 repair evidence: [download verification](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/p1-browser/verification.json), [rendered download](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/p1-download-render), and regression files under `tests/`. Work remains uncommitted. The earlier audit probes describe the pre-repair behavior; their old API assumptions are retained as historical evidence.

P2 repair evidence: [download verification](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/p2-browser/verification.json), [four rendered pages](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/p2-download-render), and [mixed-format render](/Users/tuttu/Desktop/resume-tailor/out/review-2026-09-10/p2-format/render/page-1.png).
