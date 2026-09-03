"""The structured view is what makes every later guardrail possible."""

from resume_tailor.structure import (
    BULLET,
    JOB,
    NAME,
    SECTION,
    TITLE,
    from_markdown,
    parse_paragraphs,
    to_markdown,
)

# (text, Word style) exactly as the .docx hands them over — no markdown markers.
FLAT_DOCX = [
    ("        Alex Placeholder (SAMPLE)", "Normal"),
    ("not-a-real-person@example.invalid | +1-555-0100", "Normal"),
    ("Senior Widget Engineer (SAMPLE)", "Normal"),
    ("PROFESSIONAL SUMMARY:", "Normal"),
    ("Engineer with 9+ years of widget experience.", "List Paragraph"),
    ("TECHNICAL SKILLS:", "Normal"),
    ("Languages: Python, SQL", "Normal"),
    ("PROFESSIONAL EXPERIENCE:", "Normal"),
    ("Northstar Fictional Bank, Testland | Jan 2024 – Present", "Normal"),
    ("Senior Widget Engineer (SAMPLE)", "Normal"),
    ("Built widgets.", "List Paragraph"),
    ("Environment: Python, SQL", "Normal"),
    ("EDUCATION:", "Normal"),
    ("BSc Widgetry - Placeholder State University (SAMPLE), 2018", "List Paragraph"),
]


def test_flat_word_paragraphs_are_classified():
    blocks = parse_paragraphs(FLAT_DOCX)
    kinds = [block.kind for block in blocks]
    assert kinds[0] == NAME
    assert blocks[3].kind == SECTION and blocks[3].text == "PROFESSIONAL SUMMARY:"
    assert blocks[8].kind == JOB
    assert blocks[9].kind == TITLE, "the line under a job heading is the role"
    assert kinds.count(BULLET) == 3
    assert kinds.count(SECTION) == 4


def test_markdown_gives_the_model_structure_the_docx_lost():
    markdown = to_markdown(parse_paragraphs(FLAT_DOCX))
    assert "# Alex Placeholder (SAMPLE)" in markdown
    assert "## PROFESSIONAL SUMMARY" in markdown
    assert "### Northstar Fictional Bank, Testland | Jan 2024 – Present" in markdown
    assert "- Built widgets." in markdown
    # Without these markers the guardrails extract nothing and the model cannot
    # tell a bullet from a paragraph.
    assert markdown.count("\n## ") == 4


def test_round_trip_is_stable():
    blocks = parse_paragraphs(FLAT_DOCX)
    again = from_markdown(to_markdown(blocks))
    assert [b.key for b in again] == [b.key for b in blocks]


def test_para_index_points_back_at_the_source_paragraph():
    blocks = parse_paragraphs(FLAT_DOCX)
    for block in blocks:
        assert FLAT_DOCX[block.para_index][0].strip() == block.text


def test_an_all_caps_name_is_not_a_section():
    """"JANE DOE" as a heading emptied the contact block and disabled the guard."""
    blocks = parse_paragraphs([
        ("JANE DOE", "Normal"),
        ("jane@example.invalid | +1-555-0100", "Normal"),
        ("PROFESSIONAL SUMMARY:", "Normal"),
        ("Did things.", "List Paragraph"),
    ])
    assert blocks[0].kind == NAME
    assert blocks[1].kind == "contact"
    assert blocks[2].kind == SECTION


def test_an_all_caps_employer_is_not_a_section():
    """"GOLDMAN SACHS" as a section made every employer/title check a no-op."""
    blocks = parse_paragraphs([
        ("Jane Doe", "Normal"),
        ("jane@example.invalid", "Normal"),
        ("PROFESSIONAL EXPERIENCE:", "Normal"),
        ("GOLDMAN SACHS", "Normal"),
        ("Data Scientist | Jan 2020 – Present", "Normal"),
    ])
    assert [b.kind for b in blocks].count(SECTION) == 1
    assert blocks[3].kind != SECTION
