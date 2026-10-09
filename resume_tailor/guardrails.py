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
    a new degree/certification gets its original back instead of failing the run,
    and dropped points or skills are put back.
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

    out, retargeted = _restore_headline(base_n, out)
    if retargeted:
        original = _headline_text(_contact_block(base_n))
        report.warnings.append(
            f"Kept your original headline: “{original}”" if original
            else "Removed a headline the edit added; your resume has none."
        )

    out, kept = restore_frozen(base_n, out)
    report.warnings.extend(kept)

    # One repair can strand a number that moved from a sibling line, so repeat.
    undone: list[str] = []
    for _ in range(3):
        out, more = _repair_lines(base_n, out)
        undone += more
        if not more:
            break
    for line in undone:
        report.warnings.append(
            "Kept your original line because the edit changed a number, a date, or a client/project name, "
            "added a degree/certification or location/work-authorization claim, or named a tool released after "
            f"that role ended: “{line[:70]}…”"
        )
    # A repair can drop an added point; restoring afterwards keeps the count whole.
    out, restored = restore_content(base_n, out)
    report.warnings.extend(restored)

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
    # tailor_resume already spent the corrective retry on merged points.
    report.violations.extend(content_preservation_issues(base_n, out, allow_merges=True))
    report.ok = not report.violations
    if not out.endswith("\n"):
        out += "\n"
    return out, report


def restore_frozen(base: str, out: str) -> tuple[str, list[str]]:
    """Put back reworded section headings, job headings and titles, and qualification sections.

    The checks require these to match the original, so when the structure still
    lines up (same number of sections and of jobs) the original wording goes
    back instead of failing the run. A changed structure or order is left for the checks.
    """
    def layout(text):
        lines = text.splitlines()
        return lines, dict(zip((i for i, line in enumerate(lines) if line.strip()), from_markdown(text)))

    base_lines, base_blocks = layout(base)
    lines, blocks = layout(out)
    notes: list[str] = []

    def at(found, kind):
        return [i for i, block in found.items() if block.kind == kind]

    def following(found, i):
        return next((j for j in found if j > i), None)

    def lead(found, i):
        """Non-bullet lines between a job heading and its first point."""
        after = [k for k in found if k > i]
        return [k for k in after[:next((n for n, k in enumerate(after)
                                         if found[k].kind in (BULLET, JOB, SECTION)), len(after))]]

    aligned = {}
    for kind, name in ((SECTION, "section heading"), (JOB, "job heading")):
        old, new = at(base_blocks, kind), at(blocks, kind)
        before = [_fact_text(base_blocks[i].text) for i in old]
        after = [_fact_text(blocks[j].text) for j in new]
        # A reordered section or job is not a rewording: restoring by position
        # would put the wrong heading over its content. Each changed heading must
        # resemble its own original more than any other changed one.
        changed = [k for k in range(len(before)) if len(old) == len(new) and after[k] != before[k]]

        def resembles(k):
            # A replaced section ("Awards" -> "Selected Projects") is not a rewording;
            # a renamed one still holds its own content.
            if _similarity(after[k], before[k]) >= 0.5:
                return True
            if kind != SECTION:
                return False
            mine = _content_words("\n".join(base_lines[old[k] + 1:next((i for i in old if i > old[k]), len(base_lines))]))
            theirs = _content_words("\n".join(lines[new[k] + 1:next((j for j in new if j > new[k]), len(lines))]))
            return len(mine & theirs) >= max(1, 0.3 * min(len(mine), len(theirs)))

        aligned[kind] = len(old) == len(new) and all(
            max(changed, key=lambda k: _similarity(after[index], before[k])) == index and resembles(index)
            for index in changed)
        if not aligned[kind]:
            continue
        for i, j in zip(old, new):
            if before[old.index(i)] != after[new.index(j)]:
                lines[j] = base_lines[i]
                notes.append(f"Kept your original {name}: “{base_blocks[i].text}”")
            ti, tj = following(base_blocks, i), following(blocks, j)
            # A reworded title, or one that lost its bold, goes back as written, but
            # only when the lines under the heading still line up one to one: an extra
            # bold line the model inserted must not be overwritten into a second title.
            if (kind == JOB and ti is not None and tj is not None and base_blocks[ti].kind == TITLE
                    and len(lead(base_blocks, i)) == len(lead(blocks, j))
                    and (blocks[tj].kind == TITLE or _fact_text(blocks[tj].text) == _fact_text(base_blocks[ti].text))
                    and lines[tj] != base_lines[ti]):
                if _fact_text(blocks[tj].text) != _fact_text(base_blocks[ti].text):
                    notes.append(f"Kept your original job title: “{base_blocks[ti].text}”")
                lines[tj] = base_lines[ti]

    if aligned[SECTION]:
        old, new = at(base_blocks, SECTION), at(blocks, SECTION)
        for i, j in reversed(list(zip(old, new))):
            if not QUALIFICATION_HEADING_RE.search(base_blocks[i].text):
                continue
            i_end = next((k for k in old if k > i), len(base_lines))
            j_end = next((k for k in new if k > j), len(lines))
            if _fact_text("\n".join(base_lines[i + 1:i_end])) != _fact_text("\n".join(lines[j + 1:j_end])):
                lines[j + 1:j_end] = base_lines[i + 1:i_end]
                notes.append(f"Kept your {base_blocks[i].text} section exactly as written.")
    if not notes and lines == out.splitlines():
        return out, []
    return "\n".join(lines) + ("\n" if out.endswith("\n") else ""), notes


