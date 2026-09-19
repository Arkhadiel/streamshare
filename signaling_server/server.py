"""
StreamShare Signaling Server (Fase 4)
Servidor HTTP mínimo para troca de sessões entre Host e Viewer pela internet.

Deploy: Railway, Render, Fly.io (plano gratuito funciona bem — tráfego é mínimo).

Endpoints:
  POST   /session          — Host registra sessão
  GET    /session/{code}   — Viewer busca sessão
  DELETE /session/{code}   — Host remove sessão ao encerrar
  GET    /health           — healthcheck

Sessões expiram automaticamente após SESSION_TTL_SECONDS sem heartbeat.
"""
import os
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# TTL padrão: 60 segundos. Host envia heartbeat a cada ~20s, então há folga suficiente.
SESSION_TTL_SECONDS = int(os.environ.get("SESSION_TTL", "60"))

app = FastAPI(title="StreamShare Signaling", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Armazenamento em memória: {normalized_code: {public_ip, video_port, host_name, expires_at}}
_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def _normalize(code: str) -> str:
    return code.upper().replace(" ", "").replace("-", "")


def _cleanup_expired():
    """Remove sessões expiradas. Chamado a cada requisição write."""
    now = time.time()
    expired = [k for k, v in _sessions.items() if v["expires_at"] < now]
    for k in expired:
        del _sessions[k]


# --- Schemas ---

class SessionRegister(BaseModel):
    code:       str = Field(..., min_length=8, max_length=9)
    public_ip:  str = Field(..., min_length=7)
    video_port: int = Field(..., ge=1024, le=65535)
    host_name:  str = Field(default="Anônimo", max_length=32)


class SessionInfo(BaseModel):
    public_ip:  str
    video_port: int
    host_name:  str


# --- Endpoints ---

@app.get("/health")
def health():
    with _lock:
        count = len(_sessions)
    return {"status": "ok", "active_sessions": count}


@app.post("/session", status_code=201)
def register_session(body: SessionRegister):
    key = _normalize(body.code)
    with _lock:
        _cleanup_expired()
        _sessions[key] = {
            "public_ip":  body.public_ip,
            "video_port": body.video_port,
            "host_name":  body.host_name,
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
    return {"ok": True}


@app.get("/session/{code}", response_model=SessionInfo)
def lookup_session(code: str):
    key = _normalize(code)
    with _lock:
        session = _sessions.get(key)
        if session and session["expires_at"] < time.time():
            del _sessions[key]
            session = None
    if not session:
        raise HTTPException(status_code=404, detail="Sessão não encontrada ou expirada.")
    return SessionInfo(
        public_ip=session["public_ip"],
        video_port=session["video_port"],
        host_name=session["host_name"],
    )


@app.delete("/session/{code}")
def unregister_session(code: str):
    key = _normalize(code)
    with _lock:
        _sessions.pop(key, None)
    return {"ok": True}
