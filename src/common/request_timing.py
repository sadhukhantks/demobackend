"""Per-request timing: server time vs database time, logged and returned as headers.

Owner request 2026-10-12 ("check the query load times and the server processing
times separately"). For every request whose path starts with one of the configured
prefixes this logs one line

    TIMING GET /api/longAbsenteeism/get?co_id=2&case_id=9 200 total=1234.5ms db=1180.2ms queries=14

and adds the response headers ``X-Server-Time-Ms`` (whole handler, including
JSON encoding), ``X-DB-Time-Ms`` (sum of the SQL round trips made while serving
it) and ``X-DB-Queries`` (how many). The difference between the two times is the
Python side: snapshot shaping, template merge, PDF build, waiting for a pool
connection.

How it works: a ContextVar carries one accumulator per request; SQLAlchemy's
``before_cursor_execute`` / ``after_cursor_execute`` events on the Engine class
add each statement's wall time to it. Starlette copies the context into the
threadpool that runs sync handlers, so the accumulator is visible there. Requests
outside the prefixes have no accumulator and cost one dict lookup per statement.

The middleware itself is async only because Starlette requires it (no DB work
happens here); handlers stay plain ``def`` as CLAUDE.md requires.
"""

import contextvars
import logging
import time
from typing import Iterable

from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger("request_timing")

_acc: contextvars.ContextVar[dict | None] = contextvars.ContextVar("request_timing_acc", default=None)


def _before(conn, cursor, statement, parameters, context, executemany):
    acc = _acc.get()
    if acc is not None:
        conn.info["_rt_t0"] = time.perf_counter()


def _after(conn, cursor, statement, parameters, context, executemany):
    acc = _acc.get()
    if acc is None:
        return
    t0 = conn.info.pop("_rt_t0", None)
    if t0 is not None:
        acc["db"] += time.perf_counter() - t0
        acc["n"] += 1


def install(app, prefixes: Iterable[str]) -> None:
    """Register the SQLAlchemy listeners once and the HTTP middleware on ``app``."""
    prefixes = tuple(prefixes)
    if not getattr(install, "_listening", False):
        event.listen(Engine, "before_cursor_execute", _before)
        event.listen(Engine, "after_cursor_execute", _after)
        install._listening = True  # type: ignore[attr-defined]

    @app.middleware("http")
    async def request_timing_middleware(request, call_next):
        path = request.url.path
        if not path.startswith(prefixes):
            return await call_next(request)
        acc = {"db": 0.0, "n": 0}
        token = _acc.set(acc)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            _acc.reset(token)
        total_ms = (time.perf_counter() - t0) * 1000
        db_ms = acc["db"] * 1000
        response.headers["X-Server-Time-Ms"] = f"{total_ms:.1f}"
        response.headers["X-DB-Time-Ms"] = f"{db_ms:.1f}"
        response.headers["X-DB-Queries"] = str(acc["n"])
        q = request.url.query
        logger.info(
            "TIMING %s %s%s %s total=%.1fms db=%.1fms queries=%d",
            request.method, path, f"?{q}" if q else "", response.status_code, total_ms, db_ms, acc["n"],
        )
        return response
