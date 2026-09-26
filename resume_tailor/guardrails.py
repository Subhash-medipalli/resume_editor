"""Preserve document identity, history, structure, and metrics during tailoring."""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from resume_tailor.structure import DATE_SPAN_RE

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
# An email address or a phone number — the details this guardrail exists to protect.
CONTACT_MARKER_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ()./-]{7,}\d"
)
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
H3_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class GuardrailReport:
    ok: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    restored_contact: bool = False
    changed_line_count: int = 0
    review_items: list[dict] = field(default_factory=list)


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
    return ProtectedFacts(
        contact_block=contact,
        h2_headings=h2,
        job_headings=job_headings,
        companies=tuple(companies),
        titles=tuple(titles),
        date_spans=date_spans,
        education_lines=education_lines,
        cert_lines=cert_lines,
    )


def apply_guardrails(base: str, tailored: str) -> tuple[str, GuardrailReport]:
    """Protect identity, work history, qualifications, and numbers.

    Skills and wording may change freely. A line that changes a number or claims
    a new certification gets its original back instead of failing the whole run.
    """
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

    out, undone = _repair_lines(base_n, out)
    for line in undone:
        report.warnings.append(
            "Kept your original line because the edit changed a number or claimed a "
            f"certification: “{line[:70]}…”"
        )

    out_facts = extract_facts(out)
    report.violations.extend(_check_bound_facts(base_n, out))

    if len(out_facts.h2_headings) != len(base_facts.h2_headings):
        report.violations.append(
            "Section count changed "
            f"({len(base_facts.h2_headings)} → {len(out_facts.h2_headings)}). "
            "Preserve section order and structure."
        )
    elif tuple(_fact_text(h) for h in base_facts.h2_headings) != tuple(_fact_text(h) for h in out_facts.h2_headings):
        report.violations.append("Section headings or order changed. Preserve the document structure.")

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
    if _named_history(base_n) != _named_history(out):
        report.violations.append("Named clients or projects changed. Preserve their names.")
    known = _known_credentials(base_n)
    if any(_credential_claims(line) - known for line in out.splitlines()):
        report.violations.append("A certification claim is not on the base resume.")

    report.changed_line_count = _changed_content_lines(base_n, out)
    report.review_items = review_edits(base_n, out)
    # Compare quantities inside each role/section, so broad rewrites and bullet
    # reordering work without allowing a metric to migrate to another employer.
    original_scopes, edited_scopes = _scopes(base_n), _scopes(out)
    for section in dict.fromkeys([*original_scopes, *edited_scopes]):
        old_lines = [line for _, line in original_scopes.get(section, [])]
        new_lines = [line for _, line in edited_scopes.get(section, [])]
        if any(not line.lstrip().startswith("#") for line in old_lines) and not any(
            not line.lstrip().startswith("#") for line in new_lines
        ):
            report.violations.append(f"Section or role content was emptied in {section}.")
        if any(line.lstrip().startswith("- ") for line in old_lines) and not any(
            line.lstrip().startswith("- ") for line in new_lines
        ):
            report.violations.append(f"All bullets were removed or flattened in {section}. Preserve bullet structure.")
        # _repair_lines has already undone isolated edits; what remains is structural.
        if Counter(_quantities("\n".join(old_lines))) != Counter(_quantities("\n".join(new_lines))):
            report.violations.append(
                f"Numbers or units changed in {section}. Preserve metrics with their original role or section."
            )
    report.ok = not report.violations
    if not out.endswith("\n"):
        out += "\n"
    return out, report


def _fact_text(text: str) -> str:
    text = DATE_SPAN_RE.sub(lambda m: _canon_span(m.group()), text)
    return re.sub(r"\s+", " ", text.translate(_TYPOGRAPHY)).strip().lower()


def _check_bound_facts(base: str, out: str) -> list[str]:
    """Keep jobs in order with their own titles/dates, and freeze qualifications."""
    def jobs(text):
        records = []
        for match in H3_RE.finditer(text):
            body = re.split(r"^#{2,3}\s", text[match.end():], maxsplit=1, flags=re.MULTILINE)[0]
            first = next((line.strip() for line in body.splitlines() if line.strip()), "")
            role = first if re.fullmatch(r"\*\*(.+?)\*\*", first) else ""
            records.append((_fact_text(match.group(1)), _fact_text(role),
                            tuple(_canon_span(m.group()) for m in DATE_SPAN_RE.finditer(body))))
        return records

    def qualifications(text):
        sections = []
        for match in H2_RE.finditer(text):
            if re.search(r"educat|academic|certificat|credential|licen[sc]", match.group(1), re.I):
                body = re.split(r"^##\s", text[match.end():], maxsplit=1, flags=re.MULTILINE)[0]
                sections.append(_fact_text(body))
        return sections

    problems = []
    if jobs(base) != jobs(out):
        problems.append("A job's employer, title, dates, or order changed. Keep each job's facts together.")
    if qualifications(base) != qualifications(out):
        problems.append("Education or certifications changed. Do not add, remove, or rewrite qualifications.")
    return problems


