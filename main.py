"""Vivo relay v2: bounded FIFO queue for one local inference client.

Compatibility: the old frontend can still POST /api/chat and wait for a response.
The new frontend posts {prompt, async_mode: true} and polls GET /api/chat/{id}.
One Uvicorn worker is required: queue and websocket live in this process.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Practical response deadline, not an unlimited backlog. Incoming requests are
# rejected quickly when they cannot reasonably fit inside the latency budget.
MAX_QUEUED = int(os.getenv("VIVO_MAX_QUEUED", "2"))
EXEC_TIMEOUT = min(27.0, float(os.getenv("VIVO_EXEC_TIMEOUT", "27")))
ADMISSION_LIMIT = min(26.0, float(os.getenv("VIVO_ADMISSION_LIMIT", "24")))
ESTIMATED_SECONDS = float(os.getenv("VIVO_ESTIMATED_SECONDS", "10"))
RESULT_TTL = int(os.getenv("VIVO_RESULT_TTL", "900"))
MAX_PROMPT_CHARS = int(os.getenv("VIVO_MAX_PROMPT_CHARS", "800"))
ORIGINS = [v.strip() for v in os.getenv(
    "VIVO_CORS_ORIGINS", "https://albero-project.vercel.app,http://localhost:3000"
).split(",") if v.strip()]

@dataclass
class Job:
    id: str
    prompt: str
    created_at: float = field(default_factory=time.monotonic)
    status: str = "queued"
    response: str = ""
    error: str = ""
    completed_at: float | None = None
    deadline_at: float = 0.0
    started_at: float = 0.0
    result: asyncio.Future = field(default=None, repr=False)  # type: ignore
    completion: asyncio.Future = field(default=None, repr=False)  # type: ignore

queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=MAX_QUEUED)
jobs: dict[str, Job] = {}
active_id: str | None = None
client_ws: WebSocket | None = None
worker_task: asyncio.Task | None = None
# EWMA is updated from real completed requests, so admission follows measured
# throughput rather than allowing a 24-person, many-minute queue.
avg_duration = ESTIMATED_SECONDS


def _close_job(job: Job, status: str, response: str = "", error: str = "") -> None:
    job.status = status
    job.response = response
    job.error = error
    job.completed_at = time.monotonic()
    if job.completion and not job.completion.done():
        job.completion.set_result(None)


def _predicted_seconds() -> float:
    """Expected completion time for a newly submitted request."""
    now = time.monotonic()
    current = jobs.get(active_id) if active_id else None
    elapsed = (now - current.started_at) if current else 0.0
    remaining = max(2.0, avg_duration - elapsed) if current else 0.0
    return remaining + queue.qsize() * avg_duration + avg_duration


def _cleanup() -> None:
    now = time.monotonic()
    for id_, job in list(jobs.items()):
        if job.completed_at is not None and now - job.completed_at > RESULT_TTL:
            del jobs[id_]


def _position(job_id: str) -> int:
    # Position includes the active request. 0 means now running.
    target = jobs[job_id]
    if target.status == "running":
        return 0
    if target.status != "queued":
        return 0
    older_queued = sum(
        1 for j in jobs.values()
        if j.status == "queued" and j.created_at < target.created_at
    )
    return older_queued + (1 if active_id else 0) + 1


def _view(job: Job) -> dict[str, Any]:
    data: dict[str, Any] = {"task_id": job.id, "status": job.status}
    if job.status == "queued":
        data["position"] = _position(job.id)
    elif job.status == "running":
        data["position"] = 0
    elif job.status == "done":
        data["response"] = job.response
    else:
        data["error"] = job.error or "Generazione non riuscita"
    return data


async def _worker() -> None:
    global active_id, avg_duration
    while True:
        job = await queue.get()
        active_id = job.id
        job.status = "running"
        job.started_at = time.monotonic()
        try:
            remaining = job.deadline_at - time.monotonic()
            if remaining <= 1.0:
                _close_job(job, "error", error="Tempo di attesa esaurito; riprova")
                continue
            ws = client_ws
            if ws is None:
                _close_job(job, "error", error="PC non collegato")
                continue
            await ws.send_text(json.dumps({
                "type": "task", "task_id": job.id, "prompt": job.prompt,
                "budget_seconds": max(1.0, job.deadline_at - time.monotonic())
            }, ensure_ascii=False))
            # Exactly one task is sent at a time. Legacy client results lacking an ID
            # remain unambiguous because of this serialization.
            reply = await asyncio.wait_for(
                job.result, timeout=max(.1, min(EXEC_TIMEOUT, job.deadline_at - time.monotonic()))
            )
            if time.monotonic() > job.deadline_at:
                _close_job(job, "error", error="Risposta arrivata fuori tempo massimo")
                continue
            text = reply.get("response")
            if reply.get("error"):
                _close_job(job, "error", error=str(reply["error"])[:300])
            elif not isinstance(text, str) or not text.strip():
                _close_job(job, "error", error="Risposta incompleta o vuota")
            else:
                _close_job(job, "done", response=text)
                duration = time.monotonic() - job.started_at
                avg_duration = max(4.0, min(24.0, avg_duration * .75 + duration * .25))
        except asyncio.TimeoutError:
            _close_job(job, "error", error="Tempo di elaborazione superato")
        except Exception as exc:
            print("Worker error:", repr(exc), flush=True)
            _close_job(job, "error", error="Client AI non disponibile")
        finally:
            active_id = None
            queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global worker_task
    worker_task = asyncio.create_task(_worker())
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=ORIGINS, allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["content-type"]
)


@app.get("/api/status")
async def status():
    return {
        "connected": client_ws is not None,
        "queued": queue.qsize(),
        "running": active_id is not None,
        "estimated_seconds": round(avg_duration, 1),
        "deadline_seconds": EXEC_TIMEOUT,
    }


@app.websocket("/ws/client")
async def client_connection(ws: WebSocket):
    global client_ws
    # Prevent stale websocket cleanup from removing a newer connection.
    if client_ws is not None:
        await ws.close(code=1013, reason="Un client e gia collegato")
        return
    await ws.accept()
    client_ws = ws
    print("PC connesso", flush=True)
    try:
        while True:
            message = json.loads(await ws.receive_text())
            if message.get("type") != "result":
                continue
            id_ = message.get("task_id") or active_id
            if id_ == active_id:
                job = jobs.get(id_)
                if job and job.result and not job.result.done():
                    job.result.set_result(message)
    except WebSocketDisconnect:
        print("PC disconnesso", flush=True)
    except Exception as exc:
        print("WebSocket error:", repr(exc), flush=True)
    finally:
        if client_ws is ws:
            client_ws = None
            if active_id:
                job = jobs.get(active_id)
                if job and job.result and not job.result.done():
                    job.result.set_exception(RuntimeError("Client disconnesso"))


@app.post("/api/chat")
async def chat(payload: dict):
    _cleanup()
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt mancante")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="Domanda troppo lunga")
    if client_ws is None:
        raise HTTPException(status_code=503, detail="PC offline")
    if queue.full() or queue.qsize() >= MAX_QUEUED:
        raise HTTPException(status_code=429, detail="Vivo è occupato: riprova tra pochi secondi")
    if _predicted_seconds() > ADMISSION_LIMIT:
        raise HTTPException(status_code=429, detail="Coda troppo lunga per rispondere in tempo: riprova tra poco")

    loop = asyncio.get_running_loop()
    job = Job(id=uuid.uuid4().hex, prompt=prompt.strip())
    job.deadline_at = job.created_at + EXEC_TIMEOUT
    job.result = loop.create_future()
    job.completion = loop.create_future()
    jobs[job.id] = job
    queue.put_nowait(job)

    if payload.get("async_mode") is True:
        return JSONResponse(status_code=202, content=_view(job),
                            headers={"Cache-Control": "no-store"})

    # Legacy compatibility for deployed clients that only expect {response}.
    try:
        await asyncio.wait_for(asyncio.shield(job.completion), timeout=EXEC_TIMEOUT + 1)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Attesa del relay scaduta. Usa async_mode")
    if job.status == "done":
        return {"response": job.response}
    raise HTTPException(status_code=503, detail=job.error)


@app.get("/api/chat/{task_id}")
async def get_chat(task_id: str):
    _cleanup()
    job = jobs.get(task_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Richiesta non trovata o scaduta")
    return JSONResponse(content=_view(job), headers={"Cache-Control": "no-store"})
