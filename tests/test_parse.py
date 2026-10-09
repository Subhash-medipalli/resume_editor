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
        "add missing jd skills",
        "never invent employers",
        "dates",
        "historical job titles",
        "education",
        "certifications",
        "do not add new numbers",
        "write like the candidate",
        "changelog",
    ):
        assert phrase in text, f"system prompt missing {phrase!r}"


def test_parser_removes_reasoning_and_resume_only_fence():
    raw = pack_model_output(changelog=["Updated summary"], match="good: relevant",
                            resume="```markdown\n# Test\nPython developer\n```")
    result = parse_model_output("<think>Private drafting text</think>\n" + raw)
    assert result.resume_markdown == "# Test\nPython developer\n"


def test_system_prompt_spreads_platforms_across_the_timeline():
    from resume_tailor.prompt import SYSTEM_PROMPT
    text = SYSTEM_PROMPT.lower()
    assert "most recent role" in text and "comparable alternative" in text
    assert "ended before that tool was released" in text


def test_system_prompt_only_lets_the_headline_be_narrowed():
    text = " ".join(SYSTEM_PROMPT.lower().split())
    assert "rewrite the headline" not in text and "headline may change" not in text
    assert "do not retarget the headline" in text and "only words it already contains" in text
