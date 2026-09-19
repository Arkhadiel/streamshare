"""
StreamShare - Cliente STUN (Fase 4)
Implementação mínima do protocolo STUN (RFC 5389) para descoberta de IP público e porta.
Não requer dependências externas além da stdlib.

Testa múltiplos servidores STUN públicos em paralelo e retorna o primeiro resultado válido.
"""
import os
import socket
import struct
import threading
import time

# Servidores STUN públicos confiáveis
STUN_SERVERS = [
    ("stun.l.google.com",      19302),
    ("stun1.l.google.com",     19302),
    ("stun.cloudflare.com",    3478),
    ("stun.nextcloud.com",     3478),
    ("stun.relay.metered.ca",  80),
]

STUN_BINDING_REQUEST  = 0x0001
STUN_BINDING_RESPONSE = 0x0101
STUN_MAGIC_COOKIE     = 0x2112A442
ATTR_MAPPED_ADDRESS   = 0x0001
ATTR_XOR_MAPPED_ADDRESS = 0x0020


def _build_binding_request() -> tuple[bytes, bytes]:
    """Monta um STUN Binding Request. Retorna (pacote, transaction_id)."""
    transaction_id = os.urandom(12)
    # Header: type(2) length(2) magic(4) transaction_id(12) = 20 bytes
    header = struct.pack(">HHI", STUN_BINDING_REQUEST, 0, STUN_MAGIC_COOKIE) + transaction_id
    return header, transaction_id


def _parse_binding_response(data: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """
    Analisa a resposta STUN. Retorna (public_ip, public_port) ou None se inválida.
    Suporta MAPPED-ADDRESS (0x0001) e XOR-MAPPED-ADDRESS (0x0020).
    """
    if len(data) < 20:
        return None

    msg_type, msg_len, magic = struct.unpack(">HHI", data[:8])
    if msg_type != STUN_BINDING_RESPONSE:
        return None
    if magic != STUN_MAGIC_COOKIE:
        return None
    if data[8:20] != transaction_id:
        return None

    # Percorre atributos TLV
    offset = 20
    while offset + 4 <= len(data):
        attr_type, attr_len = struct.unpack(">HH", data[offset:offset + 4])
        attr_val = data[offset + 4: offset + 4 + attr_len]
        # Alinha para múltiplo de 4
        offset += 4 + ((attr_len + 3) & ~3)

        if attr_type == ATTR_XOR_MAPPED_ADDRESS and len(attr_val) >= 8:
            family = attr_val[1]
            if family != 0x01:  # IPv4 apenas
                continue
            port = struct.unpack(">H", attr_val[2:4])[0] ^ (STUN_MAGIC_COOKIE >> 16)
            ip_int = struct.unpack(">I", attr_val[4:8])[0] ^ STUN_MAGIC_COOKIE
            ip = socket.inet_ntoa(struct.pack(">I", ip_int))
            return ip, port

        elif attr_type == ATTR_MAPPED_ADDRESS and len(attr_val) >= 8:
            family = attr_val[1]
            if family != 0x01:
                continue
            port = struct.unpack(">H", attr_val[2:4])[0]
            ip = socket.inet_ntoa(attr_val[4:8])
            return ip, port

    return None


def get_public_address(local_port: int = 0, timeout: float = 4.0) -> tuple[str, int] | None:
    """
    Descobre o IP público e a porta mapeada pelo NAT para `local_port` usando STUN.

    Args:
        local_port: porta UDP local a ser mapeada (0 = escolha do SO).
        timeout: tempo máximo de espera total em segundos.

    Returns:
        (public_ip, public_port) ou None se todos os servidores falharem.
    """
    result: list[tuple[str, int] | None] = [None]
    found = threading.Event()

    def probe(server_host: str, server_port: int):
        if found.is_set():
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout * 0.8)
            if local_port:
                sock.bind(("0.0.0.0", local_port))

            pkt, txid = _build_binding_request()
            sock.sendto(pkt, (server_host, server_port))
            data, _ = sock.recvfrom(512)
            parsed = _parse_binding_response(data, txid)
            if parsed and not found.is_set():
                result[0] = parsed
                found.set()
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

    threads = []
    for host, port in STUN_SERVERS:
        t = threading.Thread(target=probe, args=(host, port), daemon=True)
        t.start()
        threads.append(t)

    found.wait(timeout=timeout)
    return result[0]


if __name__ == "__main__":
    print("Detectando IP público via STUN...")
    addr = get_public_address()
    if addr:
        print(f"IP público: {addr[0]}  porta: {addr[1]}")
    else:
        print("Falhou — sem resposta de nenhum servidor STUN.")
