"""Regression: the Flask mobile app is served through Starlette's WSGIMiddleware, which
needs the anyio.from_thread / anyio.to_thread submodules loaded. Prod (anyio 4.15,
starlette 1.6) returned 500 "module 'anyio' has no attribute 'from_thread'" on every
mobile route because nothing imported them under uvicorn (2026-09-03/04).

Must run in a CLEAN interpreter: pytest/TestClient import anyio.from_thread themselves
and would mask the bug, so the request is driven by hand in a subprocess.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SCRIPT = textwrap.dedent("""
    import sys, anyio
    from src.main import app

    async def main():
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                 "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "",
                 "headers": [(b"host", b"dev3.localhost:8000")],
                 "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000)}
        out = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(msg):
            out.append(msg)
        await app(scope, receive, send)
        status = next(m["status"] for m in out if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
        print("STATUS", status)
        print("BODY", body.decode(errors="replace")[:200])
        print("FROM_THREAD", "anyio.from_thread" in sys.modules)
        print("TO_THREAD", "anyio.to_thread" in sys.modules)

    anyio.run(main)
""")


def test_mobile_home_route_served_via_wsgi_bridge():
    env = dict(os.environ, PYTHONPATH=str(REPO))
    proc = subprocess.run([sys.executable, "-c", SCRIPT], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=300)
    out = proc.stdout
    assert proc.returncode == 0, f"subprocess failed:\n{proc.stderr[-2000:]}"
    assert "STATUS 200" in out, out + proc.stderr[-1500:]
    assert "Attendance Server Running" in out, out
    assert "FROM_THREAD True" in out, out
    assert "TO_THREAD True" in out, out
