"""Preserve document identity, history, structure, and metrics during tailoring."""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass, field
from resume_tailor.structure import BULLET, CONTACT_MARKER_RE, DATE_SPAN_RE, JOB, SECTION, TEXT, TITLE, from_markdown

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
QUALIFICATION_HEADING_RE = re.compile(r"educat|academic|certificat|credential|licen[sc]", re.I)


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
    blocks = from_markdown(text)
    job_headings = tuple(block.text for block in blocks if block.kind == JOB)
    companies: list[str] = []
    titles: list[str] = []
    for heading in job_headings:
        title, company = _split_job_heading(heading)
        if title:
            titles.append(title)
        if company:
            companies.append(company)
    for title in (block.text for block in blocks if block.kind == TITLE):
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

    Wording may change freely; preserve bullet depth and expand existing skills.
    A line that changes a number or claims
    a new degree/certification gets its original back instead of failing the run.
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
            "Kept your original line because the edit changed a number, added a "
            f"degree/certification claim, or named a tool released after that role ended: “{line[:70]}…”"
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
    if any(_has_unknown_credential(line, known) for line in out.splitlines()):
        report.violations.append("A certification claim is not on the base resume.")
    if _degree_levels(base_n) != _degree_levels(out):
        report.violations.append("A degree claim changed. Preserve the candidate's degree levels throughout the resume.")

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
    report.violations.extend(content_preservation_issues(base_n, out))
    report.ok = not report.violations
    if not out.endswith("\n"):
        out += "\n"
    return out, report


def content_preservation_issues(base: str, draft: str) -> list[str]:
    """Keep resume depth and existing skills while allowing complete JD rewrites."""
    original, edited = _scopes(base), _scopes(draft)
    problems = []

    def blocks(items):
        return from_markdown("\n".join(line for _, line in items))

    for scope, items in original.items():
        old = blocks(items)
        heading = old[0] if old else None
        if not heading or not (heading.kind == JOB or (
            heading.kind == SECTION and re.search(r"\b(?:summary|profile|objective)\b", heading.text, re.I)
        )):
            continue
        required = sum(block.kind == BULLET for block in old)
        actual = sum(block.kind == BULLET for block in blocks(edited.get(scope, [])))
        if actual < required:
            problems.append(
                f"Bullet count dropped in {scope}: expected at least {required}, found {actual}. "
                "Rewrite the existing points instead of removing or combining them."
            )

    def skill_lines(scopes):
        for items in scopes.values():
            parsed = blocks(items)
            if parsed and parsed[0].kind == SECTION and re.search(
                r"\b(?:skills?|competenc\w*|expertise|proficienc\w*)\b|\b(?:technical|technology|tech) stack\b",
                parsed[0].text, re.I,
            ):
                yield from (block.text for block in parsed[1:])

    # ponytail: explicit skill lists, not semantic aliases; preserve original names.
    # Parentheses split grouped tools such as AWS (EC2, S3), without losing AWS.
    existing = {}
    category_words = set("""programming scripting processing languages tools technologies
        technology technical skills services platforms cloud data databases analytics
        engineering architecture quality operations devops development frameworks
        libraries competencies expertise governance and & /""".split())
    for line in skill_lines(original):
        label, separator, values = line.partition(":")
        if separator:
            # Keep tool names used as labels (AWS, SQL Server), not generic categories.
            label_words = label.split()
            while label_words and label_words[0].casefold() in category_words:
                label_words.pop(0)
            while label_words and label_words[-1].casefold() in category_words:
                label_words.pop()
            values = ", ".join([" ".join(label_words), values])
        else:
            values = label
        for item in re.split(r"[,;()\n]|\s+[|/•]\s+", values):
            item = re.sub(r"^(?:and|or|&)\s+", "", item.strip(), flags=re.I).strip(" .")
            key = re.sub(r"\s+", " ", item.translate(_TYPOGRAPHY)).casefold()
            if key:
                existing.setdefault(key, item)
    available = re.sub(r"\s+", " ", "\n".join(skill_lines(edited)).translate(_TYPOGRAPHY)).casefold()
    missing = [label for key, label in existing.items()
               if not re.search(rf"(?<![\w+#]){re.escape(key)}(?![\w+#])", available)]
    if missing:
        problems.append("Existing skills were removed from the skills sections: " + ", ".join(missing)
                        + ". Preserve them while adding job-relevant skills.")
    return problems


