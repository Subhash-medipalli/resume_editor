"""Run with an installed wheel's Python, outside the repository import path."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

from resume_tailor import server


def main():
    assert "site-packages" in str(Path(server.__file__).resolve()), server.__file__
    assert server.ROOT == Path.cwd(), "installed runs must use the working folder"
    assert (server.STATIC_DIR / "index.html").is_file()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        conn = HTTPConnection("127.0.0.1", httpd.server_port)
        conn.request("GET", "/")
        response = conn.getresponse()
        assert response.status == 200
        assert b"Tailor resume" in response.read()
        conn.close()
    finally:
        httpd.shutdown(); httpd.server_close(); worker.join()
    print("Installed wheel serves the resume UI successfully.")


if __name__ == "__main__":
    main()
