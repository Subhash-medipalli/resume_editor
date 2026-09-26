"""Validate destinations and publish complete files without following output links."""

from contextlib import contextmanager
from collections.abc import Sequence
from pathlib import Path
import os
import tempfile
import uuid


def new_run_dir(output_root: Path) -> Path:
    path = output_root / "runs" / uuid.uuid4().hex
    path.mkdir(parents=True, mode=0o700)
    return path


def check_output_paths(sources: Sequence[Path], destinations: Sequence[Path]) -> None:
    for index, dest in enumerate(destinations):
        for source in [*sources, *destinations[:index]]:
            if dest.resolve() == source.resolve() or (
                dest.exists() and source.exists() and dest.samefile(source)
            ):
                raise ValueError(f"Output {dest} points to a protected source or another output: {source}")


@contextmanager
def atomic_output(dest: Path, *, sources: Sequence[Path] = ()):
    check_output_paths(sources, [dest])
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Resolve the directory once; replace the directory entry, never its target.
    target = dest.parent.resolve() / dest.name
    fd, name = tempfile.mkstemp(prefix=".resume-tailor-", suffix=dest.suffix, dir=target.parent)
    os.close(fd)
    staging = Path(name)
    try:
        yield staging
        check_output_paths(sources, [target])
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)


def write_text(dest: Path, text: str, *, sources: Sequence[Path] = ()) -> None:
    with atomic_output(dest, sources=sources) as staging:
        staging.write_text(text, encoding="utf-8")
