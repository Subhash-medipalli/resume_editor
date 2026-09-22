"""Tiny localhost UI so a JD can be pasted in the browser."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from resume_tailor.files import new_run_dir, write_text
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



_MISSING_BASE = (
    "Attach a .docx base resume, or choose a previously uploaded one."
)


def _library_root() -> Path:
    return ROOT / "out" / "library" / "resumes"


def _safe_docx_name(name: str) -> str:
    cleaned = Path(str(name or "attached.docx")).name
    if not cleaned.lower().endswith(".docx"):
        raise ValueError("Attached resume must be a .docx file.")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", cleaned).strip("._") or "attached"
    if not safe.lower().endswith(".docx"):
        safe += ".docx"
    return safe


def _validate_docx_bytes(data: bytes) -> None:
    if len(data) < 100 or data[:2] != b"PK":
        raise ValueError("Attached file does not look like a .docx (zip) file.")
    if len(data) > 5_000_000:
        raise ValueError("Attached resume is too large (max 5 MB).")


def _decode_upload(payload: dict) -> tuple[str, str, bytes]:
    b64 = str(payload.get("resume_b64") or "").strip()
    if "," in b64 and b64.lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not decode attached resume: {exc}") from exc
    _validate_docx_bytes(data)
    original = Path(str(payload.get("resume_name") or "attached.docx")).name
    return original, _safe_docx_name(original), data


def _store_library_resume(filename: str, data: bytes, original_name: str) -> str:
    resume_id = uuid.uuid4().hex
    folder = _library_root() / resume_id
    folder.mkdir(parents=True, mode=0o700)
    safe = _safe_docx_name(filename)
    with (folder / safe).open("xb") as stream:
        stream.write(data)
    write_text(folder / "meta.json", json.dumps({
        "id": resume_id,
        "filename": safe,
        "original_name": Path(original_name).name,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bytes": len(data),
    }))
    return resume_id


def _read_library_resume(resume_id: str) -> tuple[dict, bytes]:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_id):
        raise ValueError("Unknown saved resume.")
    library = _library_root().resolve()
    folder = (library / resume_id).resolve()
    if folder.parent != library:
        raise ValueError("Unknown saved resume.")
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unknown saved resume.") from exc
    if not isinstance(meta, dict):
        raise ValueError("Saved resume is invalid.")
    filename = meta.get("filename")
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.lower().endswith(".docx"):
        raise ValueError("Saved resume is invalid.")
    dest = folder / filename
    if dest.is_symlink() or not dest.is_file() or dest.resolve().parent != folder:
        raise ValueError("Saved resume is invalid.")
    data = dest.read_bytes()
    _validate_docx_bytes(data)
    return meta, data


def _selection_from_payload(payload: dict) -> tuple[str, bytes, str | None, str]:
    """Return disk filename, bytes, library id, and the name to show.

    A new upload returns library id None; the caller stores it. Neither an
    upload nor a saved id means there is no base — never a built-in sample.
    """
    if str(payload.get("resume_b64") or "").strip():
        original, filename, data = _decode_upload(payload)
        return filename, data, None, original
    resume_id = str(payload.get("resume_id") or "").strip()
    if resume_id:
        meta, data = _read_library_resume(resume_id)
        shown = meta.get("original_name") if isinstance(meta.get("original_name"), str) else meta["filename"]
        return str(meta["filename"]), data, resume_id, shown
    raise ValueError(_MISSING_BASE)


def _snapshot_into_run(run_dir: Path, filename: str, data: bytes) -> Path:
    dest_dir = run_dir / "source"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_docx_name(filename)
    with dest.open("xb") as stream:
        stream.write(data)
    return dest


def _write_run_meta(run_dir: Path, *, source_name: str, resume_id: str, jd: str) -> None:
    write_text(run_dir / "meta.json", json.dumps({
        "id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_name": source_name,
        "resume_id": resume_id,
        "jd_preview": " ".join(jd.split())[:160],
    }))


def _list_resumes() -> list[dict]:
    root = _library_root()
    if not root.is_dir():
        return []
    items = []
    for folder in root.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(meta, dict) or meta.get("id") != folder.name:
            continue
        items.append({
            "id": folder.name,
            "filename": meta.get("filename") if isinstance(meta.get("filename"), str) else "",
            "original_name": meta.get("original_name") if isinstance(meta.get("original_name"), str) else meta.get("filename"),
            "saved_at": meta.get("saved_at") if isinstance(meta.get("saved_at"), str) else "",
            "bytes": meta.get("bytes") if isinstance(meta.get("bytes"), int) else None,
        })
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    return items[:50]


def _list_runs() -> list[dict]:
    root = ROOT / "out" / "runs"
    if not root.is_dir():
        return []
    items = []
    for folder in root.iterdir():
        if not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        meta = {}
        try:
            loaded = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, json.JSONDecodeError, UnicodeError):
            meta = {}
        state, ok = "unknown", None
        try:
            status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
            if isinstance(status, dict):
                state = status.get("state") if status.get("state") in {"working", "complete"} else "unknown"
                if state == "complete" and isinstance(status.get("result"), dict):
                    ok = bool(status["result"].get("ok"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            pass
        download, filename = None, None
        try:
            result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            if isinstance(result, dict) and result.get("status") == "ready":
                name = result.get("file")
                if isinstance(name, str) and Path(name).name == name and name.endswith(".docx"):
                    filename = name
                    download = f"/api/download/{folder.name}"
        except (OSError, json.JSONDecodeError, UnicodeError):
            pass
        created = meta.get("created_at") if isinstance(meta.get("created_at"), str) else ""
        if not created:
            created = datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        source_name = meta.get("source_name") if isinstance(meta.get("source_name"), str) else ""
        preview = meta.get("jd_preview") if isinstance(meta.get("jd_preview"), str) else ""
        resume_id = meta.get("resume_id") if isinstance(meta.get("resume_id"), str) else ""
        items.append({
            "id": folder.name,
            "created_at": created,
            "source_name": source_name,
            "resume_id": resume_id,
            "jd_preview": preview,
            "state": state,
            "ok": ok,
            "download": download,
            "file": filename,
        })
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:50]


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


def _finish_run(jd: str, snapshot: Path, run_dir: Path, resume_id: str) -> None:
    try:
        try:
            result = _execute_run(jd, snapshot, run_dir)
            result["resume_id"] = resume_id
        except Exception as exc:
            result = {
                "ok": False, "error": str(exc), "download": None,
                "run_id": run_dir.name, "resume_id": resume_id,
            }
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
    return {
        "ok": False,
        "error": (
            "There is no built-in base resume to restore. "
            "Use the CLI: --reset --resume PATH."
        ),
    }


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
            status, body, ctype = _json_bytes({"ok": True, "base": None})
            self._send(status, body, ctype)
            return
        if path == "/api/library":
            status, body, ctype = _json_bytes({"resumes": _list_resumes()})
            self._send(status, body, ctype)
            return
        if path == "/api/runs":
            status, body, ctype = _json_bytes({"runs": _list_runs()})
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
                try:
                    filename, data, library_id, original_name = _selection_from_payload(payload)
                except ValueError as exc:
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                if not _TAILOR_LOCK.acquire(blocking=False):
                    self._send(*_json_bytes({"ok": False, "error": "Another resume is being processed. Wait for it to finish, then run again."}, 409))
                    return
                try:
                    if library_id is None:
                        library_id = _store_library_resume(filename, data, original_name)
                    run_dir = _new_run_dir()
                    snapshot = _snapshot_into_run(run_dir, filename, data)
                    _write_run_meta(run_dir, source_name=original_name, resume_id=library_id, jd=payload["jd"])
                    write_text(run_dir / "status.json", json.dumps({"state": "working", "message": "Preparing the source resume"}))
                    worker = threading.Thread(
                        target=_finish_run, args=(payload["jd"], snapshot, run_dir, library_id), daemon=True,
                    )
                    worker.start()
                except Exception as exc:
                    _TAILOR_LOCK.release()
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                self._send(*_json_bytes({
                    "ok": True, "run_id": run_dir.name,
                    "status": f"/api/status/{run_dir.name}", "resume_id": library_id,
                }, 202))
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
