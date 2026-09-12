"""Reusable role strategy and budget definitions for multi-pass tailoring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from resume_tailor.jd_profile import JDProfile


@dataclass(frozen=True)
class RoleProfile:
    key: str
    title_aliases: tuple[str, ...]
    required_terms: tuple[str, ...]
    positioning_focus: tuple[str, ...]
    section_budget: tuple[tuple[str, int], ...]
    max_sections_touched: int
    factual_instructions: tuple[str, ...]
    positioning_instructions: tuple[str, ...]

    @property
    def section_budget_map(self) -> dict[str, int]:
        return dict(self.section_budget)


_DEFAULT_SECTION_BUDGET = (
    ("Summary", 2),
    ("Skills", 2),
    ("Experience", 4),
    ("Professional Summary", 2),
    ("Technical Skills", 2),
)


def _role_profile(
    *,
    key: str,
    title_aliases: tuple[str, ...],
    required_terms: tuple[str, ...],
    positioning_focus: tuple[str, ...],
    section_budget: tuple[tuple[str, int], ...] = _DEFAULT_SECTION_BUDGET,
    max_sections_touched: int = 4,
    factual_instructions: tuple[str, ...] | None = None,
    positioning_instructions: tuple[str, ...] | None = None,
) -> RoleProfile:
    return RoleProfile(
        key=key,
        title_aliases=title_aliases,
        required_terms=required_terms,
        positioning_focus=positioning_focus,
        section_budget=section_budget,
        max_sections_touched=max_sections_touched,
        factual_instructions=factual_instructions
        or ("Keep every wording change evidence-first.", "Do not add any new employers, dates, metrics, tools, or certifications."),
        positioning_instructions=positioning_instructions
        or ("Emphasize the candidate’s existing evidence for the role responsibilities.", "Do not invent scale, outcomes, or tools."),
    )


_DATA_SCIENCE = _role_profile(
    key="data_science",
    title_aliases=("data scientist", "machine learning scientist", "ml scientist", "ml engineer"),
    required_terms=("random forest", "gradient boosting", "xgboost", "svm", "deep learning", "clustering", "pySpark", "spark", "sql"),
    positioning_focus=(
        "high-impact business problems",
        "model evaluation",
        "data quality",
        "monitoring across lifecycle",
    ),
    section_budget=(
        ("Summary", 3),
        ("Skills", 2),
        ("Technical Skills", 2),
        ("Professional Summary", 3),
        ("Experience", 5),
    ),
    max_sections_touched=4,
    factual_instructions=(
        "Focus on evidence-backed ML/analysis claims already present in the base resume.",
        "Prefer summary and experience bullets; avoid adding new skills lines unless all terms are already present.",
    ),
    positioning_instructions=(
        "Translate each edit into business-facing language around product teams, process improvement, and measurable impact.",
        "Surface 1–2 role-relevant outcomes in the summary and most relevant bullets.",
    ),
)

_ENGINEERING = _role_profile(
    key="general_engineering",
    title_aliases=("software engineer", "backend engineer", "full stack", "platform engineer", "cloud engineer"),
    required_terms=("python", "sql", "aws", "api", "terraform", "cloud", "ci/cd", "docker"),
    positioning_focus=("delivery", "scoped engagement", "contract", "operational reliability"),
    section_budget=(
        ("Summary", 2),
        ("Skills", 2),
        ("Experience", 4),
        ("Professional Summary", 2),
        ("Technical Skills", 2),
    ),
)

_DEFAULT_ROLE = _role_profile(
    key="default",
    title_aliases=("contractor", "consultant", "professional"),
    required_terms=("python", "aws", "sql", "api"),
    positioning_focus=("scope", "evidence-first", "delivery"),
    section_budget=_DEFAULT_SECTION_BUDGET,
    max_sections_touched=5,
)


ROLE_PROFILES = {
    profile.key: profile
    for profile in (_DATA_SCIENCE, _ENGINEERING, _DEFAULT_ROLE)
}


def select_role_profile(profile: JDProfile | None) -> RoleProfile:
    if profile is None:
        return _DEFAULT_ROLE
    hints = {item.lower() for item in (profile.title_hints + profile.terms + profile.keywords)}
    if {"data scientist", "machine learning scientist", "machine learning", "ml scientist", "ml"}.intersection(hints):
        return _DATA_SCIENCE
    if {"software engineer", "backend engineer", "platform engineer", "cloud engineer"}.intersection(hints):
        return _ENGINEERING
    return _DEFAULT_ROLE


def profile_from_key(key: str) -> RoleProfile:
    return ROLE_PROFILES.get(key, _DEFAULT_ROLE)


def format_section_budget(profile: RoleProfile) -> str:
    return "\n".join(f"- {name}: {limit} line(s)" for name, limit in profile.section_budget_map.items())


def format_role_prompt_instructions(profile: RoleProfile, pass_type: str = "positioning") -> str:
    if pass_type == "factual":
        return "\n".join(f"- {line}" for line in profile.factual_instructions)
    return "\n".join(f"- {line}" for line in profile.positioning_instructions)


def role_template(profile: RoleProfile) -> Mapping[str, tuple[str, ...]]:
    return {
        "required_terms": profile.required_terms,
        "focus": profile.positioning_focus,
        "section_budget": profile.section_budget,
    }
