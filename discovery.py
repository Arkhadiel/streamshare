"""
StreamShare - Módulo de Descoberta na LAN (Fase 2)
Protocolo de anúncio, descoberta e solicitação de conexão via UDP Broadcast na LAN com nomes customizáveis.
"""
import re
import secrets
import socket
import struct
import sys
import threading
import time

DISCOVERY_PORT = 5556

MAGIC_ANNOUNCE = b"SSD1"  # StreamShare Discovery Announce v1 (4 bytes)
ANNOUNCE_FORMAT = "!4s8sH32s"  # magic(4) code(8) video_port(2) host_name(32) = 46 bytes
ANNOUNCE_SIZE = struct.calcsize(ANNOUNCE_FORMAT)

MAGIC_CONNECT = b"SSCR"  # StreamShare Connect Request v1 (4 bytes)
CONNECT_FORMAT = "!4s8s32sH"  # magic(4) code(8) viewer_name(32) viewer_video_port(2) = 46 bytes
CONNECT_SIZE = struct.calcsize(CONNECT_FORMAT)

# Pool de caracteres amigáveis (sem 0, O, 1, I, L)
CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def generate_session_code() -> str:
    """
    Gera um código de sessão no formato XXXX-XXXX usando caracteres não ambíguos.
    Exemplo: "K89P-M37X"
    """
    part1 = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
    part2 = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
    return f"{part1}-{part2}"


def normalize_code(code: str) -> str:
    """Remove hífens, espaços e converte para maiúsculas."""
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()


def _encode_name_32bytes(name: str) -> bytes:
    """Codifica nome em UTF-8 truncando de forma segura para no máximo 32 bytes e preenchendo com \\x00."""
    if not name or not name.strip():
        name = "Anônimo"
    b_name = name.strip().encode("utf-8")
    if len(b_name) > 32:
        b_name = b_name[:32]
        while b_name:
            try:
                b_name.decode("utf-8")
                break
            except UnicodeDecodeError:
                b_name = b_name[:-1]
    return b_name.ljust(32, b"\x00")


def _decode_name_32bytes(b_name: bytes) -> str:
    """Decodifica nome UTF-8 removendo o preenchimento \\x00."""
    s = b_name.rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    return s if s else "Anônimo"


def pack_discovery_packet(code: str, video_port: int, host_name: str = "Anônimo") -> bytes:
    """Monta o pacote binário de anúncio UDP do Host."""
    norm_code = normalize_code(code)
    if len(norm_code) != 8:
        raise ValueError(f"Código inválido para empacotamento: {code}")
    b_name = _encode_name_32bytes(host_name)
    return struct.pack(ANNOUNCE_FORMAT, MAGIC_ANNOUNCE, norm_code.encode("ascii"), video_port, b_name)


def pack_connect_request(code: str, viewer_name: str = "Anônimo", viewer_video_port: int = 5555) -> bytes:
    """Monta o pacote binário de solicitação de conexão (Connect Request) do Viewer."""
    norm_code = normalize_code(code)
    if len(norm_code) != 8:
        raise ValueError(f"Código inválido para empacotamento: {code}")
    b_name = _encode_name_32bytes(viewer_name)
    return struct.pack(CONNECT_FORMAT, MAGIC_CONNECT, norm_code.encode("ascii"), b_name, viewer_video_port)


def unpack_discovery_packet(data: bytes):
    """Desempacota um pacote de anúncio ou solicitação de conexão."""
    if len(data) >= ANNOUNCE_SIZE and data[:4] == MAGIC_ANNOUNCE:
        try:
            magic, code_bytes, video_port, name_bytes = struct.unpack(ANNOUNCE_FORMAT, data[:ANNOUNCE_SIZE])
            return {
                "type": "announce",
                "code": code_bytes.decode("ascii", errors="replace"),
                "video_port": video_port,
                "name": _decode_name_32bytes(name_bytes),
            }
        except Exception:
            return None
    elif len(data) >= CONNECT_SIZE and data[:4] == MAGIC_CONNECT:
        try:
            magic, code_bytes, name_bytes, viewer_video_port = struct.unpack(CONNECT_FORMAT, data[:CONNECT_SIZE])
            return {
                "type": "connect",
                "code": code_bytes.decode("ascii", errors="replace"),
                "name": _decode_name_32bytes(name_bytes),
                "video_port": viewer_video_port,
            }
        except Exception:
            return None
    return None