def _fact_text(text: str) -> str:
    text = DATE_SPAN_RE.sub(lambda m: _canon_span(m.group()), text)
    return re.sub(r"\s+", " ", text.translate(_TYPOGRAPHY)).strip().lower()


def _check_bound_facts(base: str, out: str) -> list[str]:
    """Keep jobs in order with their own titles/dates, and freeze qualifications."""
    def jobs(text):
        records = []
        current = None
        # Use the Word writer's parser: it also recognizes Company | dates
        # without a markdown heading marker.
        for block in from_markdown(text):
            if block.kind == SECTION:
                current = None
            elif block.kind == JOB:
                current = [_fact_text(block.text), "", []]
                records.append(current)
            elif current is not None:
                if block.kind == TITLE:
                    current[1] = _fact_text(block.text)
                current[2].extend(_canon_span(m.group()) for m in DATE_SPAN_RE.finditer(block.text))
        return records

    def qualifications(text):
        sections = []
        for match in H2_RE.finditer(text):
            if QUALIFICATION_HEADING_RE.search(match.group(1)):
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
        r"models?|engineers?|persons?|people|teams?|documents?|types?|requests?|events?|"
        r"services?|pipelines?|endpoints?|jobs?|tests?|deployments?|transactions?|"
        r"calls?|applications?|projects?|servers?|clusters?|regions?|databases?|"
        r"terabytes?|gigabytes?|petabytes?|TB|GB|PB"
    )
    separator = r"[ \t]*(?:[-‐‑‒–—][ \t]*)?"
    pattern = rf"[$€£]\s*{number}\s*(?:[kmb]\b|thousand\b|million\b|billion\b)?|{number}\s*\+?{separator}(?:%|(?:{units})\b)|\b\d{{1,3}}(?:,\d{{3}})+\+?"
    quantities = []
    for match in re.finditer(pattern, text, re.I):
        value = re.sub(r"[\s+\-‐‑‒–—]", "", match.group()).lower()
        # "2-hour" and "2 hours" express the same quantity; keep ms intact.
        value = re.sub(r"([a-z]{2,})s$", r"\1", value)
        quantities.append(re.sub(r"people$", "person", value))
    return quantities


