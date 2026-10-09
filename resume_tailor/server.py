"""Tiny localhost UI so a JD can be pasted in the browser."""

from __future__ import annotations

import base64
import json
import hashlib
import io
import os
import re
import threading
import zipfile
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

from resume_tailor.files import new_run_dir, save_copy, write_text
from resume_tailor.pipeline import run_tailoring, validate_job_description
from resume_tailor.llm import DEFAULT_BASE_URL, DEFAULT_MODEL, complete, load_dotenv

STATIC_DIR = Path(__file__).resolve().parent / "static"
ROOT = Path.cwd()

# ponytail: one active provider run for this local app; return busy instead of
# silently queuing. Use a bounded queue if concurrent users become necessary.
_TAILOR_LOCK = threading.Lock()
# Readers must not see completion until the server can accept another run.
_RUN_STATUS_LOCK = threading.Lock()
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



_MISSING_BASE = (
    "Attach a .docx base resume, or choose a previously uploaded one."
)


def _library_root() -> Path:
    return ROOT / "out" / "library" / "resumes"


def _safe_docx_name(name: str) -> str:
    cleaned = Path(name or "attached.docx").name
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


def _decode_upload(payload: dict) -> tuple[str, str, bytes]:
    b64 = payload.get("resume_b64", "")
    if not isinstance(b64, str):
        raise ValueError("resume_b64 must be a base64 string.")
    name = payload.get("resume_name", "attached.docx")
    if not isinstance(name, str):
        raise ValueError("resume_name must be a string.")
    original = Path(name or "attached.docx").name
    safe = _safe_docx_name(original)
    b64 = b64.strip()
    if "," in b64 and b64.lower().startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not decode attached resume: {exc}") from exc
    _validate_docx_bytes(data)
    return original, safe, data


def _store_library_resume(filename: str, data: bytes, original_name: str) -> str:
    # Attaching the same file again reuses its entry instead of adding a copy.
    digest, now = hashlib.sha256(data).hexdigest(), datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in _list_resumes():
        if item["sha256"] == digest and item["original_name"] == Path(original_name).name:
            meta_path = _library_root() / item["id"] / "meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            write_text(meta_path, json.dumps({**meta, "saved_at": now}))
            return item["id"]
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
        "saved_at": now,
        "bytes": len(data),
    }))
    return resume_id


def _read_library_resume(resume_id: str) -> tuple[dict, bytes]:
    if not re.fullmatch(r"[0-9a-f]{32}", resume_id):
        raise ValueError("Unknown saved resume.")
    library = _library_root().resolve()
    entry = library / resume_id
    folder = entry.resolve()
    if entry.is_symlink() or folder.parent != library:
        raise ValueError("Unknown saved resume.")
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Unknown saved resume.") from exc
    if not isinstance(meta, dict) or meta.get("id") != resume_id:
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
    b64 = payload.get("resume_b64", "")
    if not isinstance(b64, str):
        raise ValueError("resume_b64 must be a base64 string.")
    if b64.strip():
        original, filename, data = _decode_upload(payload)
        return filename, data, None, original
    resume_id = payload.get("resume_id", "")
    if not isinstance(resume_id, str):
        raise ValueError("resume_id must be a string.")
    resume_id = resume_id.strip()
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
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
            continue
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if not isinstance(meta, dict) or meta.get("id") != folder.name:
            continue
        filename = meta.get("filename")
        if (not isinstance(filename, str) or Path(filename).name != filename
                or not filename.lower().endswith(".docx")
                or (folder / filename).is_symlink() or not (folder / filename).is_file()):
            continue
        try:
            digest = hashlib.sha256((folder / filename).read_bytes()).hexdigest()
        except OSError:
            continue
        items.append({
            "id": folder.name,
            "sha256": digest,
            "filename": meta.get("filename") if isinstance(meta.get("filename"), str) else "",
            "original_name": meta.get("original_name") if isinstance(meta.get("original_name"), str) else meta.get("filename"),
            "saved_at": meta.get("saved_at") if isinstance(meta.get("saved_at"), str) else "",
            "bytes": meta.get("bytes") if isinstance(meta.get("bytes"), int) else None,
        })
    items.sort(key=lambda item: item["saved_at"], reverse=True)
    # Older uploads of an identical file under the same name add nothing to the list.
    unique, seen = [], set()
    for item in items:
        key = (item["original_name"], item["sha256"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:50]


def _list_runs() -> list[dict]:
    root = ROOT / "out" / "runs"
    if not root.is_dir():
        return []
    items = []
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r"[0-9a-f]{32}", folder.name):
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
            status = _read_run_status(folder.name)
            state = status.get("state") if status.get("state") in ("working", "complete") else "unknown"
            if state == "complete" and isinstance(status.get("result"), dict):
                ok = bool(status["result"].get("ok"))
        except (OSError, ValueError):
            pass
        download, filename = None, None
        try:
            result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            if isinstance(result, dict) and result.get("status") == "ready":
                name = result.get("file")
                if (isinstance(name, str) and Path(name).name == name and name.endswith(".docx")
                        and not (folder / name).is_symlink() and (folder / name).is_file()):
                    filename = name
                    download = f"/api/download/{folder.name}"
        except (OSError, ValueError):
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


