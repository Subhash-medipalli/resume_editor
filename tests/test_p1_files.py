from pathlib import Path
import os

import pytest
from docx import Document

from resume_tailor.pipeline import _write_outputs
from resume_tailor.docx_io import extract_markdown, write_tailored_docx
from resume_tailor.files import atomic_output
from tests.helpers import SAMPLE_RESUME


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_word_writer_refuses_source_aliases(tmp_path, alias):
    source = tmp_path / "base.docx"
    document = Document()
    document.add_paragraph("Original Candidate")
    document.save(source)
    before = source.read_bytes()
    dest = source if alias == "same" else tmp_path / "output.docx"
    if alias == "symlink":
        dest.symlink_to(source)
    elif alias == "hardlink":
        os.link(source, dest)
    with pytest.raises(ValueError, match="protected source"):
        write_tailored_docx(source, "# Changed Candidate\n", dest)
    assert source.read_bytes() == before


def test_markdown_source_is_checked_before_any_output(tmp_path):
    source = tmp_path / "resume_tailored.md"
    source.write_text(SAMPLE_RESUME)
    with pytest.raises(ValueError, match="protected source"):
        _write_outputs(tailored="# Altered", resume_path=source, out_dir=tmp_path)
    assert source.read_text() == SAMPLE_RESUME


def test_writer_does_not_publish_a_failed_verification(tmp_path, monkeypatch):
    source = tmp_path / "base.docx"
    document = Document()
    document.add_paragraph("Original Candidate")
    document.save(source)
    dest = tmp_path / "tailored.docx"
    dest.write_bytes(b"previous result")
    monkeypatch.setattr("resume_tailor.docx_io.verify_written_docx", lambda *a: ["synthetic mismatch"])
    with pytest.raises(ValueError, match="verification failed"):
        write_tailored_docx(source, extract_markdown(source), dest)
    assert dest.read_bytes() == b"previous result"
    assert not list(tmp_path.glob(".resume-tailor-*"))


def test_atomic_write_rechecks_links_at_publication(tmp_path):
    source = tmp_path / "base.md"
    source.write_text("original")
    dest = tmp_path / "out.md"
    with pytest.raises(ValueError, match="protected source"):
        with atomic_output(dest, sources=[source]) as staging:
            staging.write_text("changed")
            dest.symlink_to(source)
    assert source.read_text() == "original"
