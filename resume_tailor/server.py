"""Tiny localhost UI so a JD can be pasted in the browser."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from resume_tailor.cli import (
    _ensure_original_backup,
    _original_path,
    _render_changelog,
    _load_source_text,
    _resolve_resume,
    _write_outputs,
    verify_output,
)
from resume_tailor.guardrails import (
    apply_guardrails,
    check_changelog_matches_diff,
    unified_diff,
)
from resume_tailor.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    LLMError,
    complete,
    load_dotenv,
    tailor_resume,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parent.parent

# One tailoring run at a time: every run writes the same files in out/, so
# concurrent requests would interleave and /api/download could serve a
# half-written document.
_TAILOR_LOCK = threading.Lock()

# A job description is text. Anything this large is not one.
MAX_BODY_BYTES = 8_000_000

# Only requests addressed to this machine are served. Without this check any
# web page the user visits can drive the tool, and a DNS-rebinding page can
# read the resume back out of it.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

# The file the most recent successful run produced. /api/download used to serve
# a hardcoded name, so it could hand back a previous job's resume.
_LAST_DOWNLOAD: Path | None = None


def _json_bytes(payload: dict, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return status, body, "application/json; charset=utf-8"



def _save_attached_resume(payload: dict) -> Path | None:
    """Optional base64 .docx from the browser. None = use the in-repo resume."""
    import base64
    import re

    b64 = str(payload.get("resume_b64") or "").strip()
    if not b64:
        return None
    name = str(payload.get("resume_name") or "attached.docx")
    name = Path(name).name
    if not name.lower().endswith(".docx"):
        raise ValueError("Attached resume must be a .docx file.")
    # strip data-url prefix if present
    if "," in b64 and b64.lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        data = base64.b64decode(b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not decode attached resume: {exc}") from exc
    if len(data) < 100 or data[:2] != b"PK":
        raise ValueError("Attached file does not look like a .docx (zip) file.")
    if len(data) > 5_000_000:
        raise ValueError("Attached resume is too large (max 5 MB).")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "attached.docx"
    dest_dir = ROOT / "out" / "uploads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    dest.write_bytes(data)
    return dest


def _run_tailor(job_description: str, resume_path: Path | None = None) -> dict:
    with _TAILOR_LOCK:
        return _run_tailor_locked(job_description, resume_path)


def _run_tailor_locked(job_description: str, resume_path: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env")
    import os

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "ok": False,
            "error": "OPENAI_API_KEY is missing. Put it in .env in the project folder.",
        }
    jd = job_description.strip()
    if not jd:
        return {"ok": False, "error": "Job description is empty."}

    if resume_path is None:
        resume_path = _resolve_resume(None)
        _ensure_original_backup(resume_path)
    elif not resume_path.is_file():
        return {"ok": False, "error": f"Attached resume not found: {resume_path}"}
    original = _load_source_text(resume_path)
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL
    model = os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL
    try:
        result = tailor_resume(
            resume_markdown=original,
            job_description=jd,
            api_key=api_key,
            base_url=base_url,
            model=model,
            complete_fn=complete,
        )
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}

    tailored, report = apply_guardrails(original, result.resume_markdown)
    contradictions = check_changelog_matches_diff(
        result.changelog, report.changed_line_count
    )
    if contradictions:
        report.violations.extend(contradictions)
        report.ok = False
    diff_text = unified_diff(original, tailored, fromfile=str(resume_path), tofile=str(resume_path))
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    # The untouched model reply: the only way to tell a model that echoed the
    # resume from a pipeline that dropped its edits.
    (out_dir / "model.raw.txt").write_text(result.raw, encoding="utf-8")
    written = None
    if report.ok:
        written = _write_outputs(
            tailored=tailored,
            resume_path=resume_path,
            out_dir=out_dir,
        )
        drift = verify_output(written, tailored)
        if drift:
            report.violations.extend(drift)
            report.ok = False
    else:
        (out_dir / "resume.rejected.md").write_text(tailored, encoding="utf-8")
    changelog = _render_changelog(
        match_line=result.match_line,
        match_score=result.match_score,
        changelog=result.changelog,
        report=report,
        changed_line_count=report.changed_line_count,
        resume_path=written or resume_path,
        wrote_in_place=False,
        score_inferred=result.score_inferred,
    )
    (out_dir / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (out_dir / "resume.diff").write_text(diff_text or "(no changes)\n", encoding="utf-8")
    global _LAST_DOWNLOAD
    download = None
    if report.ok and written is not None and written.suffix.lower() == ".docx":
        _LAST_DOWNLOAD = written
        download = "/api/download"
    else:
        _LAST_DOWNLOAD = None
    return {
        "ok": report.ok,
        "changelog": changelog,
        "diff": diff_text,
        "violations": report.violations,
        "resume_path": str(written or resume_path),
        "match_score": result.match_score,
        "match_line": result.match_line,
        "score_inferred": result.score_inferred,
        "changed_lines": report.changed_line_count,
        "changes": result.changelog,
        "warnings": report.warnings,
        "download": download,
    }


def _run_reset() -> dict:
    resume_path = _resolve_resume(None)
    original = _original_path(resume_path)
    if not original.is_file():
        return {"ok": False, "error": f"No backup at {original}."}
    import shutil

    shutil.copyfile(original, resume_path)
    return {"ok": True, "message": f"Restored {resume_path} from {original}"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        return host in ALLOWED_HOSTS or host == ""

    def _reject_foreign_host(self) -> bool:
        if self._host_allowed():
            return False
        self._send(
            403,
            b'{"error":"this server only answers requests addressed to localhost"}',
            "application/json",
        )
        return True

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_foreign_host():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (STATIC_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/health":
            status, body, ctype = _json_bytes({"ok": True})
            self._send(status, body, ctype)
            return
        if path == "/api/download":
            dest = _LAST_DOWNLOAD
            if dest is None or not dest.is_file():
                self._send(
                    404,
                    b'{"error":"no tailored Word file from this session yet"}',
                    "application/json",
                )
                return
            if not dest.is_file():
                self._send(404, b'{"error":"no tailored Word file yet"}', "application/json")
                return
            data = dest.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{dest.name}"',
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_foreign_host():
            return
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, b'{"error":"bad Content-Length"}', "application/json")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send(413, b'{"error":"request body too large"}', "application/json")
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type != "application/json":
            self._send(
                415,
                b'{"error":"Content-Type must be application/json"}',
                "application/json",
            )
            return
        origin = self.headers.get("Origin")
        if origin:
            host = urlparse(origin).hostname or ""
            if host.lower() not in ALLOWED_HOSTS:
                self._send(
                    403, b'{"error":"cross-origin request refused"}', "application/json"
                )
                return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send(400, b'{"error":"invalid JSON"}', "application/json")
            return
        try:
            if path == "/api/tailor":
                try:
                    attached = _save_attached_resume(payload)
                except ValueError as exc:
                    result = {"ok": False, "error": str(exc)}
                    _, body, ctype = _json_bytes(result, 400)
                    self._send(400, body, ctype)
                    return
                result = _run_tailor(str(payload.get("jd") or ""), attached)
                status = 200 if "error" not in result or result.get("ok") else 400
                if result.get("error") and not result.get("ok"):
                    status = 400
                _, body, ctype = _json_bytes(result, status)
                self._send(status, body, ctype)
                return
            if path == "/api/reset":
                result = _run_reset()
                status = 200 if result.get("ok") else 400
                _, body, ctype = _json_bytes(result, status)
                self._send(status, body, ctype)
                return
        except Exception as exc:  # noqa: BLE001
            _, body, ctype = _json_bytes({"ok": False, "error": str(exc)}, 500)
            self._send(500, body, ctype)
            return
        self._send(404, b'{"error":"not found"}', "application/json")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    load_dotenv(ROOT / ".env")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Resume tailor UI: http://{host}:{port}")
    print("Paste a job description in the browser. Ctrl-C to stop.")
    httpd.serve_forever()