class DiscoveryBroadcaster:
    """
    Thread do Host que:
    1. Anuncia periodicamente o código de sessão e seu nome na porta de descoberta (5556).
    2. Escuta solicitações de conexão (Connect Request) enviadas pelos Viewers.
    3. Quando um Connect Request válido é recebido, invoca on_viewer_connected(viewer_ip, viewer_name).
    """

    def __init__(
        self,
        session_code: str,
        video_port: int = 5555,
        discovery_port: int = DISCOVERY_PORT,
        host_name: str = "Anônimo",
        on_viewer_connected=None,
    ):
        self.session_code = session_code
        self.norm_code = normalize_code(session_code)
        self.video_port = video_port
        self.discovery_port = discovery_port
        self.host_name = host_name if host_name and host_name.strip() else "Anônimo"
        self.on_viewer_connected = on_viewer_connected
        self.stop_event = threading.Event()
        self.connected_viewer_ip = None
        self.connected_viewer_name = None
        self._thread = None
        self.accepted_ips = set()
        self._accepted_lock = threading.Lock()

    def start(self):
        self.stop_event.clear()
        with self._accepted_lock:
            self.accepted_ips.clear()
        self.connected_viewer_ip = None
        self.connected_viewer_name = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _run(self):
        packet = pack_discovery_packet(self.session_code, self.video_port, self.host_name)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass

        try:
            sock.bind(("0.0.0.0", self.discovery_port))
        except Exception as e:
            print(f"[discovery] erro ao dar bind na porta {self.discovery_port}: {e}", file=sys.stderr)

        sock.settimeout(0.5)
        last_announce = 0.0
        broadcast_addrs = [("255.255.255.255", self.discovery_port), ("<broadcast>", self.discovery_port)]

        while not self.stop_event.is_set():
            now = time.time()
            # Transmite anúncio broadcast a cada ~1.0s
            if now - last_announce >= 1.0:
                for addr in broadcast_addrs:
                    try:
                        sock.sendto(packet, addr)
                    except Exception:
                        pass
                last_announce = now

            # Escuta pedidos de conexão (Connect Request) dos Viewers
            try:
                data, addr = sock.recvfrom(1024)
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                break

            info = unpack_discovery_packet(data)
            if info and info.get("type") == "connect":
                if normalize_code(info["code"]) == self.norm_code:
                    viewer_ip = addr[0]
                    viewer_name = info.get("name", "Anônimo")
                    viewer_video_port = info.get("video_port", 5555)
                    target_key = (viewer_ip, viewer_video_port)
                    with self._accepted_lock:
                        if target_key in self.accepted_ips:
                            continue
                        self.accepted_ips.add(target_key)
                    self.connected_viewer_ip = viewer_ip
                    self.connected_viewer_name = viewer_name
                    print(f"[discovery] Connect Request aceito de {viewer_name} ({viewer_ip}:{viewer_video_port})")
                    if self.on_viewer_connected:
                        try:
                            self.on_viewer_connected(viewer_ip, viewer_name, viewer_video_port)
                        except Exception as e:
                            print(f"[discovery] erro na callback de conexão: {e}", file=sys.stderr)

        sock.close()