def content_preservation_issues(base: str, draft: str, *, allow_merges: bool = False) -> list[str]:
    """Keep resume depth and existing skills while allowing complete JD rewrites.

    With allow_merges, an original point whose words live on inside another point
    of the same role counts as kept (used once the corrective retry is spent).
    """
    original, edited = _scopes(base), _scopes(draft)
    base_lines, lines = base.splitlines(), draft.splitlines()
    problems = []
    for scope, items in _point_scopes(original):
        old = [number - 1 for number in _bullet_numbers(items)]
        new = [number - 1 for number in _bullet_numbers(edited.get(scope, []))]
        kept = len(new) + (len(_merged_points(base_lines, old, lines, new))
                           if allow_merges and 0 < len(new) < len(old) else 0)
        if kept < len(old):
            problems.append(
                f"Bullet count dropped in {scope}: expected at least {len(old)}, found {len(new)}. "
                "Rewrite the existing points instead of removing or combining them."
            )
    missing = [item for item, _ in _missing_skills(original, edited)]
    if missing:
        problems.append("Existing skills were removed from the skills sections: " + ", ".join(missing)
                        + ". Preserve them while adding job-relevant skills.")
    return problems


def restore_content(base: str, draft: str, *, points: bool = True) -> tuple[str, list[str]]:
    """Put back skills, and optionally points, that the model dropped.

    Skills are only ever appended to the end of a line in their own skills
    section: the line with their original label, else the line holding most of
    their surviving original names, else one reworded from their original line;
    otherwise their original label returns as a new line with just the missing
    names. A point returns after the nearest original line of its role that
    survived, never past a sub-heading ("Client: ...") the draft lost, unless it
    merged into another point of the role (noted, not repeated), moved to
    another role, or its numbers live on elsewhere in the role: repeating those
    would duplicate a claim, so content_preservation_issues keeps reporting them.
    An emptied section or role is left to the corrective retry.
    """
    original, edited = _scopes(base), _scopes(draft)
    base_lines, lines = base.splitlines(), draft.splitlines()
    inserts: dict[int, list[tuple[int, str]]] = {}  # draft index -> (base index, line) placed after it
    notes: list[str] = []

    targets = _skill_lines(edited)
    available = _skill_key("\n".join(text for _, _, text in targets))
    groups: dict[tuple[str, int, str], list[str]] = {}
    for item, (scope, number, source) in _missing_skills(original, edited):
        groups.setdefault((scope, number, source), []).append(item)
    restored: list[str] = []
    for (scope, number, source), items in groups.items():
        section = [(n, text) for s, n, text in targets if s == scope]
        if not section:
            continue
        restored += items
        # Names another original line also lists say nothing about where this line went.
        elsewhere = {_skill_key(name) for _, n, text in _skill_lines(original) if n != number
                     for name in _skill_items(text)}
        names = {_skill_key(name) for name in _skill_items(source)} - elsewhere
        survivors = {name for name in names if _has_skill(name, available)}
        label = _skill_key(source.partition(":")[0]) if ":" in source else None
        own = {_skill_key(name) for name in _skill_items(source.partition(":")[0] + ":")} if label else set()
        listed = [item for item in items if _skill_key(item) not in own]

        def fit(target):
            text = target[1]
            held = names & {_skill_key(name) for name in _skill_items(text)}
            return (label is not None and ":" in text and _skill_key(text.partition(":")[0]) == label,
                    len(held) >= 2 and len(held) > len(survivors) / 2,
                    _similarity(_skill_key(source), _skill_key(text)))

        best = max(section, key=fit)
        same_label, holds_most, similarity = fit(best)
        # Only label words such as "ETL" are missing: they join the line reworded from theirs.
        if same_label or holds_most or similarity >= 0.6 or not listed:
            lines[best[0] - 1] = lines[best[0] - 1].rstrip().rstrip(",;.") + ", " + ", ".join(items)
            continue
        raw = base_lines[number - 1]
        head = re.match(r"\s*(?:[-*]\s+)?" + (r"(?:\*\*)?[^:]*:(?:\*\*)?" if label else ""), raw).group()
        # The new line goes after the line that preceded it originally, else at the section start.
        earlier = [text for s, n, text in _skill_lines(original) if s == scope and n < number]
        anchor = edited[scope][0][0] if not earlier else next(
            (n for n, text in section if _skill_key(text) == _skill_key(earlier[-1])), section[-1][0])
        inserts.setdefault(anchor - 1, []).append((number - 1, f"{head.rstrip()} {', '.join(listed)}"))
    if restored:
        notes.append("Restored skills the model removed from your skills section: " + ", ".join(restored) + ".")

    if points:
        roles = {scope for scope, items in _point_scopes(original) if _scope_blocks(items)[0][1].kind == JOB}
        bullets = {n - 1 for scope in roles for n in _bullet_numbers(edited.get(scope, []))}
        base_bullets = {n - 1 for scope in roles for n in _bullet_numbers(original[scope])}
        added = set()  # points a role gained that none of its own originals became
        for scope in roles:
            role_old = [n - 1 for n in _bullet_numbers(original[scope])]
            role_new = [n - 1 for n in _bullet_numbers(edited.get(scope, []))]
            added |= set(role_new) - set(_pair_rewrites(base_lines, role_old, lines, role_new))
        for scope, items in _point_scopes(original):
            old = [number - 1 for number in _bullet_numbers(items)]
            new = [number - 1 for number in _bullet_numbers(edited.get(scope, []))]
            if not new or len(new) >= len(old):
                continue
            pairs = _pair_rewrites(base_lines, old, lines, new)
            merged = _merged_points(base_lines, old, lines, new)
            lost = (Counter(_quantities("\n".join(base_lines[k] for k in old)))
                    - Counter(_quantities("\n".join(lines[j] for j in new))))

            def moved(i):
                # Copied or reworded into another role. A boilerplate point that was
                # already in another role before is not a move.
                if scope not in roles:
                    return False
                words = _content_words(base_lines[i])
                return (sum(_similarity(base_lines[i], lines[j]) >= 0.8 for j in bullets - set(new))
                        > sum(_similarity(base_lines[i], base_lines[k]) >= 0.8 for k in base_bullets - set(old))
                        or (len(words) >= 3 and any(len(words & _content_words(lines[j])) >= 0.6 * len(words)
                                                    for j in added - set(new))))

            def numbers_live_on(i):
                quantities = Counter(_quantities(base_lines[i]))
                return bool(quantities) and not quantities & lost

            notes += [f"The model merged an original point into another one in {scope}: “{base_lines[i].strip()[2:][:70]}…”"
                      for i in sorted(merged)]
            candidates = [i for i in old if i not in pairs.values() and i not in merged
                          and not numbers_live_on(i) and not moved(i)]
            candidates.sort(key=lambda i: max(_similarity(base_lines[i], lines[j]) for j in new))
            anchors = _role_anchors(items, edited.get(scope, []), pairs)
            for i in sorted(candidates[:len(old) - len(new) - len(merged)]):
                anchor = anchors(i)
                if anchor is None:
                    continue
                inserts.setdefault(anchor, []).append((i, base_lines[i]))
                notes.append(f"Restored an original point the model dropped in {scope}: “{base_lines[i].strip()[2:][:70]}…”")
    if not notes:
        return draft, []
    out = [line for j, text in enumerate(lines) for line in (text, *(extra for _, extra in sorted(inserts.get(j, []))))]
    return "\n".join(out) + ("\n" if draft.endswith("\n") else ""), notes


