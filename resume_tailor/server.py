"""Tiny localhost UI so a JD can be pasted in the browser."""

from __future__ import annotations

import json
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from resume_tailor.cli import _original_path, _resolve_resume
from resume_tailor.files import atomic_output, new_run_dir, write_text
from resume_tailor.pipeline import run_tailoring, validate_job_description
from resume_tailor.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, complete, load_dotenv

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT = Path.cwd()

# ponytail: one active provider run for this local app; return busy instead of
# silently queuing. Use a bounded queue if concurrent users become necessary.
_TAILOR_LOCK = threading.Lock()

# A job description is text. Anything this large is not one.
MAX_BODY_BYTES = 8_000_000

# Only requests addressed to this machine are served. Without this check any
# web page the user visits can drive the tool, and a DNS-rebinding page can
# read the resume back out of it.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}

def _new_run_dir() -> Path:
    return new_run_dir(ROOT / "out")


def _json_bytes(payload: dict, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return status, body, "application/json; charset=utf-8"



def _save_attached_resume(payload: dict, run_dir: Path) -> Path | None:
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
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not decode attached resume: {exc}") from exc
    if len(data) < 100 or data[:2] != b"PK":
        raise ValueError("Attached file does not look like a .docx (zip) file.")
    if len(data) > 5_000_000:
        raise ValueError("Attached resume is too large (max 5 MB).")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "attached.docx"
    dest_dir = run_dir / "source"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    with dest.open("xb") as stream:
        stream.write(data)
    return dest


def _run_tailor(job_description: str, resume_path: Path | None = None, *, run_dir: Path | None = None) -> dict:
    run_dir = run_dir or _new_run_dir()
    source = resume_path or _resolve_resume(None)
    if source.suffix.lower() != ".docx":
        raise ValueError("The browser requires a Word .docx base resume.")
    snapshot = run_dir / "source" / source.name
    if source.resolve() != snapshot.resolve():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with snapshot.open("xb") as stream:
            stream.write(source.read_bytes())
    return _execute_run(job_description, snapshot, run_dir)


def _execute_run(job_description: str, resume_path: Path, out_dir: Path) -> dict:
    load_dotenv(ROOT / ".env")
    import os

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY is missing. Put it in .env in the project folder."}
    result = run_tailoring(
        job_description=job_description, resume_path=resume_path, out_dir=out_dir,
        api_key=api_key, base_url=os.environ.get("OPENAI_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        model=os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL,
        complete_fn=complete,
        progress=lambda message: write_text(out_dir / "status.json", json.dumps({"state": "working", "message": message})),
    )
    result["review"] = None
    if result["ok"]:
        if result["review_required"]:
            result["review"] = f"/api/review/{out_dir.name}"
        else:
            result["download"] = f"/api/download/{out_dir.name}"
    return result


def _finish_run(jd: str, attached: Path | None, run_dir: Path) -> None:
    try:
        try:
            result = _run_tailor(jd, attached, run_dir=run_dir)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "download": None, "run_id": run_dir.name}
        write_text(run_dir / "status.json", json.dumps({"state": "complete", "result": result}))
    finally:
        _TAILOR_LOCK.release()


def _read_result(run_id: str) -> tuple[Path, dict, bytes]:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Unknown run.")
    run_dir = ROOT / "out" / "runs" / run_id
    manifest = run_dir / "result.json"
    result = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Invalid result manifest.")
    name = result.get("file", "")
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".docx"):
        raise ValueError("Invalid result file.")
    dest = run_dir / name
    if dest.is_symlink() or dest.resolve().parent != run_dir.resolve():
        raise ValueError("Result file moved outside its run.")
    data = dest.read_bytes()
    if hashlib.sha256(data).hexdigest() != result.get("sha256"):
        raise ValueError("The result changed after verification. Run the tailor again.")
    return manifest, result, data


def _run_reset() -> dict:
    resume_path = _resolve_resume(None)
    original = _original_path(resume_path)
    if not original.is_file():
        return {"ok": False, "error": f"No backup at {original}."}
    import shutil

    with atomic_output(resume_path, sources=[original]) as staging:
        shutil.copyfile(original, staging)
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
            from resume_tailor.docx_io import find_parent_docx

            base = find_parent_docx(ROOT)
            status, body, ctype = _json_bytes({"ok": True, "base": base.name if base else None})
            self._send(status, body, ctype)
            return
        if path.startswith("/api/status/"):
            run_id = path.removeprefix("/api/status/")
            if not re.fullmatch(r"[0-9a-f]{32}", run_id):
                self._send(404, b'{"error":"unknown run"}', "application/json")
                return
            try:
                data = (ROOT / "out/runs" / run_id / "status.json").read_bytes()
            except OSError:
                self._send(404, b'{"error":"unknown run"}', "application/json")
                return
            self._send(200, data, "application/json")
            return
        if path.startswith("/api/download/"):
            try:
                _, result, data = _read_result(path.removeprefix("/api/download/"))
            except (OSError, ValueError):
                self._send(
                    404,
                    b'{"error":"no verified Word file for this run"}',
                    "application/json",
                )
                return
            if result.get("status") != "ready":
                self._send(409, b'{"error":"review the changed claims before downloading"}', "application/json")
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{result["file"]}"',
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b'{"error":"invalid JSON"}', "application/json")
            return
        if not isinstance(payload, dict):
            self._send(400, b'{"error":"JSON body must be an object"}', "application/json")
            return
        try:
            if path == "/api/tailor":
                try:
                    validate_job_description(payload.get("jd"))
                except ValueError as exc:
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                if not _TAILOR_LOCK.acquire(blocking=False):
                    self._send(*_json_bytes({"ok": False, "error": "Another resume is being processed. Wait for it to finish, then run again."}, 409))
                    return
                try:
                    run_dir = _new_run_dir()
                    attached = _save_attached_resume(payload, run_dir)
                    write_text(run_dir / "status.json", json.dumps({"state": "working", "message": "Preparing the source resume"}))
                    worker = threading.Thread(target=_finish_run, args=(payload["jd"], attached, run_dir), daemon=True)
                    worker.start()
                except Exception as exc:
                    _TAILOR_LOCK.release()
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                self._send(*_json_bytes({"ok": True, "run_id": run_dir.name, "status": f"/api/status/{run_dir.name}"}, 202))
                return
            if path.startswith("/api/review/"):
                try:
                    manifest, result, _ = _read_result(path.removeprefix("/api/review/"))
                    if payload.get("reviewed") is not True or payload.get("sha256") != result["sha256"]:
                        raise ValueError("Confirm the edited claims for this exact document.")
                    if result.get("status") not in {"review", "ready"}:
                        raise ValueError("This run cannot be approved.")
                    result["status"] = "ready"
                    write_text(manifest, json.dumps(result))
                except (OSError, ValueError) as exc:
                    status, body, ctype = _json_bytes({"ok": False, "error": str(exc)}, 409)
                else:
                    status, body, ctype = _json_bytes({"ok": True, "download": f"/api/download/{manifest.parent.name}"})
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
