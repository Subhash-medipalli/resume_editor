"""Code-level guardrails: preserve facts, contact, structure; cap rewrite size."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_SPAN_RE = re.compile(
    rf"{MONTH}\s+\d{{4}}\s*[–—-]\s*(?:Present|{MONTH}\s+\d{{4}})",
    re.IGNORECASE,
)
# An email address or a phone number — the details this guardrail exists to protect.
CONTACT_MARKER_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ()./-]{7,}\d"
)
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
H3_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
# Quantities a reader would treat as a factual claim. The narrow original
# version matched only currency and percentages, so "11+ years" — the most
# load-bearing number on the resume — could be changed freely.
METRIC_RE = re.compile(
    r"\$\s?[\d,]+(?:\.\d+)?\s?(?:[KMB]|thousand|million|billion)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s*\+?\s*"
    r"(?:%|percent|x\b|[KMB]\b|million|billion|years?|months?|weeks?|days?|hours?|"
    r"users?|customers?|clients?|records?|models?|engineers?|people|teams?)"
    r"|\b\d{1,3}(?:,\d{3})+\+?",
    re.IGNORECASE,
)

# Hard fail if the model rewrote most of the document.
MAX_CHANGED_LINE_RATIO = 0.35
MAX_CHANGED_LINES_HARD = 80
# Warn (still accept) above this many changed content lines.
WARN_CHANGED_LINES = 25
# Deleting this many lines is a rewrite, not a tailoring pass.
MAX_REMOVED_LINES = 6
# Additions now reach the Word file, so they need a ceiling of their own.
MAX_ADDED_LINES = 8


@dataclass
class GuardrailReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    restored_contact: bool = False
    changed_line_count: int = 0
    removed_line_count: int = 0
    added_line_count: int = 0


@dataclass(frozen=True)
class ProtectedFacts:
    contact_block: str
    h2_headings: tuple[str, ...]
    job_headings: tuple[str, ...]
    companies: tuple[str, ...]
    titles: tuple[str, ...]
    date_spans: tuple[str, ...]
    education_lines: tuple[str, ...]
    cert_lines: tuple[str, ...]
    metrics: tuple[str, ...]


def extract_facts(markdown: str) -> ProtectedFacts:
    text = _normalize(markdown)
    contact = _contact_block(text)
    h2 = tuple(_norm_heading(m.group(1)) for m in H2_RE.finditer(text))
    job_headings = tuple(m.group(1).strip() for m in H3_RE.finditer(text))
    companies: list[str] = []
    titles: list[str] = []
    for heading in job_headings:
        title, company = _split_job_heading(heading)
        if title:
            titles.append(title)
        if company:
            companies.append(company)
    for title in _bold_titles_after_h3(text):
        if title not in titles:
            titles.append(title)
    date_spans = tuple(_canon_span(m.group(0)) for m in DATE_SPAN_RE.finditer(text))
    education_lines = _section_item_lines(text, "education")
    cert_lines = _section_item_lines(text, "certifications")
    metrics = tuple(sorted({_canon_metric(m) for m in METRIC_RE.findall(text)}))
    return ProtectedFacts(
        contact_block=contact,
        h2_headings=h2,
        job_headings=job_headings,
        companies=tuple(companies),
        titles=tuple(titles),
        date_spans=date_spans,
        education_lines=education_lines,
        cert_lines=cert_lines,
        metrics=metrics,
    )


def apply_guardrails(base: str, tailored: str) -> tuple[str, GuardrailReport]:
    """Restore contact block if needed; flag invented or dropped facts."""
    report = GuardrailReport(ok=True)
    base_n = _normalize(base)
    out = _normalize(tailored)
    base_facts = extract_facts(base_n)

    out, restored = _restore_contact(base_n, out)
    if restored:
        report.restored_contact = True
        report.warnings.append(
            "Name or contact details were altered by the model; restored from the base resume."
        )

    out_facts = extract_facts(out)

    if len(out_facts.h2_headings) != len(base_facts.h2_headings):
        report.violations.append(
            "Section count changed "
            f"({len(base_facts.h2_headings)} → {len(out_facts.h2_headings)}). "
            "Preserve section order and structure."
        )
    else:
        for original, new in zip(base_facts.h2_headings, out_facts.h2_headings):
            if original != new:
                report.warnings.append(
                    f"Section heading tweaked: {original!r} → {new!r}."
                )

    if len(out_facts.job_headings) > len(base_facts.job_headings):
        report.violations.append(
            "New job heading(s) appeared. Never add jobs that were not on the base resume."
        )
    if len(out_facts.job_headings) < len(base_facts.job_headings):
        report.violations.append("One or more job headings were removed.")

    for company in base_facts.companies:
        if _norm_dashes(company).lower() not in _norm_dashes(out).lower():
            report.violations.append(f"Employer missing from tailored resume: {company}")
    for company in out_facts.companies:
        if not _fuzzy_present(company, base_facts.companies):
            report.violations.append(
                f"Invented or rewritten employer is not on the base resume: {company}"
            )
    out_norm = out.translate(_TYPOGRAPHY).lower()
    for title in base_facts.titles:
        if not title:
            continue
        if title.translate(_TYPOGRAPHY).lower() not in out_norm:
            report.violations.append(f"Job title missing from tailored resume: {title}")
    # Employers and dates were checked in both directions; titles were not, so
    # seniority inflation ("Data Scientist" -> "Senior Data Scientist") passed.
    for title in out_facts.titles:
        if not title:
            continue
        if not _fuzzy_present(title, base_facts.titles):
            report.violations.append(
                f"Invented or rewritten job title is not on the base resume: {title}"
            )
    base_spans = set(base_facts.date_spans)
    out_spans = set(out_facts.date_spans)
    for span in base_spans - out_spans:
        report.violations.append(f"Date range missing from tailored resume: {span}")
    for extra_span in out_spans - base_spans:
        report.violations.append(
            f"Invented or rewritten date range is not on the base resume: {extra_span}"
        )
    for line in base_facts.education_lines:
        if line.translate(_TYPOGRAPHY).lower() not in out_norm:
            report.violations.append(f"Education line missing: {line}")
    for line in base_facts.cert_lines:
        if line.translate(_TYPOGRAPHY).lower() not in out_norm:
            report.violations.append(f"Certification line missing: {line}")
    for metric in out_facts.metrics:
        if metric not in base_facts.metrics:
            report.violations.append(
                f"Invented metric {metric!r} is not on the base resume."
            )

    # Rule 4 of the system prompt forbids inventing tools the candidate never
    # listed, but nothing in code enforced it — a run added "GitOps" to a skills
    # line and passed clean.
    base_tech = _known_vocabulary(base_n)
    for token in sorted(
        token for token in _technology_tokens(out) if token.lower() not in base_tech
    ):
        report.violations.append(
            f"Invented technology {token!r} is not on the base resume."
        )
    # A skills line is a list of claims. Adding a term that appears nowhere on
    # the base resume is a new claim about what the candidate can do, which is
    # the fabrication this tool exists to prevent — even when the word looks
    # ordinary ("Runbooks", "Alerting").
    already = {token.lower() for token in _technology_tokens(out)}
    new_skills = sorted(
        token
        for token in _skill_line_tokens(out)
        if token.lower() not in base_tech and token.lower() not in already
    )
    if new_skills:
        report.violations.append(
            "Skill terms added that are not on the base resume: "
            + ", ".join(new_skills[:12])
            + ". Remove them or point at the experience that evidences them."
        )

    changed = _changed_content_lines(base_n, out)
    report.changed_line_count = changed
    added = _added_content_lines(base_n, out)
    report.added_line_count = added
    if added > MAX_ADDED_LINES:
        report.violations.append(
            f"{added} new lines were added. A surgical edit surfaces existing "
            "evidence; it does not write new resume content."
        )
    elif added:
        report.warnings.append(f"{added} line(s) added — check every claim is already true.")
    removed = _removed_content_lines(base_n, out)
    report.removed_line_count = removed
    if removed > MAX_REMOVED_LINES:
        report.violations.append(
            f"{removed} lines were dropped from the resume. A surgical edit does "
            "not delete content."
        )
    elif removed:
        report.warnings.append(f"{removed} line(s) removed — check nothing important was lost.")
    base_count = max(len(_content_lines(base_n)), 1)
    ratio = changed / base_count
    if changed > MAX_CHANGED_LINES_HARD or ratio > MAX_CHANGED_LINE_RATIO:
        report.violations.append(
            f"Too many lines changed ({changed} lines, {ratio:.0%} of the resume). "
            "This looks like a rewrite, not a surgical edit."
        )
    elif changed > WARN_CHANGED_LINES:
        report.warnings.append(
            f"{changed} lines changed — still review, but this is heavier than a typical 1-minute pass."
        )

    report.ok = not report.violations
    if not out.endswith("\n"):
        out += "\n"
    return out, report


# Phrases a model uses when it is reporting that it deliberately changed nothing.
# Anything else in a changelog is treated as a claim that an edit was made, so
# this check fails loud by default rather than relying on a verb whitelist.
NO_CHANGE_RE = re.compile(
    r"no (?:substantive |material |relevant )?(?:edits?|changes?|content|"
    r"modifications?)\b|left (?:it |the resume |them )?(?:unchanged|as-is|as is|"
    r"intact|untouched)|unchanged\b|not (?:modified|changed|altered)|"
    r"nothing (?:was )?(?:changed|added)|zero overlap|no overlap",
    re.IGNORECASE,
)


def check_changelog_matches_diff(
    changelog: list[str], changed_line_count: int
) -> list[str]:
    """Catch a changelog that claims edits the tailored resume does not contain.

    This is the check that would have caught the whole-output-discard bug on the
    first run: the model listed five concrete changes and the delivered file was
    byte-identical to the original.
    """
    if changed_line_count > 0 or not changelog:
        return []
    if all(NO_CHANGE_RE.search(item) for item in changelog):
        return []  # the model reported making no changes, and made none
    return [
        f"The changelog lists {len(changelog)} change(s) but the tailored resume "
        "is identical to the original (0 lines changed). The edits were lost "
        "somewhere in the pipeline — do not trust this run."
    ]


def unified_diff(base: str, tailored: str, *, fromfile: str, tofile: str) -> str:
    return "".join(
        difflib.unified_diff(
            _normalize(base).splitlines(keepends=True),
            _normalize(tailored).splitlines(keepends=True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_dashes(text: str) -> str:
    return text.replace("—", "–").replace("-", "–")


def _norm_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _canon_span(text: str) -> str:
    """Canonical form of a date range, so cosmetic reformatting is not an edit.

    Without this, "May 2024 – Present" -> "May 2024–Present" was reported as
    both a missing date and an invented one, failing a correct tailoring.
    """
    span = _norm_dashes(re.sub(r"\s+", " ", text).strip())
    span = re.sub(rf"({MONTH})[a-z]*", lambda m: m.group(1)[:3], span, flags=re.IGNORECASE)
    span = re.sub(r"\s*–\s*", "–", span)
    return span.lower()


def _canon_metric(text: str) -> str:
    """Normalise a quantity so rephrasing is allowed but changing it is not."""
    return re.sub(r"\s+", " ", text).replace("+", "").strip().lower()


# A heading-less document must never be treated as one giant contact block.
# The window is wide enough to reach a contact line that sits a few rows down.
MAX_CONTACT_LINES = 6


def _contact_block(text: str) -> str:
    """The name/contact/tagline lines above the first section heading.

    When the document has no ``## `` heading at all, fall back to the first few
    lines instead of the whole document — returning everything here caused the
    restore below to overwrite the entire tailored resume with the base.
    """
    match = re.search(r"^##\s+", text, re.MULTILINE)
    if match:
        return text[: match.start()]
    # No headings: keep only the leading lines that actually look like a header,
    # ending at the last contact detail found near the top. Returning the whole
    # document here is what let the restore below wipe out every tailoring.
    lines = text.splitlines(keepends=True)
    limit = 1
    for index, line in enumerate(lines[:MAX_CONTACT_LINES]):
        if CONTACT_MARKER_RE.search(line):
            limit = index + 1
    return "".join(lines[:limit])


def _last_identity_line(lines: list[str]) -> int:
    """Index of the last header line carrying a contact detail.

    Everything up to it is identity (name, email, phone); anything after it is
    usually a headline/tagline, which the model is allowed to retarget.
    """
    last = -1
    for index, line in enumerate(lines):
        if CONTACT_MARKER_RE.search(line):
            last = index
    return last


def _restore_contact(base: str, out: str) -> tuple[str, bool]:
    """Put the name/contact header back if the model altered it.

    The restore itself was always correct; the bug was that ``_contact_block``
    returned the *entire document* for a resume with no ``## `` headings, so
    this line silently replaced every tailored edit with the original text and
    logged it as a mere warning. With the header properly bounded, only the
    header is ever restored.
    """
    base_header = _contact_block(base)
    out_header = _contact_block(out)
    if out_header == base_header:
        return out, False

    base_lines = base_header.splitlines()
    out_lines = out_header.splitlines()
    boundary = _last_identity_line(base_lines)

    # Same shape: restore only the identity lines, so a retargeted headline
    # under the contact details is left alone.
    if boundary >= 0 and len(base_lines) == len(out_lines):
        merged = list(out_lines)
        restored = False
        for index in range(boundary + 1):
            if base_lines[index].strip() != out_lines[index].strip():
                merged[index] = base_lines[index]
                restored = True
        if not restored:
            return out, False
        trailing = out_header[len(out_header.rstrip("\n")) :]
        return "\n".join(merged) + trailing + out[len(out_header) :], True

    # The header changed shape (lines added or removed): restore all of it.
    return base_header + out[len(out_header) :], True


def _split_job_heading(heading: str) -> tuple[str, str | None]:
    """Parse `Title — Company` or `Company, Location | Mon YYYY – Mon YYYY`."""
    if "|" in heading:
        left, right = heading.split("|", 1)
        if DATE_SPAN_RE.search(right):
            company = left.strip()
            return "", company or None
    remainder = DATE_SPAN_RE.sub("", heading)
    remainder = re.sub(r"\s+", " ", remainder).strip(" |,;/-")
    for sep in (" — ", " – ", " - "):
        if sep in remainder:
            title, company = remainder.split(sep, 1)
            title, company = title.strip(), company.strip()
            return title, company or None
    for sep in (" — ", " – ", " - "):
        if sep in heading:
            left, right = heading.split(sep, 1)
            right_stripped = right.strip()
            if DATE_SPAN_RE.fullmatch(right_stripped) or right_stripped.lower() == "present":
                return "", left.strip() or None
            if not DATE_SPAN_RE.search(right_stripped):
                return left.strip(), right_stripped
    return heading.strip(), None


def _bold_titles_after_h3(text: str) -> tuple[str, ...]:
    found: list[str] = []
    for match in H3_RE.finditer(text):
        for line in text[match.end() :].splitlines():
            if not line.strip():
                continue
            bold = re.fullmatch(r"\*\*(.+?)\*\*", line.strip())
            if bold:
                found.append(bold.group(1).strip())
            break
    return tuple(found)


def _section_item_lines(text: str, heading_lname: str) -> tuple[str, ...]:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading_lname)}\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return ()
    rest = text[match.end() :]
    next_h2 = re.search(r"^##\s+", rest, re.MULTILINE)
    body = rest[: next_h2.start()] if next_h2 else rest
    lines = tuple(
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    return lines


# Tool and technology names are shaped unlike ordinary prose: internal capitals
# (GitOps, PySpark, LangGraph), short all-caps acronyms (AWS, EKS, RBAC), or a
# digit/symbol (C++, Python3, S3). Matching on shape keeps false positives low.
TECH_TOKEN_RE = re.compile(
    r"\b(?:[A-Za-z]+[A-Z][A-Za-z0-9+#.]*|[A-Z]{2,8}|[A-Za-z]+[0-9]+[A-Za-z0-9+#]*)\b"
)
# Words that merely look like acronyms but carry no claim.
TECH_STOPWORDS = frozenset(
    """A I AI ML AND OR THE FOR WITH FROM INTO API APIS CI CD IT IS AS AT ON IN
    TO BY OF US UK EU PHD BS BA MS MSC BSC MBA CV HR QA UX UI PM SME KPI ROI
    SLA SOP EOD ASAP FAQ TBD N A""".split()
)


def _technology_tokens(text: str) -> set[str]:
    """Tokens shaped like a product name. High precision — safe to hard-fail on."""
    return {
        token.strip(".")
        for token in TECH_TOKEN_RE.findall(text)
        if token.upper() not in TECH_STOPWORDS and len(token) > 1
    }


def _skill_line_tokens(text: str) -> set[str]:
    """Capitalised items listed on a "Label: value" line.

    Every entry on a skills line is a claim, but many are ordinary words
    ("Runbooks", "Alerting"), so a new one here is worth surfacing without
    failing the run.
    """
    found: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*# ").strip()
        marker = line.find(": ")
        if not 0 < marker < 60:
            continue
        for token in re.findall(r"\b[A-Z][A-Za-z0-9+#.]{1,}\b", line[marker + 2 :]):
            if token.upper() not in TECH_STOPWORDS:
                found.add(token.strip("."))
    return found


def _known_vocabulary(text: str) -> set[str]:
    """Every capitalised or technology-shaped token in the base resume.

    Deliberately a superset of what `_technology_tokens` extracts, so that a word
    the base only ever uses in prose ("Application Insights" inside a bullet) is
    not reported as invented when the model surfaces it on a skills line.
    """
    tokens = {token.lower() for token in _technology_tokens(text)}
    tokens |= {token.lower() for token in _skill_line_tokens(text)}
    capitalised = re.findall(r"\b[A-Z][A-Za-z0-9+#.]{1,}\b", text)
    tokens |= {token.lower().strip(".") for token in capitalised}
    # Products get written both ways ("Llama Index" / "LlamaIndex"), and a
    # respacing is not a new claim.
    for pair in re.findall(r"\b([A-Z][A-Za-z0-9+#.]{1,})\s+([A-Z][A-Za-z0-9+#.]{1,})\b", text):
        tokens.add((pair[0] + pair[1]).lower().strip("."))
    return tokens


def _fuzzy_present(value: str, originals: tuple[str, ...]) -> bool:
    """Is `value` one of `originals`, allowing abbreviation but not expansion.

    The original accepted a match in either direction, so any fabricated name
    that merely *contained* a real one passed — "Grab" becoming "Grab Financial
    Group Holdings Pte Ltd" raised no violation at all.
    """
    needle = _norm_dashes(value).lower().strip()
    if len(needle) < 3:
        return True
    for original in originals:
        hay = _norm_dashes(original).lower().strip()
        if needle == hay:
            return True
        # A shortened form of the real name is fine; a longer one is a new claim.
        if needle in hay and len(needle) >= 0.6 * len(hay):
            return True
    return False


# Models routinely swap curly quotes for straight ones. That is typography, not
# an edit, and the Word writer ignores it — so the reported line count must too,
# or the changelog claims changes the delivered file does not contain.
_TYPOGRAPHY = str.maketrans({
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2014": "\u2013", "\u00a0": " ",
})


def _content_lines(text: str) -> list[str]:
    return [
        line.translate(_TYPOGRAPHY).strip()
        for line in text.splitlines()
        if line.strip()
    ]


def _added_content_lines(base: str, tailored: str) -> int:
    """Content lines in the tailored resume with no counterpart in the base."""
    matcher = difflib.SequenceMatcher(
        a=_content_lines(base),
        b=_content_lines(tailored),
    )
    added = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "replace":
            added += max(0, (j2 - j1) - (i2 - i1))
    return added


def _removed_content_lines(base: str, tailored: str) -> int:
    """Content lines present in the base that no longer have a counterpart."""
    matcher = difflib.SequenceMatcher(
        a=_content_lines(base),
        b=_content_lines(tailored),
    )
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            removed += max(0, (i2 - i1) - (j2 - j1))
    return removed


def _changed_content_lines(base: str, tailored: str) -> int:
    matcher = difflib.SequenceMatcher(
        a=_content_lines(base),
        b=_content_lines(tailored),
    )
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed += max(i2 - i1, j2 - j1)
    return changed