def scan_for_session(
    target_code: str,
    viewer_name: str = "Anônimo",
    viewer_video_port: int = 5555,
    timeout: float = 15.0,
    discovery_port: int = DISCOVERY_PORT,
    stop_event: threading.Event = None,
) -> tuple[str, int, str]:
    """
    Viewer escuta por pacotes de broadcast do Host na porta de descoberta (5556).
    Ao encontrar o Host correspondente:
    1. Extrai o nome do Host da mensagem de anúncio.
    2. Envia um pacote de Connect Request (SSCR) contendo o nome do Viewer e sua porta de vídeo para o IP do Host na porta 5556.
    3. Retorna (host_ip, video_port, host_name).
    """
    norm_target = normalize_code(target_code)
    if len(norm_target) != 8:
        raise ValueError("Código de sessão deve conter 8 caracteres (formato: XXXX-XXXX)")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass

    try:
        sock.bind(("0.0.0.0", discovery_port))
    except Exception as e:
        sock.close()
        raise RuntimeError(f"Não foi possível abrir porta de descoberta {discovery_port}: {e}")

    sock.settimeout(0.5)
    deadline = time.time() + timeout

    try:
        while time.time() < deadline:
            if stop_event and stop_event.is_set():
                break

            try:
                data, addr = sock.recvfrom(1024)
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                break

            info = unpack_discovery_packet(data)
            if info and info.get("type") == "announce":
                received_code = normalize_code(info["code"])
                if received_code == norm_target:
                    host_ip = addr[0]
                    video_port = info["video_port"]
                    host_name = info.get("name", "Anônimo")

                    # Envia Connect Request contendo o nome e a porta de vídeo do Viewer para o Host
                    connect_pkt = pack_connect_request(norm_target, viewer_name, viewer_video_port)
                    try:
                        sock.sendto(connect_pkt, (host_ip, discovery_port))
                        print(f"[discovery] Connect Request ({viewer_name}, porta {viewer_video_port}) enviado para {host_ip}:{discovery_port}")
                    except Exception as e:
                        print(f"[discovery] erro ao enviar Connect Request: {e}", file=sys.stderr)

                    return host_ip, video_port, host_name
    finally:
        sock.close()

    raise TimeoutError(
        f"Código '{target_code}' não encontrado na rede local dentro de {timeout:.0f}s.\n"
        "Verifique se o Host está transmitindo na mesma rede Wi-Fi/LAN."
    )


# ---------------------------------------------------------------------------
# Fase 4 — Modo Internet (Sinalização HTTP + STUN)
# ---------------------------------------------------------------------------

class InternetHostSession:
    """
    Gerencia o lado Host para conexões pela internet:
    1. Usa STUN para obter IP público e porta mapeada pelo NAT.
    2. Registra a sessão no servidor de sinalização com heartbeat periódico.
    3. Executa UDP hole-punch com o Viewer via servidor de rendezvous.
    4. Escuta UDP na porta de vídeo para receber o Connect Request do Viewer.
    """

    def __init__(
        self,
        session_code: str,
        video_port: int,
        host_name: str = "Anônimo",
        on_viewer_connected=None,
        signal_url: str | None = None,
    ):
        self.session_code = session_code
        self.video_port = video_port
        self.host_name = host_name if host_name and host_name.strip() else "Anônimo"
        self.on_viewer_connected = on_viewer_connected
        self.signal_url = signal_url
        self.stop_event = threading.Event()
        self.public_ip: str | None = None
        self.public_port: int | None = None
        self._heartbeat = None
        self._thread: threading.Thread | None = None
        self.accepted_ips: set = set()
        self._accepted_lock = threading.Lock()

    def start(self) -> tuple[str, int]:
        """
        Inicia o registro no servidor de sinalização.
        Retorna (public_ip, public_port) para exibir na UI.
        Lança RuntimeError se STUN falhar.
        """
        from stun_client import get_public_address
        from signaling import SignalingHeartbeat, register_session, SignalingError

        addr = get_public_address(local_port=self.video_port, timeout=6.0)
        if not addr:
            raise RuntimeError(
                "Não foi possível detectar o IP público via STUN.\n"
                "Verifique sua conexão com a internet."
            )
        self.public_ip, self.public_port = addr
        print(f"[internet] IP público detectado: {self.public_ip}:{self.public_port}")

        kwargs = dict(
            session_code=self.session_code,
            public_ip=self.public_ip,
            video_port=self.video_port,
            host_name=self.host_name,
        )
        if self.signal_url:
            kwargs["signal_url"] = self.signal_url

        try:
            register_session(**kwargs)
        except SignalingError as e:
            raise RuntimeError(str(e)) from e

        hb_kwargs = dict(
            session_code=self.session_code,
            public_ip=self.public_ip,
            video_port=self.video_port,
            host_name=self.host_name,
        )
        if self.signal_url:
            hb_kwargs["signal_url"] = self.signal_url
        self._heartbeat = SignalingHeartbeat(**hb_kwargs)
        self._heartbeat.start()

        self.stop_event.clear()
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

        return self.public_ip, self.public_port

    def stop(self):
        self.stop_event.set()
        if self._heartbeat:
            self._heartbeat.stop()
            self._heartbeat = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _listen_loop(self):
        """
        Escuta pacotes UDP na porta de vídeo:
        - Pacotes SSPUNCH1: responde com o mesmo para confirmar hole-punch bidirecional.
        - Connect Requests (SSCR): processa conexão do Viewer.
        """
        from hole_punch import PUNCH_MAGIC

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception:
            pass
        try:
            sock.bind(("0.0.0.0", self.video_port))
        except Exception as e:
            print(f"[internet] erro ao dar bind na porta {self.video_port}: {e}", file=sys.stderr)
            return
        sock.settimeout(0.5)
        norm_code = normalize_code(self.session_code)

        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(256)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break

            # Responde a pacotes de hole-punch para confirmar bidirecionalidade
            if data == PUNCH_MAGIC:
                try:
                    sock.sendto(PUNCH_MAGIC, addr)
                except OSError:
                    pass
                continue

            info = unpack_connect_request(data)
            if not info:
                continue
            received_code = normalize_code(info["code"])
            if received_code != norm_code:
                continue

            viewer_ip = addr[0]
            viewer_name = info.get("name", "Anônimo")
            viewer_video_port = info.get("viewer_video_port", 5555)

            with self._accepted_lock:
                already = viewer_ip in self.accepted_ips
                if not already:
                    self.accepted_ips.add(viewer_ip)

            if not already:
                print(f"[internet] Connect Request aceito de {viewer_name} ({viewer_ip}:{viewer_video_port})")
                if self.on_viewer_connected:
                    self.on_viewer_connected(viewer_ip, viewer_name, viewer_video_port)

        sock.close()


