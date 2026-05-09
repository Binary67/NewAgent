from __future__ import annotations

import asyncio
import json
import os
import signal
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
_job_process: asyncio.subprocess.Process | None = None
_job_stdin: asyncio.StreamWriter | None = None
_job_stop_requested = False
_waiting_for_input = False
_pending_input: dict[str, str] | None = None
_next_event_id = 0
_MAX_EVENTS = 1000
_STOP_TIMEOUT_SECONDS = 5
_INPUT_PROMPTS = (
    "Answer (press Enter to use recommendation):",
    "Target repo path:",
)


def _read_config_text() -> str:
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text(encoding="utf-8")
    return CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")


def _is_job_running() -> bool:
    return _job_task is not None and not _job_task.done()


async def _append_event(kind: str, message: str, job_name: str | None = None, **fields: object) -> None:
    global _next_event_id

    async with _events_changed:
        _next_event_id += 1
        event = {
            "id": _next_event_id,
            "kind": kind,
            "message": message,
            "job": job_name if job_name is not None else _job_name,
            "running": _is_job_running(),
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        }
        event.update(fields)
        _events.append(event)
        if len(_events) > _MAX_EVENTS:
            del _events[: len(_events) - _MAX_EVENTS]
        _events_changed.notify_all()


def _format_sse(event: dict[str, object]) -> str:
    return f"id: {event['id']}\ndata: {json.dumps(event)}\n\n"


