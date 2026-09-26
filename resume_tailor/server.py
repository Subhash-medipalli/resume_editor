"""Tiny localhost UI so a JD can be pasted in the browser."""

from __future__ import annotations

import json
import hashlib
import io
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from resume_tailor.cli import _resolve_resume
from resume_tailor.files import new_run_dir, write_text
from resume_tailor.pipeline import run_tailoring, validate_job_description
from resume_tailor.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, complete, load_dotenv

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT = Path.cwd()

# ponytail: one active provider run for this local app; return busy instead of
# silently queuing. Use a bounded queue if concurrent users become necessary.
_TAILOR_LOCK = threading.Lock()
_ACTIVE_RUN_ID: str | None = None

# A job description is text. Anything this large is not one.
MAX_BODY_BYTES = 8_000_000

# Only requests addressed to this machine are served. Without this check any
# web page the user visits can drive the tool, and a DNS-rebinding page can
# read the resume back out of it.
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}

def _new_run_dir() -> Path:
    return new_run_dir(ROOT / "out")


def _json_bytes(payload: dict, status: int = 200) -> tuple[int, bytes, str]:
    body = json.dumps(payload).encode("utf-8")
    return status, body, "application/json; charset=utf-8"



def _save_attached_resume(payload: dict, run_dir: Path) -> Path | None:
    """Optional base64 .docx from the browser. None = use the in-repo resume."""
    import base64
    b64 = payload.get("resume_b64", "")
    if not isinstance(b64, str):
        raise ValueError("resume_b64 must be a base64 string.")
    b64 = b64.strip()
    if not b64:
        return None
    name = payload.get("resume_name") or "attached.docx"
    if not isinstance(name, str):
        raise ValueError("resume_name must be a string.")
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
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 1000 or sum(item.file_size for item in members) > 25_000_000:
                raise ValueError("The Word document expands beyond the supported size (25 MB).")
            if not {"[Content_Types].xml", "word/document.xml"}.issubset(archive.namelist()):
                raise ValueError("Attached file is not a Word .docx document.")
        from docx import Document
        Document(io.BytesIO(data))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Could not read this Word document. Save it as a valid .docx file.") from exc
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "attached.docx"
    dest_dir = run_dir / "source"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe
    with dest.open("xb") as stream:
        stream.write(data)
    return dest


def _run_tailor(
    job_description: str,
    resume_path: Path | None = None,
    *,
    run_dir: Path | None = None,
    two_pass: bool = False,
) -> dict:
    run_dir = run_dir or _new_run_dir()
    source = resume_path or _resolve_resume(None)
    if source.suffix.lower() != ".docx":
        raise ValueError("The browser requires a Word .docx base resume.")
    snapshot = run_dir / "source" / source.name
    if source.resolve() != snapshot.resolve():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        with snapshot.open("xb") as stream:
            stream.write(source.read_bytes())
    return _execute_run(job_description, snapshot, run_dir, two_pass=two_pass)


def _execute_run(
    job_description: str,
    resume_path: Path,
    out_dir: Path,
    *,
    two_pass: bool = False,
) -> dict:
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
        two_pass=two_pass,
    )
    if result["ok"]:
        result["download"] = f"/api/download/{out_dir.name}"
    return result


def _finish_run(
    jd: str,
    attached: Path | None,
    run_dir: Path,
    two_pass: bool = False,
) -> None:
    global _ACTIVE_RUN_ID
    try:
        try:
            result = _run_tailor(jd, attached, run_dir=run_dir, two_pass=two_pass)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "download": None, "run_id": run_dir.name}
        write_text(run_dir / "status.json", json.dumps({"state": "complete", "result": result}))
    finally:
        _ACTIVE_RUN_ID = None
        _TAILOR_LOCK.release()


def _read_result(run_id: str) -> tuple[dict, bytes]:
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
    return result, data


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _host_allowed(self) -> bool:
        authority = self.headers.get("Host", "")
        try:
            parsed = urlparse("//" + authority)
            return (
                len(self.headers.get_all("Host", [])) == 1
                and parsed.hostname in ALLOWED_HOSTS
                and (parsed.port or 80) == self.server.server_port
                and not (parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment)
            )
        except ValueError:
            return False

    def _reject_foreign_host(self) -> bool:
        origin = self.headers.get("Origin")
        if (self._host_allowed()
                and (not origin or origin.lower() == "http://" + self.headers["Host"].lower())
                and self.headers.get("Sec-Fetch-Site") != "cross-site"):
            return False
        self._send(
            403,
            b'{"error":"this server only accepts requests from its own localhost origin"}',
            "application/json",
        )
        return True

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
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
                status = json.loads((ROOT / "out/runs" / run_id / "status.json").read_text(encoding="utf-8"))
                if status.get("state") == "working" and run_id != _ACTIVE_RUN_ID:
                    status = {"state": "complete", "result": {
                        "ok": False, "download": None, "run_id": run_id,
                        "error": "This run was interrupted. Start a new tailoring run.",
                    }}
                data = json.dumps(status).encode("utf-8")
            except (OSError, ValueError):
                self._send(404, b'{"error":"unknown run"}', "application/json")
                return
            self._send(200, data, "application/json")
            return
        if path.startswith("/api/download/"):
            try:
                result, data = _read_result(path.removeprefix("/api/download/"))
            except (OSError, ValueError):
                self._send(
                    404,
                    b'{"error":"no verified Word file for this run"}',
                    "application/json",
                )
                return
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.send_header(
                "Content-Disposition",
                "attachment; filename=\"resume.docx\"; filename*=UTF-8''" + quote(result["file"], safe=""),
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return
        self._send(404, b'{"error":"not found"}', "application/json")

    def do_POST(self) -> None:  # noqa: N802
        global _ACTIVE_RUN_ID
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
                    two_pass = payload.get("two_pass", False)
                    if not isinstance(two_pass, bool):
                        raise ValueError("two_pass must be true or false.")
                except ValueError as exc:
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                if not _TAILOR_LOCK.acquire(blocking=False):
                    self._send(*_json_bytes({"ok": False, "error": "Another resume is being processed. Wait for it to finish, then run again."}, 409))
                    return
                try:
                    run_dir = _new_run_dir()
                    attached = _save_attached_resume(payload, run_dir)
                    _ACTIVE_RUN_ID = run_dir.name
                    write_text(run_dir / "status.json", json.dumps({"state": "working", "message": "Preparing the source resume"}))
                    worker = threading.Thread(
                        target=_finish_run,
                        args=(payload["jd"], attached, run_dir, two_pass),
                        daemon=True,
                    )
                    worker.start()
                except Exception as exc:
                    _ACTIVE_RUN_ID = None
                    _TAILOR_LOCK.release()
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                self._send(*_json_bytes({"ok": True, "run_id": run_dir.name, "status": f"/api/status/{run_dir.name}"}, 202))
                return
        except Exception as exc:  # noqa: BLE001
            _, body, ctype = _json_bytes({"ok": False, "error": str(exc)}, 500)
            self._send(500, body, ctype)
            return
        self._send(404, b'{"error":"not found"}', "application/json")


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("This local app must bind to 127.0.0.1 or localhost.")
    load_dotenv(ROOT / ".env")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Resume tailor UI: http://{host}:{port}")
    print("Paste a job description in the browser. Ctrl-C to stop.")
    httpd.serve_forever()