# A sub-heading that names a client or project, unlike a generic "Responsibilities:".
_NAMED_HEAD_RE = re.compile(r"(?i:\b(?:client|project|customer|engagement|account|program)\b)|:\s*\**[A-Z]")


def _role_anchors(base_items, draft_items, pairs):
    """For an original point, the draft line to insert it after: the nearest earlier
    line of its role that survived. Non-bullet lines (title, "Client: ..."
    sub-headings) are aligned in order; the walk stops at a named one the draft
    lost, so a point never crosses into another client's points."""
    base_blocks, draft_blocks = _scope_blocks(base_items), _scope_blocks(draft_items)
    base_heads = [(n - 1, _fact_text(b.text)) for n, b in base_blocks if b.kind != BULLET]
    named = {n - 1 for n, b in base_blocks if b.kind != BULLET and _NAMED_HEAD_RE.search(b.text)}
    draft_heads = [(n - 1, _fact_text(b.text)) for n, b in draft_blocks if b.kind != BULLET]
    heads = {}
    matcher = difflib.SequenceMatcher(None, [t for _, t in base_heads], [t for _, t in draft_heads], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            heads.update((base_heads[i][0], draft_heads[j][0]) for i, j in zip(range(i1, i2), range(j1, j2)))
    survivors = {k: j for j, k in pairs.items()}
    lost_heads = named - set(heads)

    def anchor(i):
        for k in range(i - 1, base_items[0][0] - 2, -1):
            if k in survivors or k in heads:
                return survivors.get(k, heads.get(k))
            if k in lost_heads:
                return None
        return None

    return anchor


_STOPWORDS = frozenset("and the for with from into using across that this their over via".split())


def _content_words(text: str) -> set[str]:
    return {re.sub(r"(?:ing|ed|es|s)$", "", word) if len(word) > 4 else word
            for word in re.findall(r"[a-z0-9+#]{3,}", text.lower()) if word not in _STOPWORDS}


def _merged_points(base_lines, old, lines, new) -> set[int]:
    """Unpaired original points that live on inside a kept point of the role: most
    of their words, or, in a rewrite that combined them with a sibling point, half
    their words and nearly all their names ("SQL Server Agent", "ETL")."""
    pairs = _pair_rewrites(base_lines, old, lines, new)
    merged = set()
    for i in old:
        if i in pairs.values():
            continue
        words, names = _content_words(base_lines[i]), _point_names(base_lines[i])
        for j in new:
            shared = len(words & _content_words(lines[j]))
            tokens = set(re.findall(r"[a-z0-9+#]+", lines[j].lower()))
            if len(words) >= 3 and (shared >= 0.6 * len(words) or (
                    j in pairs and shared >= 0.5 * len(words) and names and len(names & tokens) >= 0.75 * len(names))):
                merged.add(i)
                break
    return merged


def _point_names(line: str) -> set[str]:
    """Capitalised words and acronyms after a point's first word: its tools and names."""
    return {word.lower() for word in re.findall(r"\b[A-Z][A-Za-z0-9+#]*", line.strip().lstrip("-*• ").partition(" ")[2])}


# Category words in "Cloud Platforms: ..." labels. Tool names used as labels
# (AWS, SQL Server) are skills too.
_SKILL_CATEGORY_WORDS = frozenset("""programming scripting processing languages tools technologies
    technology technical skills services platforms cloud data databases analytics
    engineering architecture quality operations devops development frameworks
    libraries competencies expertise governance security storage warehousing
    orchestration streaming messaging ingestion integration retrieval evaluation
    monitoring observability automation deployment infrastructure ai ml agentic
    visualization reporting testing big soft other web core additional key general
    ecosystem ecosystems methodologies methodology practices packages formats file
    workflow workflows version source control concepts areas domains environments
    utilities operating systems servers application applications misc miscellaneous
    and & /""".split())
_FILLER = frozenset({"etc", "others", "more", "and more"})
_SKILL_HEADING_RE = re.compile(
    r"\b(?:skills?|competenc\w*|expertise|proficienc\w*)\b|\b(?:technical|technology|tech) stack\b", re.I)


def _scope_blocks(items):
    """A scope's blocks, each with its 1-based line number."""
    return list(zip((number for number, _ in items), from_markdown("\n".join(line for _, line in items))))


def _point_scopes(scopes):
    """Job roles and summary sections (not skills summaries): the scopes whose points must survive."""
    for scope, items in scopes.items():
        blocks = _scope_blocks(items)
        heading = blocks[0][1] if blocks else None
        if heading and (heading.kind == JOB or (
            heading.kind == SECTION and re.search(r"\b(?:summary|profile|objective)\b", heading.text, re.I)
            and not _SKILL_HEADING_RE.search(heading.text)
        )):
            yield scope, items


def _bullet_numbers(items) -> list[int]:
    return [number for number, block in _scope_blocks(items) if block.kind == BULLET]


def _skill_lines(scopes) -> list[tuple[str, int, str]]:
    """(section, line number, text) of every line under a skills heading."""
    found = []
    for scope, items in scopes.items():
        blocks = _scope_blocks(items)
        if blocks and blocks[0][1].kind == SECTION and _SKILL_HEADING_RE.search(blocks[0][1].text):
            found += [(scope, number, block.text) for number, block in blocks[1:]]
    return found


def _skill_items(line: str) -> list[str]:
    """Tool and skill names on one skills line, not descriptive phrases.

    Parentheses split AWS (EC2, S3) without losing AWS; a version (v9–v10) or an
    abbreviation (Secrets Manager (SM)) in them only qualifies the item before.
    A multi-word phrase with no capital, digit, or symbol after its first letter
    ("batch and real-time pipelines"), or any phrase over four words, describes
    work; the model may reword it. What remains of a label is a name only when
    it has no connector ("Deep Learning & Generative" is a category).
    """
    label, separator, values = line.partition(":")
    dash = None if separator else re.match(r"([^,()]{2,40}?)\s[–—-]\s(.+)", line)  # "Languages – Python, SQL"
    if dash:
        label, separator, values = dash.group(1), ":", dash.group(2)
    if separator:
        words = label.split()

        def category(word):  # "Cloud/DevOps" is a category; "CI/CD" is a skill
            return all(part.strip(",;").casefold() in _SKILL_CATEGORY_WORDS for part in word.split("/"))

        while words and category(words[0]):
            words.pop(0)
        while words and category(words[-1]):
            words.pop()
        head = " ".join(words).strip(",; ")
        values = ", ".join(["" if re.search(r"[&,]|\b(?:and|or)\b", head, re.I) else head, values])
    else:
        values = label
    items, previous = [], ""
    for part in re.split(r"[,;()\n]|\s+[|/•]\s+", values):
        item = re.sub(r"^(?:and|or|&)\s+", "", part.strip(), flags=re.I).strip(" .")
        if not item or item.casefold() in _FILLER:
            continue
        qualifier = re.fullmatch(r"v?\d[\w.–-]*", item, re.I) or (
            len(item) > 1 and item == "".join(word[0] for word in previous.split()).upper())
        previous = item
        if not qualifier and len(item.split()) <= 4 and (" " not in item or re.search(r"[A-Z0-9+#/.]", item[1:])):
            items.append(item)
    return items


def _skill_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_TYPOGRAPHY)).casefold()