def _current_pending_input_event() -> dict[str, object] | None:
    if not _waiting_for_input or _pending_input is None:
        return None

    event = {
        "id": _next_event_id,
        "kind": "input_request",
        "message": "Waiting for browser input.",
        "job": _job_name,
        "running": _is_job_running(),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    event.update(_pending_input)
    return event


def _matches_current_pending_input(event: dict[str, object]) -> bool:
    if event.get("kind") != "input_request" or _pending_input is None:
        return False
    return all(event.get(key) == value for key, value in _pending_input.items())


def _find_input_prompt(text: str) -> str:
    prompt_positions = [
        (text.rfind(prompt), prompt)
        for prompt in _INPUT_PROMPTS
        if text.rfind(prompt) != -1
    ]
    if not prompt_positions:
        return ""
    return max(prompt_positions, key=lambda item: item[0])[1]


def _last_prefixed_line(text: str, prefix: str) -> str:
    for line in reversed(text.splitlines()):
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""


def _build_input_request(prompt: str, recent_output: str) -> dict[str, str]:
    return {
        "prompt": prompt,
        "question": _last_prefixed_line(recent_output, "Question:"),
        "recommendation": _last_prefixed_line(recent_output, "Recommendation:"),
    }


async def _mark_waiting_for_input(job_name: str, prompt: str, recent_output: str) -> None:
    global _pending_input, _waiting_for_input

    input_request = _build_input_request(prompt, recent_output)
    async with _job_lock:
        _pending_input = input_request
        _waiting_for_input = True
    await _append_event("input_request", "Waiting for browser input.", job_name, **input_request)


async def _read_process_output(process: asyncio.subprocess.Process, job_name: str) -> None:
    if process.stdout is None:
        return

    output_buffer = ""
    recent_output = ""
    prompt_reported = False

    while True:
        chunk = await process.stdout.read(256)
        if not chunk:
            break

        text = chunk.decode("utf-8", errors="replace")
        output_buffer += text
        recent_output = (recent_output + text)[-8000:]

        while "\n" in output_buffer:
            line, output_buffer = output_buffer.split("\n", 1)
            await _append_event("log", line.rstrip("\r"), job_name)
            prompt_reported = False

        prompt = _find_input_prompt(output_buffer)
        if prompt and not prompt_reported:
            await _append_event("log", output_buffer, job_name)
            await _mark_waiting_for_input(job_name, prompt, recent_output)
            output_buffer = ""
            prompt_reported = True

    if output_buffer:
        await _append_event("log", output_buffer.rstrip("\r\n"), job_name)


async def _run_script(job_name: str, script_path: Path) -> None:
    global _job_name, _job_process, _job_stdin, _job_task, _job_stop_requested, _pending_input, _waiting_for_input

    command = [sys.executable, "-u", str(script_path)]

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(PROJECT_ROOT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        async with _job_lock:
            _job_process = process
            _job_stdin = process.stdin
        await _append_event("status", f"Starting {job_name}.", job_name)
        await _append_event("command", f"$ {' '.join(command)}", job_name)
        await _read_process_output(process, job_name)

        exit_code = await process.wait()
        if _job_stop_requested:
            await _append_event("status", f"{job_name} stopped by user.", job_name)
        elif exit_code == 0:
            await _append_event("status", f"{job_name} finished with exit code 0.", job_name)
        else:
            await _append_event("error", f"{job_name} finished with exit code {exit_code}.", job_name)
    except Exception as exc:
        await _append_event("error", f"{job_name} failed: {exc}", job_name)
    finally:
        async with _job_lock:
            if asyncio.current_task() is _job_task:
                _job_name = None
                _job_process = None
                _job_stdin = None
                _job_task = None
                _job_stop_requested = False
                _pending_input = None
                _waiting_for_input = False
        await _append_event("status", "Backend is idle.", job_name)


async def _launch_job(job_name: str, script_path: Path) -> JSONResponse:
    global _job_name, _job_stop_requested, _job_task

    async with _job_lock:
        if _is_job_running():
            return JSONResponse(
                {"ok": False, "message": f"{_job_name} is already running."},
                status_code=409,
            )
        _job_name = job_name
        _job_stop_requested = False
        _job_task = asyncio.create_task(_run_script(job_name, script_path))

    return JSONResponse({"ok": True, "message": f"{job_name} started."})


async def _stop_experiment_job() -> JSONResponse:
    global _job_stop_requested, _pending_input, _waiting_for_input

    async with _job_lock:
        if not _is_job_running():
            return JSONResponse(
                {"ok": False, "message": "No backend job is running."},
                status_code=409,
            )
        if _job_name != "experiment":
            return JSONResponse(
                {"ok": False, "message": f"{_job_name} is running and cannot be stopped here."},
                status_code=409,
            )
        if _job_process is None:
            return JSONResponse(
                {"ok": False, "message": "Experiment process is not ready yet."},
                status_code=409,
            )

        process = _job_process
        job_name = _job_name
        _job_stop_requested = True
        _pending_input = None
        _waiting_for_input = False

    await _append_event("status", "Stop requested for experiment.", job_name)

    if process.returncode is not None:
        await _append_event("status", "Experiment process already stopped.", job_name)
        return JSONResponse({"ok": True, "message": "Experiment already stopped."})

    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            await _append_event("status", "Experiment process terminated.", job_name)
        except asyncio.TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            await _append_event("status", "Experiment process killed after stop timeout.", job_name)
    except ProcessLookupError:
        await _append_event("status", "Experiment process already stopped.", job_name)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": f"Failed to stop experiment: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ok": True, "message": "Experiment stop requested."})


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


@app.post("/jobs/stop")
async def stop_experiment() -> JSONResponse:
    return await _stop_experiment_job()


@app.post("/jobs/input")
async def submit_job_input(request: Request) -> JSONResponse:
    global _pending_input, _waiting_for_input

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        payload = {}

    response = str(payload.get("response", "")) if isinstance(payload, dict) else ""
    async with _job_lock:
        if not _waiting_for_input or _job_stdin is None or not _is_job_running():
            return JSONResponse(
                {"ok": False, "message": "No backend input is pending."},
                status_code=409,
            )
        stdin = _job_stdin
        job_name = _job_name
        _pending_input = None
        _waiting_for_input = False

    try:
        stdin.write(f"{response}\n".encode("utf-8"))
        await stdin.drain()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": f"Failed to submit input: {exc}"},
            status_code=500,
        )

    await _append_event("input_submitted", "Browser input submitted.", job_name)
    return JSONResponse({"ok": True, "message": "Input submitted."})


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    async def stream():
        last_event_id = 0
        sent_current_input = False

        while not await request.is_disconnected():
            heartbeat = False
            pending_input_event = None
            async with _events_changed:
                new_events = [event for event in _events if int(event["id"]) > last_event_id]
                if not new_events and not sent_current_input:
                    pending_input_event = _current_pending_input_event()
                if not new_events and pending_input_event is None:
                    try:
                        await asyncio.wait_for(_events_changed.wait(), timeout=15)
                    except asyncio.TimeoutError:
                        heartbeat = True
                    else:
                        continue

            if heartbeat:
                yield ": keep-alive\n\n"
                continue

            for event in new_events or [pending_input_event]:
                if event is None:
                    continue
                last_event_id = max(last_event_id, int(event["id"]))
                if _matches_current_pending_input(event):
                    sent_current_input = True
                yield _format_sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream")
