"""Structured view of the resume.

The parent .docx is the source of truth, but a flat text dump of it loses the
information every later stage depends on: the model cannot tell a bullet from a
paragraph, the guardrails cannot find sections or employers, and the Word writer
has nothing but fuzzy string similarity to go on.

This module parses the .docx into typed blocks, renders them as markdown for the
model, and parses the model's reply back into the same block shape. Every stage
downstream then works on structure instead of guessing at it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Block kinds, in the order they appear in a resume.
NAME = "name"
CONTACT = "contact"
TAGLINE = "tagline"
SECTION = "section"
JOB = "job"
TITLE = "title"
ENVIRONMENT = "environment"
BULLET = "bullet"
TEXT = "text"

MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_SPAN_RE = re.compile(
    rf"{MONTH}\s+\d{{4}}\s*[–—-]\s*(?:Present|{MONTH}\s+\d{{4}})",
    re.IGNORECASE,
)
# "PROFESSIONAL SUMMARY:" — an all-caps line, colon optional.
SECTION_RE = re.compile(r"^[A-Z][A-Z &/,'()-]{2,}:?$")
# All-caps alone is not enough: a name typed "JANE DOE" or an employer typed
# "GOLDMAN SACHS" would become a section, which silently disables the contact
# and employer guardrails. Require a recognisable section word too.
SECTION_WORDS = frozenset(
    """summary profile objective about skills skill competencies expertise
    experience employment history work professional career education academic
    qualifications certification certifications certificates licenses projects
    project publications patents awards honors achievements accomplishments
    technical technology tools languages interests volunteer training courses
    coursework references activities affiliations memberships highlights
    background overview credentials""".split()
)
CONTACT_MARKER_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d ()./-]{7,}\d"
)
ENVIRONMENT_RE = re.compile(r"^Environment\s*:", re.IGNORECASE)


@dataclass(frozen=True)
class Block:
    """One logical line of the resume, with where it came from in the .docx."""

    kind: str
    text: str
    para_index: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        """Identity used when aligning a tailored resume against the base.

        Trailing colons and whitespace are normalised away: markdown headings
        drop the colon a Word section heading carries, and that is not an edit.
        """
        normalised = re.sub(r"\s+", " ", self.text).strip().rstrip(":").lower()
        return (self.kind, normalised)


def _is_bullet_style(style: str) -> bool:
    """Word templates name bullet styles variously: List Paragraph, List Bullet…"""
    return "list" in (style or "").lower()


def _is_section(text: str, style: str) -> bool:
    if _is_bullet_style(style):
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > 60:
        return False
    if CONTACT_MARKER_RE.search(stripped):
        return False  # a contact line is never a section heading
    letters = [c for c in stripped if c.isalpha()]
    if not letters:
        return False
    if not (all(c.isupper() for c in letters) and SECTION_RE.match(stripped)):
        return False
    words = re.findall(r"[A-Za-z]+", stripped.lower())
    return any(word in SECTION_WORDS for word in words)


def _is_job_heading(text: str) -> bool:
    return "|" in text and bool(DATE_SPAN_RE.search(text))


def parse_paragraphs(entries: list[tuple[str, str]]) -> list[Block]:
    """Classify (text, style) pairs from the .docx into typed blocks.

    `entries` must already exclude blank paragraphs; index i in the result's
    ``para_index`` refers to position i in `entries`.
    """
    blocks: list[Block] = []
    seen_section = False
    previous_kind: str | None = None

    for index, (raw_text, style) in enumerate(entries):
        text = (raw_text or "").strip()
        if not text:
            continue

        if index == 0:
            # The first line is the candidate's name, even in capitals.
            kind = NAME
        elif _is_section(text, style):
            kind = SECTION
            seen_section = True
        elif _is_job_heading(text):
            kind = JOB
        elif ENVIRONMENT_RE.match(text):
            kind = ENVIRONMENT
        elif _is_bullet_style(style):
            kind = BULLET
        elif previous_kind == JOB:
            # The line directly under a job heading is the role held there.
            kind = TITLE
        elif not seen_section and CONTACT_MARKER_RE.search(text):
            kind = CONTACT
        elif not seen_section and previous_kind == NAME:
            kind = CONTACT
        elif not seen_section:
            kind = TAGLINE
        else:
            kind = TEXT

        blocks.append(Block(kind=kind, text=text, para_index=index))
        previous_kind = kind

    return blocks


def to_markdown(blocks: list[Block]) -> str:
    """Render blocks as the markdown the model is asked to edit."""
    lines: list[str] = []
    for block in blocks:
        text = block.text
        if block.kind == NAME:
            lines.append(f"# {text}")
        elif block.kind == SECTION:
            lines.append("")
            lines.append(f"## {text.rstrip(':')}")
            lines.append("")
        elif block.kind == JOB:
            lines.append("")
            lines.append(f"### {text}")
        elif block.kind in (TITLE, TAGLINE):
            lines.append(f"**{text}**")
        elif block.kind == BULLET:
            lines.append(f"- {text}")
        else:
            lines.append(text)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def from_markdown(markdown: str) -> list[Block]:
    """Parse a markdown resume (the model's reply) back into blocks."""
    blocks: list[Block] = []
    seen_section = False
    previous_kind: str | None = None

    for raw in markdown.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("### "):
            kind, text = JOB, line[4:].strip()
        elif line.startswith("## "):
            kind, text = SECTION, line[3:].strip()
            seen_section = True
        elif line.startswith("# "):
            kind, text = NAME, line[2:].strip()
        elif line.startswith(("- ", "* ")):
            kind, text = BULLET, line[2:].strip()
        else:
            bold = re.fullmatch(r"\*\*(.+?)\*\*", line)
            if bold:
                text = bold.group(1).strip()
                kind = TITLE if previous_kind == JOB else (
                    TAGLINE if not seen_section else TEXT
                )
            else:
                text = line
                if ENVIRONMENT_RE.match(text):
                    kind = ENVIRONMENT
                elif _is_job_heading(text):
                    kind = JOB
                elif not seen_section and previous_kind == NAME:
                    kind = CONTACT
                elif not seen_section and previous_kind is None:
                    kind = NAME
                else:
                    kind = TEXT

        # Strip stray markdown emphasis the model may add inside a line.
        text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
        blocks.append(Block(kind=kind, text=text))
        previous_kind = kind

    return blocks


def header_block_length(blocks: list[Block]) -> int:
    """How many leading blocks form the contact header (name/contact/tagline)."""
    count = 0
    for block in blocks:
        if block.kind in (NAME, CONTACT, TAGLINE):
            count += 1
        else:
            break
    return count