def _quantities(text: str) -> list[str]:
    """Find achievement quantities without mistaking OAuth 2.0 for an outcome.

    A unit or scale marker distinguishes a metric from a product version/standard.
    "11+ years" and "11 years" are the same claim. Named-history/date checks
    protect historical fields independently.
    """
    number = r"(?<![\w.])\d[\d,]*(?:\.\d+)?"
    units = (
        r"ms|seconds?|minutes?|hours?|days?|weeks?|months?|years?|percent|x|"
        r"[kmb]|thousand|million|billion|users?|customers?|clients?|records?|"
        r"models?|engineers?|people|teams?|documents?|types?|requests?|events?|"
        r"services?|pipelines?|endpoints?|jobs?|tests?|deployments?|transactions?|"
        r"calls?|applications?|projects?|servers?|clusters?|regions?|databases?|"
        r"terabytes?|gigabytes?|petabytes?|TB|GB|PB"
    )
    pattern = rf"[$€£]\s*{number}\s*(?:[kmb]\b|thousand\b|million\b|billion\b)?|{number}\s*\+?\s*(?:%|(?:{units})\b)|\b\d{{1,3}}(?:,\d{{3}})+\+?"
    return [re.sub(r"[\s+]", "", m.group()).lower() for m in re.finditer(pattern, text, re.I)]


def _repair_lines(base: str, out: str) -> tuple[str, list[str]]:
    """Undo edits that add or drop a number in a role, or claim a new certification.

    Each edited line is paired with the original it most resembles. A flagged
    edit gets that original back, an added line is dropped, and a deleted
    original that carried a number returns, so one bad line never costs the run.
    ponytail: "Java 8 applications" reads as a new "8 applications" claim, so
    such a rewrite keeps the original bullet. Exempt product versions if that bites.
    """
    original, edited = _scopes(base), _scopes(out)

    def counts(items):
        return Counter(_quantities("\n".join(line for _, line in items)))

    # Numbers may move within a role; only a gain or loss for the role is undone.
    added = {scope: counts(items) - counts(original.get(scope, [])) for scope, items in edited.items()}
    lost = {scope: counts(items) - counts(edited.get(scope, [])) for scope, items in original.items()}
    base_scope = {number: scope for scope, items in original.items() for number, _ in items}
    out_scope = {number: scope for scope, items in edited.items() for number, _ in items}
    numbers_moved = any(added.values()) or any(lost.values())
    known = _known_credentials(base)
    base_lines, out_lines = base.splitlines(), out.splitlines()

    def numbers(text):
        return Counter(_quantities(text))

    def gained(j, i):
        extra = numbers(out_lines[j]) - (numbers(base_lines[i]) if i is not None else Counter())
        return extra & added.get(out_scope.get(j + 1), Counter())

    def dropped(i, j):
        missing = numbers(base_lines[i]) - (numbers(out_lines[j]) if j is not None else Counter())
        return missing & lost.get(base_scope.get(i + 1), Counter())

    matcher = difflib.SequenceMatcher(
        a=[line.strip() for line in base_lines], b=[line.strip() for line in out_lines], autojunk=False
    )
    repaired: list[str] = []
    undone: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            repaired.extend(out_lines[j1:j2])
            continue
        claims = any(_credential_claims(out_lines[j]) - known for j in range(j1, j2))
        pairs = _pair_rewrites(base_lines, range(i1, i2), out_lines, range(j1, j2)) if numbers_moved or claims else {}
        for j in range(j1, j2):
            i = pairs.get(j)
            if gained(j, i) or (i is not None and dropped(i, j)) or _credential_claims(out_lines[j]) - known:
                undone.append(out_lines[j].strip())
                if i is not None:
                    repaired.append(base_lines[i])
            else:
                repaired.append(out_lines[j])
        for i in sorted(set(range(i1, i2)) - set(pairs.values())):
            if dropped(i, None):  # a deleted line that carried a number returns
                undone.append(base_lines[i].strip())
                repaired.append(base_lines[i])
    return "\n".join(repaired) + ("\n" if out.endswith("\n") else ""), undone


def _pair_rewrites(base_lines, base_range, out_lines, out_range) -> dict[int, int]:
    """Pair each edited line with the original it most resembles, most similar first."""
    scored = sorted(
        ((difflib.SequenceMatcher(None, base_lines[i], out_lines[j]).ratio(), i, j)
         for i in base_range for j in out_range),
        reverse=True,
    )
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for ratio, i, j in scored:
        if ratio >= 0.35 and j not in pairs and i not in used:
            pairs[j] = i
            used.add(i)
    rest_i = [i for i in base_range if i not in used]
    rest_j = [j for j in out_range if j not in pairs]
    if len(rest_i) == len(rest_j) == 1:  # one line rewritten beyond recognition
        pairs[rest_j[0]] = rest_i[0]
    return pairs


_CERT_WORD_RE = re.compile(r"certified|certifications?", re.I)
# Words that end a certification name: "AWS Certified Solutions Architect with Python".
_NAME_STOP = frozenset("""a active an and are as at by current for from having in including is multiple of on or
plus relevant that the to using valid various who with""".split())
_DASHES = {"-", "\u2013", "\u2014"}