def _has_skill(key: str, text_key: str):
    return re.search(rf"(?<![\w+#]){re.escape(key)}(?![\w+#])", text_key)


def _missing_skills(original, edited) -> list[tuple[str, tuple[str, int, str]]]:
    """Original skill names absent from the edited skills lines, with their source line.

    A name survives in parts ("SQL and Scala" as "SQL, Scala") or without a vendor
    prefix ("AWS Lake Formation" as "Lake Formation"). A name taken from a category
    label (ETL in "ETL and Data Architecture") survives anywhere in the resume.
    ponytail: explicit skill lists, not semantic aliases; original names are kept.
    """
    available = _skill_key("\n".join(text for _, _, text in _skill_lines(edited)))
    everywhere = _skill_key("\n".join(line for items in edited.values() for _, line in items))
    missing: dict[str, tuple[str, tuple[str, int, str]]] = {}
    seen: set[str] = set()
    for source in _skill_lines(original):
        label = source[2].partition(":")[0] + ":" if ":" in source[2] else ""
        from_label = {_skill_key(name) for name in _skill_items(label)} if label else set()
        for item in _skill_items(source[2]):
            key = _skill_key(item)
            if key in seen:
                continue
            seen.add(key)
            parts = [part for part in re.split(r"\s*/\s*|\s*&\s*|\s+(?:and|or)\s+", key) if part]
            bare = re.sub(r"^(?:aws|amazon|azure|microsoft|google|gcp|apache)\s+", "", key)
            text = everywhere if key in from_label else available
            if not (_has_skill(key, text) or all(_has_skill(part, text) for part in parts)
                    or (bare != key and _has_skill(bare, text))):
                missing[key] = (item, source)
    return list(missing.values())


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
    number = r"(?<![\w.])\d(?:[\d,]*\d)?(?:\.\d+)?"
    units = (
        r"ms|seconds?|minutes?|hours?|days?|weeks?|months?|years?|percent|x|"
        r"[kmb]|thousand|million|billion|users?|customers?|clients?|records?|"
        r"models?|engineers?|persons?|people|teams?|documents?|types?|requests?|events?|"
        r"services?|pipelines?|endpoints?|jobs?|tests?|deployments?|transactions?|"
        r"calls?|applications?|projects?|servers?|clusters?|regions?|databases?|"
        r"terabytes?|gigabytes?|petabytes?|TB|GB|PB|"
        r"tables?|workflows?|dags?|systems?|reports?|dashboards?|files?|datasets?|feeds?|"
        r"schemas?|tickets?|defects?|microservices?|integrations?|apis?|queries|query|rows?|nodes?|"
        r"stakeholders?|members?|vendors?|partners?|products?|features?|releases?|stores?|countries|markets?"
    )
    separator = r"[ \t]*(?:[-‐‑‒–—][ \t]*)?"
    # "40 legacy Informatica workflows" is a count of workflows: up to two words
    # may sit between a number and its unit, and only the two form the claim.
    pattern = (rf"[$€£]\s*{number}\s*(?:[kmb]\b|thousand\b|million\b|billion\b)?"
               rf"|(?P<n>{number})[ \t]*\+?{separator}(?:(?P<pct>%)|(?:[A-Za-z][\w/-]*[ \t]+){{0,2}}?(?P<unit>{units})\b)"
               rf"|\b\d{{1,3}}(?:,\d{{3}})+\+?")
    quantities = []
    for match in re.finditer(pattern, text, re.I):
        claim = match["n"] + (match["pct"] or match["unit"]) if match["n"] else match.group()
        value = re.sub(r"[\s+\-‐‑‒–—]", "", claim).lower()
        # "2-hour" and "2 hours" express the same quantity; keep ms intact.
        value = re.sub(r"([a-z]{2,})s$", r"\1", value)
        quantities.append(re.sub(r"people$", "person", value))
    return quantities


