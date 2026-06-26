"""HTTP health/metrics endpoint — zero-dependency asyncio server."""
from __future__ import annotations

import asyncio
import json
import time

HEALTH_HOST = "127.0.0.1"
HEALTH_PORT = 9099
_started = time.time()
_turns_total = 0
_errors_total = 0
_turn_latencies: list[float] = []


def record_turn() -> None:
    global _turns_total
    _turns_total += 1


def record_error() -> None:
    global _errors_total
    _errors_total += 1


def record_latency(ms: float) -> None:
    _turn_latencies.append(ms)
    if len(_turn_latencies) > 1000:
        _turn_latencies[:500] = []


def _health_body() -> bytes:
    try:
        import os
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        mem = 0
    return json.dumps({
        "status": "ok",
        "uptime_seconds": time.time() - _started,
        "turns_total": _turns_total,
        "errors_total": _errors_total,
    }).encode()


def _metrics_body() -> bytes:
    lines = [
        "# HELP bridge_turns_total Total turns processed",
        "# TYPE bridge_turns_total counter",
        f"bridge_turns_total {_turns_total}",
        "# HELP bridge_errors_total Total turn errors",
        "# TYPE bridge_errors_total counter",
        f"bridge_errors_total {_errors_total}",
        "# HELP bridge_uptime_seconds Daemon uptime",
        "# TYPE bridge_uptime_seconds gauge",
        f"bridge_uptime_seconds {time.time() - _started:.1f}",
        "# HELP bridge_active 1 if daemon is running",
        "# TYPE bridge_active gauge",
        "bridge_active 1",
    ]
    if _turn_latencies:
        avg = sum(_turn_latencies) / len(_turn_latencies)
        lines.extend([
            "# HELP bridge_turn_latency_ms_avg Average turn latency",
            "# TYPE bridge_turn_latency_ms_avg gauge",
            f"bridge_turn_latency_ms_avg {avg:.0f}",
        ])
    return "\n".join(lines).encode() + b"\n"


async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        req = (await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)).decode().strip()
    except (asyncio.TimeoutError, ValueError):
        writer.close()
        return

    path = req.split(" ")[1] if " " in req else "/"

    # Drain headers
    try:
        while True:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 5)
            if line in (b"\r\n", b""):
                break
    except (asyncio.TimeoutError, ValueError):
        pass

    if path == "/metrics":
        body, ct = _metrics_body(), "text/plain; version=0.0.4"
    else:
        body, ct = _health_body(), "application/json"

    resp = (f"HTTP/1.1 200 OK\r\nContent-Type: {ct}\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    writer.write(resp + body)
    await writer.drain()
    writer.close()


async def start_server(host: str = HEALTH_HOST, port: int = HEALTH_PORT) -> asyncio.AbstractServer:
    return await asyncio.start_server(_handler, host, port)
