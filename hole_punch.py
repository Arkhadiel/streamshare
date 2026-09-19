"""
StreamShare - UDP Hole Punching (Fase 4)
Implementa hole-punching simultâneo via servidor de rendezvous.

Fluxo:
  1. Ambos os lados descobrem seu IP:porta público via STUN.
  2. Ambos registram seu endpoint no servidor de sinalização (/punch/{code}/host ou /punch/{code}/viewer).
     O servidor faz long-poll e retorna o endpoint do outro lado quando ambos estão presentes.
  3. Ambos disparam pacotes UDP simultaneamente ao endpoint do outro → abre os mappings NAT.
  4. Se o punch falhar após PUNCH_TIMEOUT segundos, levanta HolePunchFailed para que o
     chamador possa fazer fallback para TURN.

Uso:
    # Host
    peer_ip, peer_port = punch_as_host(session_code, local_video_port)

    # Viewer
    peer_ip, peer_port = punch_as_viewer(session_code, local_video_port)
"""
import socket
import threading
import time

import requests

from stun_client import get_public_address

# URL do servidor de sinalização (mesma variável usada em signaling.py)
import os
DEFAULT_SIGNAL_URL = os.environ.get(
    "STREAMSHARE_SIGNAL_URL",
    "https://streamshare-zj00.onrender.com",
)

# Quantos segundos enviar pacotes de punch antes de desistir
PUNCH_TIMEOUT = 8.0
# Intervalo entre pacotes de punch (ms → s)
PUNCH_INTERVAL = 0.15
# Payload mágico dos pacotes de hole-punch (não é vídeo)
PUNCH_MAGIC = b"SSPUNCH1"
# Timeout HTTP para chamadas ao servidor (inclui long-poll de até 15s no servidor)
HTTP_TIMEOUT = 20.0


class HolePunchFailed(Exception):
    """Disparado quando o hole-punch não consegue estabelecer comunicação bidirecional."""


def _register_punch(role: str, code: str, local_port: int, signal_url: str) -> tuple[str, int]:
    """
    Registra o endpoint local no servidor de sinalização e aguarda o endpoint do par.

    Args:
        role: "host" ou "viewer"
        code: código de sessão normalizado (8 chars, sem hífen)
        local_port: porta UDP local usada para vídeo
        signal_url: URL base do servidor de sinalização

    Returns:
        (peer_ip, peer_port) — endpoint público do par

    Raises:
        HolePunchFailed se STUN ou o servidor falharem
    """
    # 1. Descobre IP:porta públicos via STUN usando a porta de vídeo real
    pub = get_public_address(local_port=local_port, timeout=6.0)
    if not pub:
        raise HolePunchFailed("STUN falhou — não foi possível determinar endpoint público.")
    my_ip, my_port = pub

    print(f"[punch] endpoint público detectado: {my_ip}:{my_port} (porta local {local_port})")

    # 2. Registra no servidor e aguarda o par (long-poll no servidor)
    url = f"{signal_url.rstrip('/')}/punch/{code}/{role}"
    try:
        resp = requests.post(
            url,
            json={"ip": my_ip, "port": my_port},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise HolePunchFailed(f"Falha ao registrar punch no servidor: {e}") from e

    if resp.status_code == 408:
        raise HolePunchFailed("Par não conectou dentro do tempo limite (408).")
    if resp.status_code != 200:
        raise HolePunchFailed(f"Servidor retornou {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    peer_ip = data["ip"]
    peer_port = int(data["port"])
    print(f"[punch] endpoint do par recebido: {peer_ip}:{peer_port}")
    return peer_ip, peer_port


def _do_punch(sock: socket.socket, peer_ip: str, peer_port: int, stop: threading.Event) -> bool:
    """
    Envia pacotes PUNCH_MAGIC periodicamente ao par e escuta respostas.
    Retorna True se receber um pacote de punch de volta (comunicação bidirecional confirmada).
    """
    received = threading.Event()

    def sender():
        while not stop.is_set() and not received.is_set():
            try:
                sock.sendto(PUNCH_MAGIC, (peer_ip, peer_port))
            except OSError:
                break
            time.sleep(PUNCH_INTERVAL)

    def receiver():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(64)
                if data == PUNCH_MAGIC and addr[0] == peer_ip:
                    print(f"[punch] punch bidirecional confirmado com {addr[0]}:{addr[1]}")
                    received.set()
                    return
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break

    t_send = threading.Thread(target=sender, daemon=True)
    t_recv = threading.Thread(target=receiver, daemon=True)
    t_send.start()
    t_recv.start()

    received.wait(timeout=PUNCH_TIMEOUT)
    stop.set()
    t_send.join(timeout=1.0)
    t_recv.join(timeout=1.0)
    return received.is_set()


def punch_as_host(
    session_code: str,
    local_video_port: int,
    signal_url: str = DEFAULT_SIGNAL_URL,
) -> tuple[str, int]:
    """
    Executa hole-punch como Host.

    Args:
        session_code: código de sessão (com ou sem hífen)
        local_video_port: porta UDP onde o host está ouvindo vídeo
        signal_url: URL do servidor de sinalização

    Returns:
        (viewer_ip, viewer_port) — endpoint público do viewer após punch bem-sucedido

    Raises:
        HolePunchFailed se o punch falhar
    """
    from discovery import normalize_code
    code = normalize_code(session_code)

    peer_ip, peer_port = _register_punch("host", code, local_video_port, signal_url)

    # Abre socket na porta de vídeo e executa punch simultâneo
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    try:
        sock.bind(("0.0.0.0", local_video_port))
    except OSError:
        # Porta já está em uso pela HostSession — ok, o punch usa o mesmo socket
        # Nesse caso não fazemos bind e usamos porta efêmera para o punch
        sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)

    stop = threading.Event()
    try:
        success = _do_punch(sock, peer_ip, peer_port, stop)
    finally:
        sock.close()

    if not success:
        raise HolePunchFailed(
            f"Hole-punch com {peer_ip}:{peer_port} falhou após {PUNCH_TIMEOUT:.0f}s. "
            "NAT simétrico detectado — TURN necessário."
        )

    return peer_ip, peer_port


def punch_as_viewer(
    session_code: str,
    local_video_port: int,
    signal_url: str = DEFAULT_SIGNAL_URL,
) -> tuple[str, int]:
    """
    Executa hole-punch como Viewer.

    Args:
        session_code: código de sessão (com ou sem hífen)
        local_video_port: porta UDP local do viewer para receber vídeo
        signal_url: URL do servidor de sinalização

    Returns:
        (host_ip, host_port) — endpoint público do host após punch bem-sucedido

    Raises:
        HolePunchFailed se o punch falhar
    """
    from discovery import normalize_code
    code = normalize_code(session_code)

    peer_ip, peer_port = _register_punch("viewer", code, local_video_port, signal_url)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)
    try:
        sock.bind(("0.0.0.0", local_video_port))
    except OSError:
        sock.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.3)

    stop = threading.Event()
    try:
        success = _do_punch(sock, peer_ip, peer_port, stop)
    finally:
        sock.close()

    if not success:
        raise HolePunchFailed(
            f"Hole-punch com {peer_ip}:{peer_port} falhou após {PUNCH_TIMEOUT:.0f}s. "
            "NAT simétrico detectado — TURN necessário."
        )

    return peer_ip, peer_port