def _repair_lines(base: str, out: str) -> tuple[str, list[str]]:
    """Undo changed numbers, dates, client/project names, new qualifications, and tools newer than a role.

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

    # Dates belong to their own role; client/project names to the resume.
    role_spans = {scope: {_canon_span(m.group()) for _, line in items for m in DATE_SPAN_RE.finditer(line)}
                  for scope, items in original.items()}
    # Job headings are left alone: dropping an invented one would strand its
    # title and points in another role, so it must stay a hard failure.
    job_lines = {j for j, block in zip((j for j, line in enumerate(out_lines) if line.strip()), from_markdown(out))
                 if block.kind == JOB}
    names = _named_history(base)
    availability = {claim.lower() for claim in _AVAILABILITY_RE.findall(base)}
    lost_names = names - _named_history(out)
    lost_spans = {span for spans in role_spans.values() for span in spans} - {
        _canon_span(m.group()) for m in DATE_SPAN_RE.finditer(out)}

    def new_claim(j):
        # Only an enclosing H2 section freezes qualifications. A job's employer
        # may contain words such as Education or Licensing without being one.
        line = out_lines[j]
        spans = {_canon_span(m.group()) for m in DATE_SPAN_RE.finditer(line)}
        return (_has_unknown_credential(line, known)
                or (j not in qualification_lines and bool(_degree_levels(line) - degrees))
                or bool(spans - role_spans.get(out_scope.get(j + 1), set()))
                or bool(_named_history(line) - names)
                or (out_scope.get(j + 1) != "Contact and headline"
                    and bool({claim.lower() for claim in _AVAILABILITY_RE.findall(line)} - availability)))

    def lost_fact(i):
        """Original line i names a client/project or date range the edit lost."""
        return bool(_named_history(base_lines[i]) & lost_names or
                    {_canon_span(m.group()) for m in DATE_SPAN_RE.finditer(base_lines[i])} & lost_spans)

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
        if numbers_moved or claims or lost_names or lost_spans:
            # An identical bullet moved to a different role is still an edit.
            # Pair only within that role, including the single-line fallback.
            for scope in dict.fromkeys(out_scope.get(j + 1) for j in range(j1, j2)):
                old = [i for i in range(i1, i2) if base_scope.get(i + 1) == scope]
                new = [j for j in range(j1, j2) if out_scope.get(j + 1) == scope]
                found = _pair_rewrites(base_lines, old, out_lines, new)
                # An original that must come back (its number or fact was lost) takes the
                # place of the unpaired rewrite sharing most words with it, instead of
                # returning beside that rewrite and repeating the point.
                free = [j for j in new if j not in found]
                for i in old:
                    if i in found.values() or not (dropped(i, None) or lost_fact(i)):
                        continue
                    words = _content_words(base_lines[i])
                    best = max(free, default=None, key=lambda j: (
                        len(words & _content_words(out_lines[j])), _similarity(base_lines[i], out_lines[j])))
                    if best is not None and words & _content_words(out_lines[best]):
                        found[best] = i
                        free.remove(best)
                pairs.update(found)
        for j in range(j1, j2):
            i = pairs.get(j)
            if j in job_lines:  # restore_frozen and the job checks own headings
                repaired.append(out_lines[j])
                continue
            # A lost name or date returns only through its own rewrite, never
            # beside an unpaired one, which would repeat the point.
            if (gained(j, i) or (i is not None and (dropped(i, j) or lost_fact(i)))
                    or new_claim(j) or too_new(j)):
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


def _similarity(a: str, b: str) -> float:
    # autojunk would ignore common characters in lines over 200 characters.
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _pair_rewrites(base_lines, base_range, out_lines, out_range) -> dict[int, int]:
    """Pair each edited line with the original it most resembles.

    Shared content words rank first, then character similarity; lines that
    share no content word pair only as the last line left on both sides.
    """
    words = {i: _content_words(base_lines[i]) for i in base_range}
    scored = sorted(
        ((len(words[i] & _content_words(out_lines[j])), _similarity(base_lines[i], out_lines[j]), i, j)
         for i in base_range for j in out_range),
        reverse=True,
    )
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for shared, ratio, i, j in scored:
        if shared and (ratio >= 0.35 or shared >= 3) and j not in pairs and i not in used:
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
    usually a headline/tagline, which the model may reword (see ``_restore_headline``).
    """
    last = -1
    for index, line in enumerate(lines):
        if CONTACT_MARKER_RE.search(line):
            last = index
    return last


