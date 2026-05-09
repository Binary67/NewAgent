from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "CodexConfig.toml"
CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "CodexConfig.example.toml"

app = FastAPI(title="NextResearcher")
app.mount("/static", StaticFiles(directory=WEB_ROOT / "static"), name="static")
templates = Jinja2Templates(directory=WEB_ROOT / "templates")

_events: list[dict[str, object]] = []
_events_changed = asyncio.Condition()
_job_lock = asyncio.Lock()
_job_task: asyncio.Task[None] | None = None
_job_name: str | None = None
_next_event_id = 0
_MAX_EVENTS = 1000


def _read_config_text() -> str:
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text(encoding="utf-8")
    return CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")


def _is_job_running() -> bool:
    return _job_task is not None and not _job_task.done()


async def _append_event(kind: str, message: str, job_name: str | None = None) -> None:
    global _next_event_id

    async with _events_changed:
        _next_event_id += 1
        _events.append(
            {
                "id": _next_event_id,
                "kind": kind,
                "message": message,
                "job": job_name if job_name is not None else _job_name,
                "running": _is_job_running(),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
        )
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]
        _events_changed.notify_all()


def _format_sse(event: dict[str, object]) -> str:
    return f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"


async def _run_script(job_name: str, script_path: Path) -> None:
    command = [sys.executable, "-u", str(script_path)]
    await _append_event("status", f"Starting {job_name}.", job_name)
    await _append_event("command", f"$ {' '.join(command)}", job_name)

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if process.stdout is not None:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                await _append_event("log", line.decode("utf-8", errors="replace").rstrip("\r\n"), job_name)

        exit_code = await process.wait()
        if exit_code == 0:
            await _append_event("status", f"{job_name} finished with exit code 0.", job_name)
        else:
            await _append_event("error", f"{job_name} finished with exit code {exit_code}.", job_name)
    except Exception as exc:
        await _append_event("error", f"{job_name} failed: {exc}", job_name)
    finally:
        global _job_name, _job_task
        async with _job_lock:
            if asyncio.current_task() is _job_task:
                _job_name = None
                _job_task = None
        await _append_event("status", "Backend is idle.", job_name)


async def _launch_job(job_name: str, script_path: Path) -> JSONResponse:
    global _job_name, _job_task

    async with _job_lock:
        if _is_job_running():
            return JSONResponse(
                {"ok": False, "message": f"{_job_name} is already running."},
                status_code=409,
            )
        _job_name = job_name
        _job_task = asyncio.create_task(_run_script(job_name, script_path))

    return JSONResponse({"ok": True, "message": f"{job_name} started."})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "config_text": _read_config_text(),
            "config_path": CONFIG_PATH,
            "config_exists": CONFIG_PATH.exists(),
        },
    )


@app.post("/config")
async def save_config(request: Request) -> JSONResponse:
    content = (await request.body()).decode("utf-8", errors="replace")
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        return JSONResponse(
            {"ok": False, "message": f"TOML syntax error: {exc}"},
            status_code=400,
        )

    CONFIG_PATH.write_text(f"{content.rstrip()}\n", encoding="utf-8")
    await _append_event("status", f"Saved {CONFIG_PATH.name}.")
    return JSONResponse({"ok": True, "message": f"Saved {CONFIG_PATH.name}."})


@app.post("/jobs/start")
async def start_experiment() -> JSONResponse:
    return await _launch_job("experiment", PROJECT_ROOT / "Main.py")


@app.post("/jobs/reset")
async def reset_experiment() -> JSONResponse:
    return await _launch_job("reset", PROJECT_ROOT / "ResetExperiments.py")


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    async def stream():
        async with _events_changed:
            snapshot = list(_events)

        last_event_id = 0
        for event in snapshot:
            last_event_id = int(event["id"])
            yield _format_sse(event)

        while not await request.is_disconnected():
            heartbeat = False
            async with _events_changed:
                try:
                    await asyncio.wait_for(_events_changed.wait(), timeout=15)
                except asyncio.TimeoutError:
                    new_events = []
                    heartbeat = True
                else:
                    new_events = [event for event in _events if int(event["id"]) > last_event_id]

            if heartbeat:
                yield ": keep-alive\n\n"
                continue

            for event in new_events:
                last_event_id = int(event["id"])
                yield _format_sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream")
