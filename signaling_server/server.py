"""
StreamShare Signaling Server (Fase 4)
Servidor HTTP mínimo para troca de sessões entre Host e Viewer pela internet.

Deploy: Railway, Render, Fly.io (plano gratuito funciona bem — tráfego é mínimo).

Endpoints:
  POST   /session             — Host registra sessão
  GET    /session/{code}      — Viewer busca sessão
  DELETE /session/{code}      — Host remove sessão ao encerrar
  POST   /punch/{code}/host   — Host registra endpoint público para hole-punch
  POST   /punch/{code}/viewer — Viewer registra endpoint público para hole-punch
  GET    /punch/{code}        — Ambos os lados buscam o endpoint do outro (long-poll até 15s)
  GET    /health              — healthcheck

Sessões expiram automaticamente após SESSION_TTL_SECONDS sem heartbeat.
Entradas de punch expiram após PUNCH_TTL_SECONDS.
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

# TTL para entradas de hole-punch: 30s é mais que suficiente para a negociação.
PUNCH_TTL_SECONDS = int(os.environ.get("PUNCH_TTL", "30"))

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

# Hole-punch rendezvous: {normalized_code: {host: {ip, port, expires_at}, viewer: {...}, event: threading.Event}}
_punch: dict[str, dict[str, Any]] = {}
_punch_lock = threading.Lock()


def _normalize(code: str) -> str:
    return code.upper().replace(" ", "").replace("-", "")


def _cleanup_expired():
    """Remove sessões expiradas. Chamado a cada requisição write."""
    now = time.time()
    expired = [k for k, v in _sessions.items() if v["expires_at"] < now]
    for k in expired:
        del _sessions[k]


def _cleanup_punch_expired():
    """Remove entradas de punch expiradas."""
    now = time.time()
    expired = []
    for k, v in _punch.items():
        host_exp = v.get("host", {}).get("expires_at", 0)
        viewer_exp = v.get("viewer", {}).get("expires_at", 0)
        # Remove se ambos expiraram ou se a entrada tem mais de 2× TTL sem nenhum lado
        oldest = max(host_exp, viewer_exp)
        if oldest > 0 and oldest < now:
            expired.append(k)
    for k in expired:
        del _punch[k]


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


class PunchRegister(BaseModel):
    ip:   str = Field(..., min_length=7)
    port: int = Field(..., ge=1024, le=65535)


class PunchPeer(BaseModel):
    ip:   str
    port: int


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


# ---------------------------------------------------------------------------
# Hole-punch rendezvous endpoints
# ---------------------------------------------------------------------------

def _get_or_create_punch(key: str) -> dict:
    """Retorna a entrada de punch para o código, criando se necessário."""
    if key not in _punch:
        _punch[key] = {"host": None, "viewer": None, "event": threading.Event()}
    return _punch[key]


@app.post("/punch/{code}/host", response_model=PunchPeer, status_code=200)
def punch_register_host(code: str, body: PunchRegister):
    """
    Host registra seu endpoint público para hole-punch.
    Retorna o endpoint do Viewer imediatamente se já estiver disponível,
    senão aguarda até 15s (long-poll).
    """
    key = _normalize(code)
    deadline = time.time() + 15.0

    with _punch_lock:
        _cleanup_punch_expired()
        entry = _get_or_create_punch(key)
        entry["host"] = {"ip": body.ip, "port": body.port, "expires_at": time.time() + PUNCH_TTL_SECONDS}
        event: threading.Event = entry["event"]
        # Se viewer já registrou, retorna imediatamente
        if entry["viewer"]:
            viewer = entry["viewer"]
            return PunchPeer(ip=viewer["ip"], port=viewer["port"])
        event.clear()

    # Aguarda o viewer registrar (long-poll fora do lock)
    remaining = deadline - time.time()
    event.wait(timeout=max(0.0, remaining))

    with _punch_lock:
        entry = _punch.get(key)
        if entry and entry.get("viewer"):
            v = entry["viewer"]
            return PunchPeer(ip=v["ip"], port=v["port"])

    raise HTTPException(status_code=408, detail="Viewer não conectou dentro do tempo limite.")


@app.post("/punch/{code}/viewer", response_model=PunchPeer, status_code=200)
def punch_register_viewer(code: str, body: PunchRegister):
    """
    Viewer registra seu endpoint público para hole-punch.
    Retorna o endpoint do Host imediatamente se já estiver disponível,
    senão aguarda até 15s (long-poll).
    """
    key = _normalize(code)
    deadline = time.time() + 15.0

    with _punch_lock:
        _cleanup_punch_expired()
        entry = _get_or_create_punch(key)
        entry["viewer"] = {"ip": body.ip, "port": body.port, "expires_at": time.time() + PUNCH_TTL_SECONDS}
        event: threading.Event = entry["event"]
        # Se host já registrou, notifica e retorna imediatamente
        if entry["host"]:
            host = entry["host"]
            event.set()
            return PunchPeer(ip=host["ip"], port=host["port"])
        event.clear()

    # Aguarda o host registrar (long-poll fora do lock)
    remaining = deadline - time.time()
    event.wait(timeout=max(0.0, remaining))

    with _punch_lock:
        entry = _punch.get(key)
        if entry and entry.get("host"):
            h = entry["host"]
            return PunchPeer(ip=h["ip"], port=h["port"])

    raise HTTPException(status_code=408, detail="Host não encontrado dentro do tempo limite.")


@app.delete("/punch/{code}")
def punch_cleanup(code: str):
    """Remove entrada de punch após negociação concluída."""
    key = _normalize(code)
    with _punch_lock:
        _punch.pop(key, None)
    return {"ok": True}
