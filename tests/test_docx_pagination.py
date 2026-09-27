from docx import Document

from resume_tailor.docx_io import extract_markdown, write_tailored_docx


def test_headings_stay_with_content_across_blank_spacers(tmp_path):
    document = Document()
    document.add_paragraph("Sample Person")
    document.add_paragraph("person@example.invalid")
    document.add_paragraph("PROFESSIONAL EXPERIENCE")
    document.add_paragraph("")
    document.add_paragraph("Example Company | Jan 2020 – Present")
    document.add_paragraph("Software Engineer")
    document.add_paragraph("")
    document.add_paragraph("Built Python services.", "List Bullet")
    document.add_paragraph("\t\t ")
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    document.save(source)
    before = source.read_bytes()
    write_tailored_docx(source, extract_markdown(source), output)
    paragraphs = Document(output).paragraphs
    assert all(paragraphs[i].paragraph_format.keep_with_next for i in range(2, 7))
    assert paragraphs[7].paragraph_format.keep_with_next is None
    assert len(paragraphs) == 8
    assert source.read_bytes() == before
