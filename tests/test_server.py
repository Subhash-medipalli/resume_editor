import base64
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
import threading
import time
import zipfile

from docx import Document
import pytest

from resume_tailor import server
from resume_tailor.llm import LLMError
from tests.helpers import pack_model_output


def resume_bytes(name="Sample Candidate"):
    document = Document()
    for text, style in [(name, "Normal"), ("sample@example.invalid", "Normal"),
                        ("PROFESSIONAL SUMMARY:", "Normal"), ("Built Python services.", "List Bullet"),
                        ("Maintained Python services.", "List Bullet"),
                        ("TECHNICAL SKILLS:", "Normal"), ("Tools: Python", "Normal")]:
        document.add_paragraph(text, style)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def reply(messages, **kwargs):
    base = messages[1]["content"].split("\n## Resume to tailor ", 1)[1].split("\n\n", 1)[1]
    return pack_model_output(changelog=["Reworded one bullet"], match="SCORE: 70\npartial: Test reply",
                             resume=base.replace("Built Python services.", "Built reliable Python services."))


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(server, "complete", reply)
    monkeypatch.setenv("LLM_API_KEY", "fake-no-network")
    (tmp_path / "resume").mkdir()
    (tmp_path / "resume/candidate.docx").write_bytes(resume_bytes())
    monkeypatch.chdir(tmp_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()

    def raw_request(method, path, payload=None, *, headers=None, raw_body=None):
        conn = HTTPConnection("127.0.0.1", httpd.server_port, timeout=10)
        try:
            conn.request(method, path, body=raw_body if raw_body is not None else json.dumps(payload) if payload is not None else None,
                         headers={"Content-Type": "application/json", **(headers or {})})
            res = conn.getresponse()
            body = res.read()
            if res.getheader("Content-Type", "").startswith("application/json"):
                body = json.loads(body)
            return res.status, body
        finally:
            conn.close()
    def request(method, path, payload=None):
        status, body = raw_request(method, path, payload)
        if status == 202:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status, progress = raw_request("GET", body["status"])
                if progress["state"] == "complete":
                    return status, progress["result"]
                time.sleep(0.01)
            pytest.fail("run did not finish")
        return status, body
    request.raw = raw_request
    request.port = httpd.server_port
    yield request
    httpd.shutdown()
    httpd.server_close()
    worker.join()


def test_downloads_are_immediate_and_stay_bound_to_their_run(api, monkeypatch):
    status, first = api("POST", "/api/tailor", {"jd": "Job A"})
    assert status == 200 and first["ok"]
    assert first["download"] and first["review_items"]
    first_url = f"/api/download/{first['run_id']}"
    assert first["download"] == first_url
    status, first_bytes = api("GET", first_url)
    assert status == 200
    _, second = api("POST", "/api/tailor", {"jd": "Job B"})
    assert second["run_id"] != first["run_id"]
    assert second["download"] != first_url
    assert api("GET", first_url) == (200, first_bytes)
    monkeypatch.setattr(server, "complete", lambda *a, **k: (_ for _ in ()).throw(LLMError("provider failed")))
    status, failed = api("POST", "/api/tailor", {"jd": "Job C"})
    assert status == 200 and not failed["ok"] and not failed.get("download")
    assert api("GET", "/api/download")[0] == 404
    assert api("GET", first_url) == (200, first_bytes)


def test_busy_request_is_explicit_and_same_name_uploads_remain_separate(api, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def held_reply(messages, **kwargs):
        if "Candidate One" in messages[1]["content"]:
            started.set()
            assert release.wait(5)
        return reply(messages, **kwargs)
    monkeypatch.setattr(server, "complete", held_reply)
    def run(name):
        return api("POST", "/api/tailor", {"jd": "Python", "resume_name": "resume.docx",
                   "resume_b64": base64.b64encode(resume_bytes(name)).decode()})
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(run, "Candidate One")
        try:
            assert started.wait(5)
            status, busy = run("Candidate Two")
            assert status == 409 and "being processed" in busy["error"]
        finally:
            release.set()
        first_result = first.result()
    second_result = run("Candidate Two")
    for name, (status, result) in zip(("Candidate One", "Candidate Two"), (first_result, second_result)):
        assert status == 200 and result["ok"], result
        status, data = api("GET", result["download"])
        assert status == 200
        document = Document(io.BytesIO(data))
        assert document.paragraphs[0].text == name
        assert any("Built reliable" in p.text for p in document.paragraphs)
    assert first_result[1]["run_id"] != second_result[1]["run_id"]


def test_default_source_is_snapshotted_before_model_work(api, monkeypatch, tmp_path):
    source = tmp_path / "resume/candidate.docx"
    def change_source(messages, **kwargs):
        source.write_bytes(resume_bytes("Updated Candidate"))
        return reply(messages, **kwargs)
    monkeypatch.setattr(server, "complete", change_source)
    _, result = api("POST", "/api/tailor", {"jd": "Python"})
    _, data = api("GET", result["download"])
    assert Document(io.BytesIO(data)).paragraphs[0].text == "Sample Candidate"
    assert Document(source).paragraphs[0].text == "Updated Candidate"


@pytest.mark.parametrize("failure", ["guardrail", "verification"])
def test_failed_run_has_no_downloadable_artifact(api, monkeypatch, tmp_path, failure):
    if failure == "guardrail":
        def bad_reply(messages, **kwargs):
            return reply(messages, **kwargs) + "\n## EDUCATION\n- Master of Science\n"
        monkeypatch.setattr(server, "complete", bad_reply)
    else:
        monkeypatch.setattr("resume_tailor.docx_io.verify_written_docx", lambda *a: ["synthetic mismatch"])
    status, result = api("POST", "/api/tailor", {"jd": "Python"})
    assert status == 200 and not result["ok"]
    assert not result["download"]
    run_dir = tmp_path / "out/runs" / result["run_id"]
    assert not (run_dir / "result.json").exists()
    assert not list(run_dir.glob("*.docx"))
    assert api("GET", f"/api/download/{result['run_id']}")[0] == 404


def test_changed_result_cannot_be_downloaded(api, tmp_path):
    _, result = api("POST", "/api/tailor", {"jd": "Python"})
    url = result["download"]
    run_dir = tmp_path / "out/runs" / result["run_id"]
    next(run_dir.glob("*.docx")).write_bytes(b"changed after verification")
    assert api("GET", url)[0] == 404


@pytest.mark.parametrize(
    "payload",
    [["not an object"], {"jd": 123}, {"jd": ""}, {"jd": "Python", "two_pass": "yes"}],
)
def test_invalid_request_is_rejected_before_allocating_a_run(api, tmp_path, payload):
    assert api("POST", "/api/tailor", payload)[0] == 400
    assert not (tmp_path / "out/runs").exists()


def test_async_status_shows_provider_progress_and_completion(api, monkeypatch):
    waiting, release = threading.Event(), threading.Event()
    def with_progress(messages, **kwargs):
        kwargs["progress"]("Provider unavailable — retrying in 5 seconds")
        waiting.set()
        assert release.wait(5)
        return reply(messages, **kwargs)
    monkeypatch.setattr(server, "complete", with_progress)
    status, started = api.raw("POST", "/api/tailor", {"jd": "Python"})
    try:
        assert status == 202
        assert waiting.wait(5)
        status, progress = api.raw("GET", started["status"])
        assert status == 200 and progress["state"] == "working"
        assert "retrying" in progress["message"]
        assert api.raw("GET", "/api/download/" + started["run_id"])[0] == 404
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        _, progress = api.raw("GET", started["status"])
        if progress["state"] == "complete":
            break
        time.sleep(0.01)
    assert progress["result"]["ok"] and progress["result"]["download"]


def test_oversized_job_description_rejected_before_run_allocation(api, tmp_path):
    status, result = api.raw("POST", "/api/tailor", {"jd": "x" * 60001})
    assert status == 400 and "too long" in result["error"]
    assert not (tmp_path / "out/runs").exists()


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.example"},
    {"Host": ""},
    {"Host": "localhost:not-a-port"},
    {"Origin": "http://localhost:1"},
    {"Origin": "https://attacker.example"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_origin_and_invalid_hosts_are_rejected(api, headers):
    assert api.raw("GET", "/api/health", headers=headers)[0] == 403
    assert api.raw("POST", "/api/tailor", {"jd": "Python"}, headers=headers)[0] == 403


def test_same_origin_request_is_allowed(api):
    status, result = api.raw("GET", "/api/health", headers={"Origin": f"http://127.0.0.1:{api.port}"})
    assert status == 200 and result["ok"]


def test_invalid_utf8_is_a_client_error(api):
    assert api.raw("POST", "/api/tailor", raw_body=b"\xff")[0] == 400


@pytest.mark.parametrize("attachment", [
    {"resume_b64": ["not", "text"]},
    {"resume_b64": "not base64!"},
    {"resume_b64": base64.b64encode(b"PK" + b"x" * 150).decode()},
    {"resume_b64": base64.b64encode(b"PK" + b"x" * 150).decode(), "resume_name": 123},
])
def test_invalid_uploads_never_start_a_model_request(api, monkeypatch, attachment):
    monkeypatch.setattr(server, "complete", lambda *a, **k: pytest.fail("Invalid upload reached model"))
    status, result = api.raw("POST", "/api/tailor", {"jd": "Python", **attachment})
    assert status == 400 and not result["ok"]
    assert not server._TAILOR_LOCK.locked()


def test_upload_expansion_is_bounded_before_word_parsing(api, monkeypatch):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "x")
        archive.writestr("word/document.xml", b"x" * 25_000_001)
    monkeypatch.setattr("docx.Document", lambda *a, **k: pytest.fail("Oversized archive reached Word parser"))
    status, result = api.raw("POST", "/api/tailor", {"jd": "Python", "resume_b64": base64.b64encode(stream.getvalue()).decode()})
    assert status == 400 and "expands" in result["error"]


def test_stale_working_run_reports_interruption(api, tmp_path):
    run_id = "a" * 32
    directory = tmp_path / "out/runs" / run_id
    directory.mkdir(parents=True)
    (directory / "status.json").write_text(json.dumps({"state": "working", "message": "Old process"}))
    status, result = api.raw("GET", "/api/status/" + run_id)
    assert status == 200 and result["state"] == "complete"
    assert not result["result"]["ok"] and "interrupted" in result["result"]["error"]


def test_unicode_result_filename_downloads(api, tmp_path):
    source = tmp_path / "resume/candidate.docx"
    source.rename(source.with_name("Candidate — résumé.docx"))
    _, result = api("POST", "/api/tailor", {"jd": "Python"})
    assert result["ok"] and result["resume_path"].endswith("/Candidate — résumé.docx")
    status, document = api("GET", result["download"])
    assert status == 200 and Document(io.BytesIO(document)).paragraphs[0].text == "Sample Candidate"


def test_uppercase_docx_upload_can_download(api):
    status, result = api("POST", "/api/tailor", {"jd": "Python", "resume_name": "Candidate.DOCX",
                         "resume_b64": base64.b64encode(resume_bytes("Upper Candidate")).decode()})
    assert status == 200 and result["ok"], result
    status, data = api("GET", result["download"])
    assert status == 200 and Document(io.BytesIO(data)).paragraphs[0].text == "Upper Candidate"


def test_remote_server_binding_is_rejected():
    with pytest.raises(ValueError, match="local app"):
        server.serve(host="0.0.0.0")


def test_model_echo_never_creates_a_successful_download(api, monkeypatch, tmp_path):
    calls = []
    def echo(messages, **kwargs):
        calls.append(messages)
        original = messages[1]["content"].split("\n## Resume to tailor ", 1)[1].split("\n\n", 1)[1]
        return pack_model_output(changelog=["No changes needed"], match="SCORE: 99\ngood: Perfect match", resume=original)
    monkeypatch.setattr(server, "complete", echo)
    status, result = api("POST", "/api/tailor", {"jd": "Python"})
    assert status == 200 and not result["ok"] and not result.get("download")
    assert len(calls) <= 2
    assert not list((tmp_path / "out/runs").glob("*/result.json"))
    assert not list((tmp_path / "out/runs").glob("*/*.docx"))