def _repair_lines(base: str, out: str) -> tuple[str, list[str]]:
    """Undo changed numbers, new qualifications, and tools newer than a role.

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
    degrees = _degree_levels(base)
    base_lines, out_lines = base.splitlines(), out.splitlines()
    qualification_lines = set()
    in_qualifications = False
    for j, line in enumerate(out_lines):
        heading = H2_RE.fullmatch(line)
        if heading:
            in_qualifications = bool(QUALIFICATION_HEADING_RE.search(heading.group(1)))
        if in_qualifications:
            qualification_lines.add(j)

    def new_claim(j):
        # Only an enclosing H2 section freezes qualifications. A job's employer
        # may contain words such as Education or Licensing without being one.
        return _has_unknown_credential(out_lines[j], known) or (
            j not in qualification_lines and bool(_degree_levels(out_lines[j]) - degrees))

    role_ends = {scope: _role_end_year("\n".join(line for _, line in items))
                 for scope, items in original.items()}
    role_tools = {scope: _tools("\n".join(line for _, line in items))
                  for scope, items in original.items() if role_ends[scope] is not None}

    def too_new(j):
        """Tools on edited line j released after its role ended, unless the original role named them."""
        scope = out_scope.get(j + 1)
        end = role_ends.get(scope)
        if end is None:
            return set()
        return {tool for tool in _tools(out_lines[j]) - role_tools[scope] if _TOOL_RELEASE_YEAR[tool] > end}

    def numbers(text):
        return Counter(_quantities(text))

    def gained(j, i):
        extra = numbers(out_lines[j]) - (numbers(base_lines[i]) if i is not None else Counter())
        return extra & added.get(out_scope.get(j + 1), Counter())

    def dropped(i, j):
        missing = numbers(base_lines[i]) - (numbers(out_lines[j]) if j is not None else Counter())
        return missing & lost.get(base_scope.get(i + 1), Counter())

    matcher = difflib.SequenceMatcher(
        a=[(base_scope.get(i + 1), line.strip()) for i, line in enumerate(base_lines)],
        b=[(out_scope.get(j + 1), line.strip()) for j, line in enumerate(out_lines)],
        autojunk=False,
    )
    repaired: list[str] = []
    undone: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            repaired.extend(out_lines[j1:j2])
            continue
        claims = any(new_claim(j) or too_new(j) for j in range(j1, j2))
        pairs = {}
        if numbers_moved or claims:
            # An identical bullet moved to a different role is still an edit.
            # Pair only within that role, including the single-line fallback.
            for scope in dict.fromkeys(out_scope.get(j + 1) for j in range(j1, j2)):
                old = [i for i in range(i1, i2) if base_scope.get(i + 1) == scope]
                new = [j for j in range(j1, j2) if out_scope.get(j + 1) == scope]
                pairs.update(_pair_rewrites(base_lines, old, out_lines, new))
        for j in range(j1, j2):
            i = pairs.get(j)
            if gained(j, i) or (i is not None and dropped(i, j)) or new_claim(j) or too_new(j):
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


# Earliest public preview/open release, not company founding or private development.
# Sources and audit limits: docs/tool-release-years.md. ponytail: a hand-kept list;
# add a sourced tool when a model back-dates one that is missing.
_TOOL_RELEASE_YEAR = {
    # AWS
    "EMR": 2009, "RDS": 2009, "CloudFormation": 2011, "DynamoDB": 2012, "Redshift": 2012,
    "Kinesis": 2013, "Lambda": 2014, "Aurora": 2014, "KMS": 2014, "ECS": 2014,
    "API Gateway": 2015, "QuickSight": 2015, "Glue": 2017, "Athena": 2016, "Step Functions": 2016,
    "SageMaker": 2017, "EKS": 2017, "Fargate": 2017, "Secrets Manager": 2018, "Lake Formation": 2018,
    "MSK": 2018, "EventBridge": 2019, "OpenSearch": 2021, "Bedrock": 2023,
    # Azure
    "HDInsight": 2012, "Azure Data Factory": 2014, "Event Hubs": 2014, "ADLS": 2015,
    "Azure Functions": 2016, "Cosmos DB": 2017, "Azure Databricks": 2017, "AKS": 2017,
    "Azure DevOps": 2018, "Synapse": 2019, "Azure OpenAI": 2021, "Microsoft Fabric": 2023,
    "Azure AI Foundry": 2024,
    # Google Cloud
    "BigQuery": 2010, "Dataproc": 2015, "Cloud Composer": 2018, "Vertex AI": 2021,
    # Data platforms
    "Kafka": 2011, "Spark": 2010, "PySpark": 2013, "Presto": 2013, "Databricks": 2014,
    "Snowflake": 2014, "Flink": 2014, "NiFi": 2014, "Airflow": 2015, "dbt": 2016, "Hudi": 2017,
    "Iceberg": 2017, "Delta Lake": 2018, "Great Expectations": 2017, "Trino": 2020,
    "Snowpark": 2021, "Airbyte": 2020, "Unity Catalog": 2022, "Delta Live Tables": 2021,
    "Databricks Genie": 2024, "Agent Bricks": 2025,
    # DevOps
    "Docker": 2013, "Kubernetes": 2014, "Terraform": 2014, "GitHub Actions": 2018,
    # ML and AI
    "TensorFlow": 2015, "PyTorch": 2017, "FAISS": 2017, "MLflow": 2018, "Pinecone": 2021,
    "RAG": 2020, "ChatGPT": 2022, "LangChain": 2022, "LlamaIndex": 2022, "GPT-4": 2023,
    "CrewAI": 2023, "AutoGen": 2023, "LangGraph": 2024, "Model Context Protocol": 2024,
}
_TOOL_RE = re.compile(
    r"(?<!\w)(" + "|".join(sorted(map(re.escape, _TOOL_RELEASE_YEAR), key=len, reverse=True)) + r")(?!\w)"
)


def _tools(text: str) -> set[str]:
    return set(_TOOL_RE.findall(text))


def _role_end_year(markdown: str) -> int | None:
    """Read original job metadata, never a section heading or dates in a bullet."""
    blocks = from_markdown(markdown)
    if not blocks or blocks[0].kind != JOB:
        return None
    for block in blocks:
        if block.kind not in (JOB, TITLE, TEXT):
            break
        text = block.text.strip("* ")
        span = DATE_SPAN_RE.search(text) if block.kind == JOB else DATE_SPAN_RE.match(text)
        if span:
            if re.search(r"present|current|now", span.group(), re.I):
                return None
            return int(re.findall(r"\d{4}", span.group())[-1])
        if block.kind not in (JOB, TITLE):
            break
    return None


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


# Words that end a certification name: "AWS Certified Solutions Architect with Python".
_NAME_STOP = frozenset("""a active an and are as at by current for from having in including is multiple of on or
plus relevant that the to using valid various who with earned holds holding obtained completed achieved
received maintained working certified certification certifications""".split())
_DASHES = {"-", "\u2013", "\u2014"}


def _is_name_word(token: str) -> bool:
    return bool(re.match(r"[^\W\d]", token)) and token.lower() not in _NAME_STOP


def _name_words(tokens) -> set[str]:
    return {part for token in tokens for part in token.lower().strip(".").split("-")
            if part and part not in _NAME_STOP}


def _credential_claims(line: str) -> list[frozenset[str]]:
    """Separate word sets for each certification explicitly claimed on `line`.

    Reads "AWS Certified Solutions Architect", "PMP certification", "CKA-certified",
    "certified in Kubernetes", and "Certifications: AZ-900, CKA".
    ponytail: word rules, not a certification list. Match a curated list if these misfire.
    """
    # Keep internal dots in acronyms, but sentence-ending periods are boundaries.
    tokens = re.findall(r"[\w+#-]+(?:\.[\w+#-]+)*|[^\w\s]", line.strip().lstrip("-*#> "))
    claims: list[frozenset[str]] = []
    for index, token in enumerate(tokens):
        match = re.fullmatch(r"(?:([\w+#.]+)-)?(?:certified|certifications?)", token, re.I)
        if not match:
            continue
        words = {"*"}
        if match.group(1):  # "CKA-certified": the prefix is the name
            claims.append(frozenset(words | _name_words([match.group(1)])))
            continue
        before: list[str] = []
        for position in range(index - 1, -1, -1):
            token = tokens[position]
            if token in _DASHES:
                continue
            if not _is_name_word(token) or len(before) == 3:
                break
            before.append(token)
        words |= _name_words(before)
        rest = tokens[index + 1:]
        if rest[:1] == [":"]:  # "Certifications: AZ-900, CKA"
            # Each list member must match a single real credential, too.
            for name in re.split(r"[,;|]|\band\b", " ".join(rest[1:]), flags=re.I):
                claims.append(frozenset(words | _name_words(
                    token for token in name.split() if _is_name_word(token))))
            continue
        after: list[str] = []
        for token in rest[1:] if rest[:1] and rest[0].lower() == "in" else rest:  # "certified in Kubernetes"
            if token in _DASHES:
                continue  # "Solutions Architect – Professional" names the level too
            if not _is_name_word(token) or len(after) == 4:
                break
            after.append(token)
        claims.append(frozenset(words | _name_words(after)))
    return claims


def _known_credentials(text: str) -> list[frozenset[str]]:
    """Keep credential identities separate, so unrelated names cannot authorize upgrades."""
    known: list[frozenset[str]] = []
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line
            continue
        if re.search(r"certif|licen[sc]", section, re.I):
            for name in re.split(r"[,;|]|\band\b", line, flags=re.I):
                words = _name_words(re.findall(r"[\w+#.-]+", name))
                if words:
                    known.append(frozenset(words | {"*"}))
        else:
            known.extend(_credential_claims(line))
    return known


def _has_unknown_credential(line: str, known: list[frozenset[str]]) -> bool:
    return any(not any(claim <= original for original in known)
               for claim in _credential_claims(line))


def _degree_levels(text: str) -> set[str]:
    """Recognize degree claims anywhere without treating tools like MS SQL as degrees.

    This checks common degree levels; qualification-section checks separately
    preserve the exact institution, subject, and dates recorded there.
    """
    # Normalize this role only for claim detection, preserving the actual resume
    # text and recognizing Word's nonbreaking spaces and typographic hyphens.
    text = re.sub(r"\bscrum[\s\-‐‑‒–—]+master\b", "ScrumMaster", text, flags=re.I)
    levels: set[str] = set()
    patterns = {
        "doctorate": r"\b(?:Ph\.?\s*D\.?|doctorate|doctoral\s+degree|doctor\s+of\s+\w+)(?!\w)",
        "master": r"\b(?:M\.(?:Sc|S|A|Eng)\.?|MSc|MBA|MTech|MEng|master['’]?s?[ -]+(?:degree|holder|qualified|graduate|(?:of|in)\s+\w+))(?!\w)",
        "bachelor": r"\b(?:B\.(?:Sc|S|A|Eng)\.?|BSc|BBA|BTech|BEng|bachelor['’]?s?[ -]+(?:degree|holder|qualified|graduate|(?:of|in)\s+\w+))(?!\w)",
        "associate": r"\b(?:A\.(?:S|A)\.?|associate['’]?s?\s+(?:degree|of\s+\w+))(?!\w)",
    }
    for level, pattern in patterns.items():
        if re.search(pattern, text, re.I):
            levels.add(level)
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line
        for abbreviation, level in (("MS|MA", "master"), ("BS|BA", "bachelor"), ("AS|AA", "associate")):
            if re.search(r"educat|academic", section, re.I):
                pattern = rf"\b(?:{abbreviation})\b"
            else:
                # Bare acronyms require an explicit educational phrase.
                pattern = (rf"\b(?:{abbreviation})(?=\s+(?i:degree|in\b|of\b)|[- ](?i:qualified|educated|graduate)\b)"
                           rf"|\b(?i:earned|holds?|completed|received)\s+(?i:an?\s+)?(?:{abbreviation})\b")
            if re.search(pattern, line):
                levels.add(level)
    return levels


def _scopes(text: str) -> dict[str, list[tuple[int, str]]]:
    groups: dict[str, list[tuple[int, str]]] = {}
    section = "Contact and headline"
    role = 0
    lines = [(number, line) for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    for (number, line), block in zip(lines, from_markdown(text)):
        if block.kind == SECTION:
            section = _fact_text(block.text)
        elif block.kind == JOB:
            role += 1
            section = f"Role {role}: {_fact_text(block.text)}"
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

    Everything up to it is identity (name, email, phone, links); anything after it is
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
    r"\b(?i:client|project)(?P<marker>[ \t]+(?i:named|called)[ \t]+|[ \t]*:[ \t]*|[ \t]+)[\"'“]?"
    r"(?P<name>[A-Z][A-Za-z0-9&.+-]*(?:[ \t]+[A-Z][A-Za-z0-9&.+-]*){0,3})"
)
_HISTORY_DESCRIPTORS = frozenset("""api apis sdk sdks ui ux management planning delivery
coordination lifecycle development architecture services support success""".split())


def _named_history(text: str) -> set[str]:
    """Named clients/projects are work history, even when the JD mentions them."""
    # ponytail: a bounded name grammar, not entity recognition. Direct names
    # need relationship context or a standalone label; "client OAuth integration"
    # is a capability. Explicit named/called/colon labels always identify history.
    names = set()
    for match in NAMED_HISTORY_RE.finditer(text):
        name = re.split(r"(?<=[a-z0-9])\.[ \t]+", match["name"], maxsplit=1)[0]
        explicit = bool(re.search(r"named|called|:", match["marker"], re.I))
        prefix = text[text.rfind("\n", 0, match.start()) + 1:match.start()].strip(" -*")
        relationship = bool(re.search(r"\b(?:for|at|with|on)\s+(?:the\s+)?$", prefix + " ", re.I))
        remaining = text[match.end():].split("\n", 1)[0].strip(" .:*\"'”")
        standalone = not prefix and not remaining
        if not explicit:
            if name.split()[0].lower().rstrip(".") in _HISTORY_DESCRIPTORS:
                continue
            if not relationship and (not standalone or re.search(r"\b(?:SDKs?|APIs?)\b", name, re.I)):
                continue
        names.add(name.lower().strip().rstrip("."))
    return names


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