# A job-title headline between the name and the contact line ("Sr. Data Engineer")
# may be reworded like one below it; a location line there may not.
_HEADLINE_RE = re.compile(
    r"\b(?:engineer|developer|architect|analyst|scientist|consultant|manager|administrator|"
    r"specialist|designer|programmer|tester|lead|director)s?\b", re.I)


# Header lines the model may not add: a location or a work authorization.
_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
           "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_PERSONAL_RE = re.compile(rf",\s*(?:{'|'.join(_STATES)})(?=\s*(?:$|[|·•(),]|\d{{5}}))"
                          r"|(?i:\b(?:citizen|green card|h-?1b|ead|opt|cpt|visa|authori[sz]\w*"
                          r"|relocat\w*|remote|hybrid|on-?site|c2c|w-?2|1099)\b)")


# Availability claims a body line may not add: a place ("Newark, NJ") or a work
# authorization. "Hybrid cloud" and "remote teams" are ordinary tailoring.
_AVAILABILITY_RE = re.compile(rf",\s*(?:{'|'.join(_STATES)})(?=\s*(?:$|[|·•(),.;]|\d{{5}}))"
                              r"|(?i:\b(?:us citizen|citizenship|green card|h-?1b|visa|sponsorship|work authori[sz]ation"
                              r"|authori[sz]ed to work|open to relocation|relocat(?:e|ion)|c2c|w-?2|1099)\b)")


