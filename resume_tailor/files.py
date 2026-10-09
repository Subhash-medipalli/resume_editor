"""Validate destinations and publish complete files without following output links."""

from contextlib import contextmanager
from collections.abc import Sequence
from pathlib import Path
import os
import tempfile
import time
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
        _replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)


def _replace(staging: Path, target: Path, attempts: int = 40) -> None:
    """os.replace, retried while Windows refuses to replace a file another request
    has open (a status poll reading status.json); such a read lasts milliseconds."""
    for attempt in range(attempts):
        try:
            os.replace(staging, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def write_text(dest: Path, text: str, *, sources: Sequence[Path] = ()) -> None:
    with atomic_output(dest, sources=sources) as staging:
        staging.write_text(text, encoding="utf-8")


def save_copy(data: bytes, folder: Path, name: str) -> Path:
    """Write `data` into `folder` as `name`, or `name (1)`, `name (2)` ... if taken.

    Created exclusively, so a file already there (the source resume, an earlier
    download) is never replaced, and a symlink at the target is never followed.
    """
    folder.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    for number in range(1000):
        target = folder / (name if number == 0 else f"{stem} ({number}){suffix}")
        try:
            handle = open(target, "xb")
        except FileExistsError:  # only the open means "taken"; a later failure must not skip ahead
            continue
        try:
            with handle:  # the buffered bytes reach the disk at close, so close is inside the cleanup
                handle.write(data)
        except BaseException:
            target.unlink(missing_ok=True)  # never leave half a resume behind
            raise
        return target
    raise OSError(f"Too many files named like {name} in {folder}.")