def _is_name_word(token: str) -> bool:
    return bool(re.match(r"[^\W\d]", token)) and token.lower() not in _NAME_STOP


def _name_words(tokens) -> set[str]:
    return {part for token in tokens for part in token.lower().strip(".").split("-")
            if part and part not in _NAME_STOP}


def _credential_claims(line: str) -> set[str]:
    """Words naming each certification claimed on `line`, plus "*" when one is claimed.

    Reads "AWS Certified Solutions Architect", "PMP certification", "CKA-certified",
    "certified in Kubernetes", and "Certifications: AZ-900, CKA".
    ponytail: word rules, not a certification list. Match a curated list if these misfire.
    """
    tokens = re.findall(r"[\w+#.-]+|[^\w\s]", line.strip().lstrip("-*#> "))
    claims: set[str] = set()
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"(?:([\w+#.]+)-)?(?:certified|certifications?)", token, re.I)
        if not match:
            continue
        claims.add("*")
        if match.group(1):  # "CKA-certified": the prefix is the name
            claims |= _name_words([match.group(1)])
            continue
        before: list[str] = []
        for position in range(index - 1, -1, -1):
            token = tokens[position]
            if token in _DASHES:
                continue
            if not _is_name_word(token) or len(before) == 3 or (position == 0 and token.istitle()):
                break  # a sentence-initial verb ("Earned") names nothing
            before.append(token)
        claims |= _name_words(before)
        rest = tokens[index + 1:]
        if rest[:1] == [":"]:  # "Certifications: AZ-900, CKA"
            claims |= _name_words(word for word in rest[1:] if _is_name_word(word))
            continue
        after: list[str] = []
        for token in rest[1:] if rest[:1] and rest[0].lower() == "in" else rest:  # "certified in Kubernetes"
            if token in _DASHES:
                continue  # "Solutions Architect – Professional" names the level too
            if not _is_name_word(token) or len(after) == 4:
                break
            after.append(token)
        claims |= _name_words(after)
    return claims


def _known_credentials(text: str) -> set[str]:
    """Every word on the base's certification lines, so a real one can be named anywhere."""
    known: set[str] = set()
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line
        if _CERT_WORD_RE.search(line) or re.search(r"certif|licen[sc]", section, re.I):
            known |= _name_words(re.findall(r"[\w+#.-]+", line)) | {"*"}
    return known


def _scopes(text: str) -> dict[str, list[tuple[int, str]]]:
    groups: dict[str, list[tuple[int, str]]] = {}
    section = "Contact and headline"
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith(("## ", "### ")):
            section = _fact_text(line.lstrip("# "))
        if line.strip():
            groups.setdefault(section, []).append((number, line))
    return groups


def review_edits(base: str, out: str) -> list[dict]:
    """Before/after text, aligned within a section or job, with source lines."""
    original, edited = _scopes(base), _scopes(out)
    items = []
    for section in dict.fromkeys([*original, *edited]):
        old, new = original.get(section, []), edited.get(section, [])
        matcher = difflib.SequenceMatcher(a=[_fact_text(l) for _, l in old],
                                         b=[_fact_text(l) for _, l in new], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                items.append({"section": section,
                              "source_lines": [n for n, _ in old[i1:i2]],
                              "before": "\n".join(l for _, l in old[i1:i2]),
                              "after": "\n".join(l for _, l in new[j1:j2])})
    return items


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
    span = re.sub(r"\b(?:current|now)\b", "Present", span, flags=re.I)
    return span.lower()


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

    out_boundary = _last_identity_line(out_lines)
    # Locate the contact boundary in each header independently. A new headline
    # changes header length; that must not restore the entire old header.
    if boundary >= 0 and out_boundary >= 0:
        original_identity = base_lines[:boundary + 1]
        current_identity = out_lines[:out_boundary + 1]
        if original_identity == current_identity:
            return out, False
        merged = original_identity + out_lines[out_boundary + 1:]
        trailing = out_header[len(out_header.rstrip("\n")) :]
        return "\n".join(merged) + trailing + out[len(out_header) :], True

    # If the contact block was dropped entirely, restore it without touching body.
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


# Only capitalised names count: "project: migrated the data lake" names nothing.
NAMED_HISTORY_RE = re.compile(
    r"\b(?i:client|project)\s*(?:(?i:named|called)|:)\s*[\"'“]?"
    r"([A-Z][A-Za-z0-9&.+-]*(?:\s+[A-Z][A-Za-z0-9&.+-]*){0,3})"
)


def _named_history(text: str) -> set[str]:
    """Named clients/projects are work history, even when the JD mentions them."""
    return {match.group(1).lower().strip() for match in NAMED_HISTORY_RE.finditer(text)}


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
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "—": "–", " ": " ",
})


def _content_lines(text: str) -> list[str]:
    return [
        line.translate(_TYPOGRAPHY).strip()
        for line in text.splitlines()
        if line.strip()
    ]


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