def _bare_headline(line: str) -> bool:
    """A job-title line with nothing else on it ("Sr. Data Engineer")."""
    return bool(line.strip() and _HEADLINE_RE.search(line) and not CONTACT_MARKER_RE.search(line)
                and not re.search(r"[|·•,]", line))


def _identity(lines: list[str]) -> tuple[list[str | None], list[str]]:
    """Nonblank header lines through the last contact detail, with each bare
    job-title headline (never the name line) as a None slot whose wording may
    change. A line that also holds contact details, a location, or other
    segments ("Engineer | Dallas, TX") stays frozen."""
    kept = [line for line in lines[:_last_identity_line(lines) + 1] if line.strip()]
    return [None if index and _bare_headline(line) else line for index, line in enumerate(kept)], kept


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
        (original, base_kept), (current, out_kept) = _identity(base_lines), _identity(out_lines)
        # Below the contact line the model may reword taglines, but not add a location
        # or work authorization.
        base_tail = [line for line in base_lines[boundary + 1:] if line.strip()]
        out_tail = [line for line in out_lines[out_boundary + 1:] if line.strip()]
        known = set(base_kept) | set(base_tail)

        def invented(line):  # a location or work authorization the original header lacks
            return line not in known and bool(_PERSONAL_RE.search(line))

        slots = [line for line, slot in zip(out_kept, current) if slot is None]
        tail = [line for line in out_tail if not invented(line)]
        rejected = len(tail) < len(out_tail) or any(invented(line) for line in slots)
        if rejected:  # what a rejected line displaced comes back
            tail += [line for line in base_tail if line not in tail]
        if original == current and not rejected:
            return out, False
        # Name and contact lines come back; the model's headline wording is kept,
        # wherever it put the headline relative to the contact line.
        pool = [line for line in slots if not invented(line)] + [line for line in tail if _bare_headline(line)]
        merged = [slot if slot is not None else (pool.pop(0) if pool else line)
                  for line, slot in zip(base_kept, original)]
        merged += pool + [line for line in tail if not _bare_headline(line)]
        if merged == [line for line in out_lines if line.strip()]:
            return out, False
        trailing = out_header[len(out_header.rstrip("\n")) :]
        return "\n".join(merged) + trailing + out[len(out_header) :], True

    # If the contact block was dropped entirely, restore it without touching body.
    return base_header + out[len(out_header) :], True