def _execute_run(
    job_description: str,
    resume_path: Path,
    out_dir: Path,
    *,
    two_pass: bool = False,
) -> dict:
    load_dotenv(ROOT / ".env")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "LLM_API_KEY is missing. Put it in .env in the project folder."}
    result = run_tailoring(
        job_description=job_description, resume_path=resume_path, out_dir=out_dir,
        api_key=api_key, base_url=os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
        complete_fn=complete,
        progress=lambda message: _write_progress(out_dir, message),
        two_pass=two_pass,
    )
    if result["ok"]:
        result["download"] = f"/api/download/{out_dir.name}"
        _save_copy(result, out_dir)
    return result


def _save_copy(result: dict, run_dir: Path) -> None:
    """Also write the verified Word file into RESUME_SAVE_DIR, when that is set.

    A browser download goes through the browser's own save dialog, which a script (or
    an AI driving the page) cannot answer; this copy needs no browser. The folder comes
    only from the server's environment, never from a request. A failure is a warning:
    the run's verified download is unaffected and the page falls back to it.
    """
    configured = os.environ.get("RESUME_SAVE_DIR", "").strip()
    if not configured:
        return
    try:
        # load_dotenv expands nothing, so `$HOME/Downloads` arrives literally; an unset variable
        # would otherwise become a folder inside the repo, which git does not ignore.
        expanded = os.path.expandvars(configured)
        if "$" in expanded:
            raise ValueError("it uses a variable that is not set")
        folder = ROOT / Path(expanded).expanduser()  # RuntimeError: "~user" that does not exist
        manifest, data = _read_result(run_dir.name)  # rechecks the SHA-256 first
        result["saved_to"] = str(save_copy(data, folder, manifest["file"]))
    except (OSError, ValueError, RuntimeError) as exc:
        result.setdefault("warnings", []).append(f"Could not save a copy to {configured}: {exc}")


def _write_progress(run_dir: Path, message: str) -> None:
    # Progress text is cosmetic: a failed write must never fail a paid model run.
    try:
        with _RUN_STATUS_LOCK:
            write_text(run_dir / "status.json", json.dumps({"state": "working", "message": message}))
    except OSError:
        pass


def _finish_run(
    jd: str,
    snapshot: Path,
    run_dir: Path,
    resume_id: str,
    two_pass: bool = False,
) -> None:
    global _ACTIVE_RUN_ID
    status = None
    try:
        try:
            result = _execute_run(jd, snapshot, run_dir, two_pass=two_pass)
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "download": None, "run_id": run_dir.name}
        result["resume_id"] = resume_id
        result["run_id"] = run_dir.name
        status = json.dumps({"state": "complete", "result": result})
    finally:
        with _RUN_STATUS_LOCK:
            try:
                if status is not None:
                    write_text(run_dir / "status.json", status)
            finally:
                _ACTIVE_RUN_ID = None
                _TAILOR_LOCK.release()


def _read_run_status(run_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise ValueError("Unknown run.")
    with _RUN_STATUS_LOCK:
        status = json.loads((ROOT / "out/runs" / run_id / "status.json").read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            raise ValueError("Invalid run status.")
        if status.get("state") == "working" and run_id != _ACTIVE_RUN_ID:
            return {"state": "complete", "result": {
                "ok": False, "download": None, "run_id": run_id,
                "error": "This run was interrupted. Start a new tailoring run.",
            }}
        return status


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
                status = _read_run_status(run_id)
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
                    filename, data, library_id, original_name = _selection_from_payload(payload)
                    if library_id is None:
                        library_id = _store_library_resume(filename, data, original_name)
                    run_dir = _new_run_dir()
                    snapshot = _snapshot_into_run(run_dir, filename, data)
                    _write_run_meta(run_dir, source_name=original_name, resume_id=library_id, jd=payload["jd"])
                    _ACTIVE_RUN_ID = run_dir.name
                    write_text(run_dir / "status.json", json.dumps({"state": "working", "message": "Preparing the source resume"}))
                    worker = threading.Thread(
                        target=_finish_run,
                        args=(payload["jd"], snapshot, run_dir, library_id, two_pass),
                        daemon=True,
                    )
                    worker.start()
                except Exception as exc:
                    _ACTIVE_RUN_ID = None
                    _TAILOR_LOCK.release()
                    self._send(*_json_bytes({"ok": False, "error": str(exc)}, 400))
                    return
                self._send(*_json_bytes({"ok": True, "run_id": run_dir.name,
                                        "status": f"/api/status/{run_dir.name}", "resume_id": library_id}, 202))
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
    with ThreadingHTTPServer((host, port), Handler) as httpd:
        print(f"Resume tailor UI: http://{host}:{httpd.server_port}")
        print("Paste a job description in the browser. Ctrl-C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nResume tailor stopped.")
