"""Job-description parsing for strategy-first tailoring."""

from __future__ import annotations

from dataclasses import dataclass
import re


_STOP_WORDS = {
    "the", "and", "or", "a", "an", "to", "in", "for", "of", "on", "at", "with",
    "without", "as", "by", "from", "is", "are", "be", "that", "this", "these",
    "those", "it", "we", "you", "your", "their", "their", "there", "here", "our",
    "you", "i", "me", "my", "will", "must", "have", "has", "have", "who",
    "who's", "would", "should", "can", "may", "should", "need", "needs", "required",
}

_TITLE_HINT_RE = re.compile(
    r"\b(?:title|role|position|looking for|we\s+are\s+looking\s+for|position\s*:)\s*[:\-–—]?\s*(.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class JDProfile:
    """Parsed signals from a job description for repeatable tailoring behavior."""

    raw: str
    title_hints: tuple[str, ...]
    must_haves: tuple[str, ...]
    preferred: tuple[str, ...]
    responsibilities: tuple[str, ...]
    constraints: tuple[str, ...]
    keywords: tuple[str, ...]
    terms: tuple[str, ...]
    location_hints: tuple[str, ...]


def parse_jd_profile(text: str) -> JDProfile:
    text = (text or "").strip()
    if not text:
        return JDProfile(
            raw="", title_hints=(), must_haves=(), preferred=(), responsibilities=(),
            constraints=(), keywords=(), terms=(), location_hints=(),
        )

    title_hints: list[str] = []
    must_haves: list[str] = []
    preferred: list[str] = []
    responsibilities: list[str] = []
    constraints: list[str] = []
    location_hints: list[str] = []

    section = "body"
    raw_lines = text.splitlines()

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue

        lowered = line.lower()
        if lowered.startswith(("must-have", "must haves", "must have", "requirements", "required")):
            section = "must"
            continue
        if lowered.startswith(("nice-to-have", "nice to have", "preferred", "plus", "bonus")):
            section = "preferred"
            continue
        if lowered.startswith(("responsibilities", "what you will do", "you will", "your responsibilities", "role")):
            section = "responsibilities"
            continue
        if lowered.startswith(("constraints", "must be", "must-have but", "location", "work location")):
            section = "constraints"
            continue

        title_match = _TITLE_HINT_RE.search(line)
        if title_match:
            value = title_match.group(1).strip(": –—-.")
            if value:
                title_hints.append(_clean_fragment(value))

        bullet = _as_bullet_text(line)
        if section == "must":
            _append_if_present(must_haves, bullet or line)
            continue
        if section == "preferred":
            _append_if_present(preferred, bullet or line)
            continue
        if section == "responsibilities":
            _append_if_present(responsibilities, bullet or line)
            continue
        if section == "constraints":
            _append_if_present(constraints, bullet or line)
            continue

        _append_if_present(constraints, bullet or line, required=False)

    tokens = []
    for item in (*must_haves, *preferred, *responsibilities, *constraints):
        tokens.extend(_extract_terms(item))

    terms = _extract_terms(" ".join([t.lower() for t in title_hints]))
    location_hints = _extract_location_hints(text)
    keywords = sorted(set(tokens + terms))
    return JDProfile(
        raw=text,
        title_hints=tuple(dict.fromkeys(_compact(title_hints))),
        must_haves=tuple(dict.fromkeys(_compact(must_haves))),
        preferred=tuple(dict.fromkeys(_compact(preferred))),
        responsibilities=tuple(dict.fromkeys(_compact(responsibilities))),
        constraints=tuple(dict.fromkeys(_compact(constraints))),
        keywords=tuple(keywords),
        terms=tuple(dict.fromkeys(_compact(terms))),
        location_hints=tuple(dict.fromkeys(_compact(location_hints))),
    )


def _append_if_present(target: list[str], value: str, *, required: bool = True) -> None:
    value = value.strip("-* \t:–—-.")
    if len(value) < 3:
        return
    if required and any(ch.isalnum() for ch in value):
        target.append(value)


def _as_bullet_text(line: str) -> str:
    return re.sub(r"^[\-*•]\s+", "", line).strip()


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    # Keep short technology-style terms and domain phrases.
    for part in re.split(r"[,;]", text):
        phrase = part.strip()
        if not phrase:
            continue
        norm = _clean_fragment(phrase)
        if 1 < len(norm.split()) <= 4 and not _is_stopword_blob(norm):
            terms.append(norm)
    for token in re.findall(r"[A-Za-z][A-Za-z0-9.+#/-]{2,}", text):
        if token.lower() not in _STOP_WORDS and token.isascii():
            terms.append(token.lower())
    return terms


def _compact(values: list[str]) -> list[str]:
    return [value.strip() for value in values if value and value.strip()]


def _clean_fragment(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip(". –—-\n\t\r")).strip()


def _is_stopword_blob(text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w]
    if not words:
        return True
    return all(w in _STOP_WORDS for w in words)


def _extract_location_hints(text: str) -> list[str]:
    candidates: list[str] = []
    lowered = text.lower()
    if "remote" in lowered:
        candidates.append("remote")
    if "in-office" in lowered or "onsite" in lowered or "office" in lowered:
        candidates.append("onsite")
    return candidates