def _words(text: str) -> set[str]:
    """Lowercase alphanumeric words, ignoring emphasis, punctuation and connectors."""
    return set(re.findall(r"[^\W_]+", text.lower())) - {"and", "of", "for", "the"}


def _is_headline(line: str) -> bool:
    """A header line that reads as a job title.

    Not the name, contact details, a location or work authorization, and not a bullet,
    a sentence, or a line without a job-title word (a summary, a skills label or strip):
    those sit above the first section heading in some layouts and are tailored as usual.
    """
    text = line.replace("**", "").strip()
    return bool(_HEADLINE_RE.search(text) and len(text.split()) <= 20 and not text.startswith(("#", "-"))
                and not text.endswith(".") and not CONTACT_MARKER_RE.search(text) and not _PERSONAL_RE.search(text))


def _headline_text(header: str) -> str:
    return " | ".join(line.replace("**", "").strip() for line in header.splitlines() if _is_headline(line))


def _restore_headline(base: str, out: str) -> tuple[str, bool]:
    """Bring the original headline lines back if the headline was retargeted to the job.

    The headline may be narrowed or reordered with words the original headline already
    has ("Sr. AI/ML Engineer / Data Scientist" -> "ML Engineer"). A new word, whether
    a specialty, a seniority or a phrase from the job description, is a different
    title, not a rewording. Only headline lines are compared and replaced, so words in
    the name or contact lines license nothing and a rewritten summary or skills line
    in the same header stays.
    """
    base_header = _contact_block(base)
    out_header = _contact_block(out)
    if _words(_headline_text(out_header)) <= _words(_headline_text(base_header)):
        return out, False
    spare = iter([line for line in base_header.splitlines() if _is_headline(line)])
    lines: list[str] = []
    last = -1
    for line in out_header.rstrip("\n").splitlines():
        if _is_headline(line):
            line = next(spare, None)  # the original's lines, in order; surplus headline lines go
            if line is None:
                continue
            last = len(lines)
        lines.append(line)
    lines[last + 1 : last + 1] = spare  # any the model dropped, after the last one it kept
    trailing = out_header[len(out_header.rstrip("\n")) :]
    return "\n".join(lines) + trailing + out[len(out_header) :], True


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
