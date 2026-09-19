"""
StreamShare - Protocolo de pacotes UDP (Fase 1)
Experimental, apenas para validação em LAN. NÃO é a decisão final de
transporte para a Internet (ver item 4 das correções do usuário).

Cada pacote UDP = HEADER (17 bytes) + PAYLOAD (fragmento de uma NAL H.264).
Uma "unit" = uma NAL unit H.264 (SPS, PPS, ou slice/frame codificado).
NALs maiores que MAX_FRAGMENT_PAYLOAD são fragmentadas em múltiplos pacotes.

Pacote de Handshake:
  - FLAG_HANDSHAKE no campo flags do header
  - payload = HANDSHAKE_FORMAT struct com width, height, fps, quality_id
"""
import struct

HEADER_FORMAT = "!IIHHBI"
# seq(4) unit_id(4) frag_index(2) frag_count(2) flags(1) timestamp_ms(4)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

FLAG_KEYFRAME  = 0x01
FLAG_HANDSHAKE = 0x02  # pacote de sinalização com metadados do stream

MAX_PACKET_SIZE = 1200  # bytes, seguro abaixo do MTU Ethernet padrão de 1500
MAX_FRAGMENT_PAYLOAD = MAX_PACKET_SIZE - HEADER_SIZE

# Handshake payload: width(2) height(2) fps(1) quality_id(1) viewer_count(1)
# quality_id: 0=source, 1=720p, 2=1080p, 3=1440p
HANDSHAKE_FORMAT = "!HHBBB"
HANDSHAKE_SIZE   = struct.calcsize(HANDSHAKE_FORMAT)

QUALITY_SOURCE = 0
QUALITY_720P   = 1
QUALITY_1080P  = 2
QUALITY_1440P  = 3

QUALITY_NAMES = {
    QUALITY_SOURCE: "source",
    QUALITY_720P:   "720p",
    QUALITY_1080P:  "1080p",
    QUALITY_1440P:  "1440p",
}


def pack_header(seq, unit_id, frag_index, frag_count, flags, timestamp_ms):
    return struct.pack(
        HEADER_FORMAT, seq, unit_id, frag_index, frag_count, flags,
        timestamp_ms & 0xFFFFFFFF,
    )


def unpack_header(data):
    seq, unit_id, frag_index, frag_count, flags, timestamp_ms = struct.unpack(
        HEADER_FORMAT, data[:HEADER_SIZE]
    )
    return {
        "seq": seq,
        "unit_id": unit_id,
        "frag_index": frag_index,
        "frag_count": frag_count,
        "flags": flags,
        "timestamp_ms": timestamp_ms,
    }


def pack_handshake(seq, width, height, fps, quality_id=QUALITY_SOURCE, viewer_count=1):
    """Monta um pacote UDP completo de handshake (header + payload)."""
    ts_ms = 0
    header = pack_header(seq, 0, 0, 1, FLAG_HANDSHAKE, ts_ms)
    v_count = min(255, max(1, int(viewer_count)))
    payload = struct.pack(HANDSHAKE_FORMAT, width, height, fps, quality_id, v_count)
    return header + payload


def unpack_handshake(data):
    """Desempacota o payload de um pacote de handshake. Retorna dict ou None."""
    if len(data) < HEADER_SIZE + HANDSHAKE_SIZE:
        return None
    w, h, fps, quality_id, viewer_count = struct.unpack(
        HANDSHAKE_FORMAT, data[HEADER_SIZE: HEADER_SIZE + HANDSHAKE_SIZE]
    )
    return {"width": w, "height": h, "fps": fps, "quality_id": quality_id, "viewer_count": viewer_count}
