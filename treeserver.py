import asyncio
import json
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Permetti richieste dal tuo sito GitHub Pages
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # puoi restringere al dominio GitHub Pages
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Identificatore del PC
CLIENT_ID = "lorenzo_pc"

# WebSocket attivo del PC
connected_clients: Dict[str, WebSocket] = {}

# Future per risposte in attesa
pending_responses: Dict[str, asyncio.Future] = {}


@app.websocket("/ws/client")
async def client_ws(ws: WebSocket):
    """
    Connessione dal PC locale.
    Il PC resta connesso e riceve task dal server.
    """
    await ws.accept()
    connected_clients[CLIENT_ID] = ws
    print("PC connesso")

    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)

            # Risposta dal PC
            if data.get("type") == "result":
                future = pending_responses.get(CLIENT_ID)
                if future and not future.done():
                    future.set_result(data)

    except WebSocketDisconnect:
        print("PC disconnesso")

    finally:
        connected_clients.pop(CLIENT_ID, None)
        future = pending_responses.get(CLIENT_ID)
        if future and not future.done():
            future.set_exception(RuntimeError("PC disconnesso durante l'elaborazione"))


@app.post("/api/chat")
async def chat(payload: dict):
    """
    Endpoint chiamato dal sito.
    Inoltra il prompt al PC tramite WebSocket e attende la risposta.
    """
    prompt = payload.get("prompt")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt mancante")

    ws = connected_clients.get(CLIENT_ID)
    if not ws:
        raise HTTPException(status_code=503, detail="PC offline")

    # Future per la risposta
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    pending_responses[CLIENT_ID] = future

    # Invia task al PC
    await ws.send_text(json.dumps({
        "type": "task",
        "prompt": prompt,
    }))

    try:
        # Attende la risposta dal PC
        result = await asyncio.wait_for(future, timeout=120)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout risposta dal PC")
    finally:
        pending_responses.pop(CLIENT_ID, None)

    return {
        "response": result.get("response", "")
    }