def scan_for_session_internet(
    target_code: str,
    viewer_name: str = "Anônimo",
    viewer_video_port: int = 5555,
    stop_event: threading.Event | None = None,
    signal_url: str | None = None,
) -> tuple[str, int, str]:
    """
    Modo internet: busca a sessão no servidor de sinalização e envia um
    Connect Request UDP diretamente ao Host pelo IP público.
    Retorna (host_ip, video_port, host_name).
    Lança SignalingError ou RuntimeError em caso de falha.
    """
    from signaling import lookup_session, SignalingError

    if stop_event and stop_event.is_set():
        raise InterruptedError("Busca cancelada.")

    kwargs = {"session_code": target_code}
    if signal_url:
        kwargs["signal_url"] = signal_url

    try:
        host_ip, video_port, host_name = lookup_session(target_code, **({"signal_url": signal_url} if signal_url else {}))
    except SignalingError as e:
        raise RuntimeError(str(e)) from e

    if stop_event and stop_event.is_set():
        raise InterruptedError("Busca cancelada.")

    # Tenta hole-punch; se falhar, tenta conexao direta (funciona para NAT full-cone)
    try:
        from hole_punch import punch_as_viewer, HolePunchFailed
        punch_kwargs = dict(
            session_code=target_code,
            local_video_port=viewer_video_port,
        )
        if signal_url:
            punch_kwargs["signal_url"] = signal_url
        punched_host_ip, punched_host_port = punch_as_viewer(**punch_kwargs)
        print(f"[internet] hole-punch bem-sucedido: {punched_host_ip}:{punched_host_port}")
        # Usa o endpoint confirmado pelo punch (pode diferir do video_port reportado via signaling
        # em NATs que mapeiam portas diferentes)
        host_ip = punched_host_ip
        video_port = punched_host_port
    except Exception as punch_err:
        print(f"[internet] hole-punch falhou ({punch_err}), tentando conexao direta...", file=sys.stderr)

    # Envia Connect Request UDP ao Host
    connect_pkt = pack_connect_request(normalize_code(target_code), viewer_name, viewer_video_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(connect_pkt, (host_ip, video_port))
        print(f"[internet] Connect Request enviado para {host_ip}:{video_port}")
    except Exception as e:
        print(f"[internet] erro ao enviar Connect Request: {e}", file=sys.stderr)
    finally:
        sock.close()

    return host_ip, video_port, host_name
