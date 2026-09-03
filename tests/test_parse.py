from resume_tailor.llm import LLMError, parse_model_output
from resume_tailor.prompt import SYSTEM_PROMPT
from tests.helpers import pack_model_output


def test_parse_model_output_extracts_sections():
    raw = pack_model_output(
        changelog=["Tweaked summary", "Aligned one bullet"],
        match="good: overlap on Python/AWS",
        resume="# Hi\n\nHello\n",
    )
    result = parse_model_output(raw)
    assert result.changelog == ["Tweaked summary", "Aligned one bullet"]
    assert result.match_line.startswith("good:")
    assert result.resume_markdown.startswith("# Hi")
    assert result.resume_markdown.endswith("\n")


def test_parse_strips_wrapping_code_fence():
    inner = pack_model_output(
        changelog=["One change"],
        match="partial: light keywords only",
        resume="body",
    )
    result = parse_model_output(f"```markdown\n{inner}\n```")
    assert result.changelog == ["One change"]
    assert "body" in result.resume_markdown


def test_parse_rejects_missing_resume_marker():
    try:
        parse_model_output("===CHANGELOG===\n- x\n===MATCH===\nok\n")
    except LLMError as exc:
        assert "RESUME" in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_system_prompt_encodes_hard_constraints():
    text = SYSTEM_PROMPT.lower()
    for phrase in (
        "surgical",
        "never invent employers",
        "dates",
        "titles",
        "education",
        "certifications",
        "metrics",
        "never add jobs",
        "poor match",
        "do not overhaul",
        "changelog",
    ):
        assert phrase in text, f"system prompt missing {phrase!r}"
