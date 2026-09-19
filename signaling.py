"""
StreamShare - Cliente de Sinalização (Fase 4)
Registra e busca sessões em um servidor de sinalização HTTP para conexões pela internet.

Fluxo:
  Host:   register_session(code, public_ip, video_port, host_name)
  Viewer: lookup_session(code) -> (host_ip, video_port, host_name)

O servidor padrão é o endpoint público do StreamShare. Pode ser sobrescrito por variável
de ambiente STREAMSHARE_SIGNAL_URL para quem quiser hospedar o próprio servidor.
"""
import os
import threading
import time

import requests

# URL do servidor de sinalização. Sobrescreva com STREAMSHARE_SIGNAL_URL no ambiente.
DEFAULT_SIGNAL_URL = os.environ.get(
    "STREAMSHARE_SIGNAL_URL",
    "https://streamshare-signal.onrender.com",
)

_REQUEST_TIMEOUT = 8.0   # segundos por requisição HTTP
_HEARTBEAT_INTERVAL = 20  # segundos entre heartbeats do host


class SignalingError(Exception):
    """Erro de comunicação com o servidor de sinalização."""


def register_session(
    session_code: str,
    public_ip: str,
    video_port: int,
    host_name: str,
    signal_url: str = DEFAULT_SIGNAL_URL,
) -> None:
    """
    Registra a sessão do Host no servidor de sinalização.
    Lança SignalingError em caso de falha.
    """
    url = f"{signal_url.rstrip('/')}/session"
    payload = {
        "code":       session_code,
        "public_ip":  public_ip,
        "video_port": video_port,
        "host_name":  host_name,
    }
    try:
        resp = requests.post(url, json=payload, timeout=_REQUEST_TIMEOUT)
        if resp.status_code not in (200, 201):
            raise SignalingError(f"Servidor retornou {resp.status_code}: {resp.text[:200]}")
    except requests.RequestException as e:
        raise SignalingError(f"Falha ao registrar sessão: {e}") from e


def lookup_session(
    session_code: str,
    signal_url: str = DEFAULT_SIGNAL_URL,
) -> tuple[str, int, str]:
    """
    Busca uma sessão no servidor de sinalização.
    Retorna (host_ip, video_port, host_name).
    Lança SignalingError se não encontrada ou em caso de erro de rede.
    """
    url = f"{signal_url.rstrip('/')}/session/{session_code}"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 404:
            raise SignalingError(
                f"Código '{session_code}' não encontrado.\n"
                "Verifique se o Host está transmitindo e conectado à internet."
            )
        if resp.status_code != 200:
            raise SignalingError(f"Servidor retornou {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        return data["public_ip"], int(data["video_port"]), data["host_name"]
    except requests.RequestException as e:
        raise SignalingError(f"Falha ao buscar sessão: {e}") from e


def unregister_session(
    session_code: str,
    signal_url: str = DEFAULT_SIGNAL_URL,
) -> None:
    """Remove a sessão do servidor de sinalização ao encerrar."""
    url = f"{signal_url.rstrip('/')}/session/{session_code}"
    try:
        requests.delete(url, timeout=_REQUEST_TIMEOUT)
    except Exception:
        pass  # melhor esforço — não bloqueia o encerramento


class SignalingHeartbeat:
    """
    Thread de heartbeat: reenvia o registro periodicamente para manter a sessão
    viva no servidor (que expira sessões inativas após TTL).
    """

    def __init__(
        self,
        session_code: str,
        public_ip: str,
        video_port: int,
        host_name: str,
        signal_url: str = DEFAULT_SIGNAL_URL,
        interval: float = _HEARTBEAT_INTERVAL,
    ):
        self._code = session_code
        self._ip = public_ip
        self._port = video_port
        self._name = host_name
        self._url = signal_url
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        unregister_session(self._code, self._url)

    def _run(self):
        while not self._stop.wait(timeout=self._interval):
            try:
                register_session(self._code, self._ip, self._port, self._name, self._url)
            except SignalingError as e:
                print(f"[signaling] heartbeat falhou: {e}")
