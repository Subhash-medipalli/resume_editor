from pathlib import Path
import os

import pytest
from docx import Document

from resume_tailor.pipeline import _write_outputs
from resume_tailor.docx_io import extract_markdown, write_tailored_docx
from resume_tailor.files import atomic_output, save_copy
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


def test_replace_waits_out_a_windows_reader_holding_the_file(tmp_path, monkeypatch):
    import os
    from resume_tailor import files
    real, calls = os.replace, []
    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real(src, dst)
    monkeypatch.setattr(files.os, "replace", flaky)
    monkeypatch.setattr(files.time, "sleep", lambda seconds: None)
    files.write_text(tmp_path / "status.json", "{}")
    assert (tmp_path / "status.json").read_text() == "{}" and len(calls) == 3


def test_save_copy_never_replaces_a_file_that_is_already_there(tmp_path):
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "resume.docx").write_bytes(b"the source resume")
    first = save_copy(b"one", folder, "resume.docx")
    second = save_copy(b"two", folder, "resume.docx")
    assert (folder / "resume.docx").read_bytes() == b"the source resume"
    assert (first.name, first.read_bytes()) == ("resume (1).docx", b"one")
    assert (second.name, second.read_bytes()) == ("resume (2).docx", b"two")


def test_save_copy_creates_the_folder_and_does_not_follow_a_symlink(tmp_path):
    folder = tmp_path / "new" / "place"
    folder.parent.mkdir()
    folder.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"untouched")
    (folder / "resume.docx").symlink_to(victim)
    assert save_copy(b"copy", folder, "resume.docx").name == "resume (1).docx"
    assert victim.read_bytes() == b"untouched"
    assert save_copy(b"x", tmp_path / "made" / "on" / "demand", "r.docx").read_bytes() == b"x"


def test_save_copy_leaves_nothing_behind_when_the_write_fails(tmp_path):
    with pytest.raises(TypeError):
        save_copy(object(), tmp_path, "b.docx")
    assert list(tmp_path.iterdir()) == []


def test_save_copy_leaves_nothing_behind_when_the_disk_fails_at_close(tmp_path, monkeypatch):
    # A buffered write only reaches the disk when the file closes, which is where "disk full" shows up.
    from resume_tailor import files
    real_open = open

    class FullDisk:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def write(self, data):
            return self.handle.write(data)
        def __exit__(self, *exc):
            self.handle.close()
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(files, "open", lambda *args: FullDisk(real_open(*args)), raising=False)
    (tmp_path / "r.docx").write_bytes(b"old")
    with pytest.raises(OSError, match="No space"):
        save_copy(b"data", tmp_path, "r.docx")  # "r.docx" is taken, so this is the "r (1).docx" attempt
    assert [p.name for p in tmp_path.iterdir()] == ["r.docx"] and (tmp_path / "r.docx").read_bytes() == b"old"
