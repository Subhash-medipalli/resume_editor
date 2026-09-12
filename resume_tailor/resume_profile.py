"""Profile extraction from a markdown resume for deterministic matching checks."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_STOP_WORDS = {
    "and", "or", "the", "a", "an", "to", "for", "with", "of", "on", "in", "at", "as", "by",
    "from", "is", "are", "was", "were", "be", "have", "has", "this", "that", "these", "those",
    "we", "you", "our", "their", "their", "i", "it", "its", "role", "roles", "work", "working",
    "experience", "using", "used", "build", "built", "support", "platform", "team", "teams", "project",
    "projects", "service", "services", "system", "systems", "data", "cloud", "python",
}


def _tokenize_technologies(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+#/-]{2,}", text):
        norm = token.strip().strip(".,:;()")
        low = norm.lower()
        if low not in _STOP_WORDS and len(norm) > 1:
            tokens.append(norm)
    return tokens


def _is_reusable_term(value: str) -> bool:
    if not value:
        return False
    value = value.strip().lower()
    if value in _STOP_WORDS:
        return False
    return not value.isnumeric() and len(value) > 2


def _normalize_section_name(section: str) -> str:
    return section.strip().strip(":").strip()


@dataclass(frozen=True)
class ResumeProfile:
    """Capabilities and structure summary used for coverage checks and constraints."""

    raw: str
    section_headings: tuple[str, ...]
    titles: tuple[str, ...]
    companies: tuple[str, ...]
    technologies: tuple[str, ...]
    skills: tuple[str, ...]
    business_terms: tuple[str, ...]
    section_counts: tuple[tuple[str, int], ...]


def extract_resume_profile(markdown: str) -> ResumeProfile:
    lines = (markdown or "").splitlines()

    section_headings: list[str] = []
    titles: list[str] = []
    companies: list[str] = []
    technologies: list[str] = []
    skills: list[str] = []
    business: list[str] = []
    section_counts: Counter[str] = Counter()

    section = "Contact"
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith("###"):
            heading = stripped[3:].strip()
            if heading:
                # Keep the employer and dates; used to infer if business-language
                # has appeared in the experience section.
                companies.append(heading)
            continue

        if stripped.startswith("##"):
            section = _normalize_section_name(stripped[2:])
            if section:
                section_headings.append(section)
            continue

        if stripped.startswith("**") and stripped.endswith("**"):
            title = stripped.strip("*").strip()
            if title:
                titles.append(title)
            continue

        if stripped.startswith("- "):
            text = stripped[2:]
            technologies.extend(_tokenize_technologies(text))
            section_counts[section] += 1
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9']+", text.lower()) if _is_reusable_term(w)]
            business.extend(words)
            if _normalize_section_name(section).lower() == "skills":
                skills.extend(_tokenize_technologies(text))
            continue

        if " | " in stripped and any(ch.isdigit() for ch in stripped):
            technologies.extend(_tokenize_technologies(stripped))

        # Non-bullet summary and responsibility text also carry matchable terms.
        if section:
            section_counts[section] += 1
        words = re.findall(r"[a-zA-Z][a-zA-Z0-9']+", stripped.lower())
        business.extend([w for w in words if _is_reusable_term(w)])

    section_headings = _dedupe(section_headings)
    titles = _dedupe(titles)
    companies = _dedupe(companies)
    technologies = _dedupe(technologies)
    skills = _dedupe(skills)
    business = _dedupe(business)

    return ResumeProfile(
        raw=(markdown or "").strip(),
        section_headings=tuple(section_headings),
        titles=tuple(titles),
        companies=tuple(companies),
        technologies=tuple(technologies),
        skills=tuple(skills),
        business_terms=tuple(business),
        section_counts=tuple((name, count) for name, count in sorted(section_counts.items())),
    )


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))
