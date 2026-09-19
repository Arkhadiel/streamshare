"""
StreamShare - Host (Fase 1)
Captura de tela (MSS) -> encode H.264 (FFmpeg) -> fragmentação -> UDP

Melhorias:
  - Detecção automática da resolução nativa do monitor via mss
  - Opções de qualidade: source, 720p, 1080p, 1440p (escalonamento FFmpeg)
  - Envio de pacote de handshake UDP antes do stream para o viewer detectar dimensões
"""
import argparse
import socket
import subprocess
import sys
import threading
import time

import mss

from protocol import (
    pack_header, pack_handshake,
    MAX_FRAGMENT_PAYLOAD, FLAG_KEYFRAME,
    QUALITY_SOURCE, QUALITY_720P, QUALITY_1080P, QUALITY_1440P, QUALITY_NAMES,
)

START_CODE = b"\x00\x00\x01"

QUALITY_MAP = {
    "source": QUALITY_SOURCE,
    "720p":   QUALITY_720P,
    "1080p":  QUALITY_1080P,
    "1440p":  QUALITY_1440P,
}

# Altura alvo para cada preset (largura calculada proporcionalmente pelo FFmpeg)
QUALITY_HEIGHT = {
    "720p":  720,
    "1080p": 1080,
    "1440p": 1440,
}


def find_nal_units(buf: bytes):
    """Extrai NAL units completas de um buffer Annex-B (start codes de 3 ou 4 bytes). Retorna (nals, sobra)."""
    starts = []
    pos = 0
    while True:
        idx = buf.find(START_CODE, pos)
        if idx == -1:
            break
        sc_start = idx
        min_start = starts[-1][0] + starts[-1][1] if starts else 0
        while sc_start > min_start and buf[sc_start - 1] == 0:
            sc_start -= 1
        sc_len = (idx + 3) - sc_start
        starts.append((sc_start, sc_len))
        pos = idx + 3

    nals = []
    for i in range(len(starts) - 1):
        begin = starts[i][0] + starts[i][1]
        end = starts[i + 1][0]
        nal = buf[begin:end]
        if nal:
            nals.append(nal)

    leftover = buf[starts[-1][0]:] if starts else buf
    return nals, leftover


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames_captured = 0
        self.nals_sent = 0
        self.bytes_sent = 0
        self.keyframes_sent = 0

    def snapshot_and_reset(self):
        with self.lock:
            data = (self.frames_captured, self.nals_sent, self.bytes_sent, self.keyframes_sent)
            self.frames_captured = self.nals_sent = self.bytes_sent = self.keyframes_sent = 0
        return data


def capture_loop(sct, monitor, ffmpeg_proc, fps, metrics, stop_event):
    interval = 1.0 / fps
    next_time = time.perf_counter()
    while not stop_event.is_set():
        now = time.perf_counter()
        if now < next_time:
            time.sleep(next_time - now)
        next_time += interval
        try:
            frame = sct.grab(monitor)
        except Exception as e:
            print(f"[host] erro na captura: {e}", file=sys.stderr)
            continue
        try:
            ffmpeg_proc.stdin.write(frame.bgra)
        except (BrokenPipeError, OSError):
            print("[host] ffmpeg encerrou inesperadamente (stdin fechado)")
            stop_event.set()
            break
        with metrics.lock:
            metrics.frames_captured += 1


def send_loop(ffmpeg_proc, sock, targets_lock, targets, metrics, stop_event):
    buf = b""
    unit_id = 0
    seq = 0
    while not stop_event.is_set():
        chunk = ffmpeg_proc.stdout.read(65536)
        if not chunk:
            print("[host] ffmpeg (encoder) encerrou o stdout")
            stop_event.set()
            break
        buf += chunk
        nals, buf = find_nal_units(buf)
        for nal in nals:
            nal_type = nal[0] & 0x1F
            is_key = nal_type in (5, 7, 8)  # IDR, SPS, PPS
            frag_count = max(1, (len(nal) + MAX_FRAGMENT_PAYLOAD - 1) // MAX_FRAGMENT_PAYLOAD)
            ts_ms = int(time.time() * 1000)
            for frag_index in range(frag_count):
                start = frag_index * MAX_FRAGMENT_PAYLOAD
                payload = nal[start:start + MAX_FRAGMENT_PAYLOAD]
                flags = FLAG_KEYFRAME if is_key else 0
                header = pack_header(seq, unit_id, frag_index, frag_count, flags, ts_ms)
                pkt = header + payload

                with targets_lock:
                    targets_snapshot = list(targets)

                for target_addr in targets_snapshot:
                    try:
                        sock.sendto(pkt, target_addr)
                    except (socket.gaierror, OSError) as e:
                        print(f"[host] erro ao enviar pacote para {target_addr}: {e}")

                seq = (seq + 1) & 0xFFFFFFFF
                with metrics.lock:
                    metrics.bytes_sent += len(pkt) * len(targets_snapshot)
            with metrics.lock:
                metrics.nals_sent += 1
                if is_key:
                    metrics.keyframes_sent += 1
            unit_id = (unit_id + 1) & 0xFFFFFFFF


def handshake_loop(sock, targets_lock, targets, width, height, fps, quality_id, stop_event):
    """Envia pacotes de handshake periodicamente para que os viewers (re)conectem."""
    seq = 0xFFFF0000  # sequência separada para handshakes
    while not stop_event.is_set():
        with targets_lock:
            targets_snapshot = list(targets)

        viewer_count = len(targets_snapshot)
        pkt = pack_handshake(seq, width, height, fps, quality_id, viewer_count)

        for target_addr in targets_snapshot:
            try:
                sock.sendto(pkt, target_addr)
            except (socket.gaierror, OSError) as e:
                print(f"[host] erro de handshake para {target_addr}: {e}")

        seq = (seq + 1) & 0xFFFFFFFF
        # Reenviar a cada 0.5s para garantir entrega mesmo com perda de pacotes
        time.sleep(0.5)


def ffmpeg_stderr_loop(proc, stop_event):
    """Lê stderr do FFmpeg e exibe no console para diagnóstico."""
    try:
        for line in iter(proc.stderr.readline, b""):
            if stop_event.is_set():
                break
            msg = line.decode("utf-8", errors="replace").rstrip()
            if msg:
                print(f"[ffmpeg-host] {msg}", file=sys.stderr)
    except Exception:
        pass


def metrics_loop(metrics, stop_event):
    while not stop_event.is_set():
        time.sleep(1.0)
        frames, nals, byte_count, keyframes = metrics.snapshot_and_reset()
        mbps = (byte_count * 8) / 1_000_000
        print(f"[host] fps_capturado={frames} nals_enviados={nals} "
              f"keyframes={keyframes} bitrate={mbps:.2f}Mbps")


def resolve_output_dimensions(native_w, native_h, quality):
    """
    Retorna (out_w, out_h, scale_filter) para o preset de qualidade.
    scale_filter é None se não precisar escalonar.
    """
    if quality == "source":
        return native_w, native_h, None

    target_h = QUALITY_HEIGHT[quality]
    if native_h <= target_h:
        # Resolução nativa já é menor ou igual ao alvo — não escala
        print(f"[host] resolução nativa ({native_w}x{native_h}) <= {quality}, "
              f"transmitindo em resolução nativa.")
        return native_w, native_h, None

    # Calcula largura proporcional (múltiplo de 2 para yuv420p)
    ratio = target_h / native_h
    out_w = int(native_w * ratio)
    if out_w % 2 != 0:
        out_w += 1
    scale_filter = f"scale={out_w}:{target_h}"
    return out_w, target_h, scale_filter


ENCODER_PARAMS = {
    # AMD AMF — ultralowlatency usage, CBR, repeat SPS/PPS on every keyframe
    "h264_amf": ["-c:v", "h264_amf", "-usage", "ultralowlatency", "-quality", "speed",
                 "-rc", "cbr", "-b:v", "8M", "-repeat_headers", "1"],
    # Nvidia NVENC — p1 preset = fastest, ull tune, CBR, repeat SPS/PPS
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
                   "-rc", "cbr", "-b:v", "8M", "-repeat_headers", "1"],
    # Intel QSV — veryfast preset, CBR, repeat SPS/PPS
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "veryfast",
                 "-b:v", "8M", "-repeat_headers", "1"],
    # Software fallback — ultrafast + zerolatency, repeat-headers via x264-params
    "libx264": ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"],
}

# Hardware encoders that need GPU-side scaling instead of the CPU `scale` filter.
# Key: encoder_id -> scale filter template (use str.format(w=..., h=...) to fill dimensions)
HW_SCALE_FILTERS = {
    "h264_nvenc": "scale_cuda={w}:{h}",
    "h264_amf":   "scale_vulkan={w}:{h}",
    # h264_qsv can use vpp_qsv but scale= works fine via CPU→GPU copy; keep it simple
}


def list_available_monitors():
    """Retorna lista de monitores detectados pelo mss."""
    with mss.MSS() as sct:
        monitors = []
        for idx, m in enumerate(sct.monitors):
            if idx == 0:
                continue
            is_primary = m.get("is_primary", idx == 1)
            desc = f"Monitor {idx}: {m['width']}x{m['height']}" + (" (Primário)" if is_primary else "")
            monitors.append({"index": idx, "name": desc, "width": m["width"], "height": m["height"]})
        return monitors


def _probe_encoder(ffmpeg_path: str, encoder_id: str) -> bool:
    """
    Testa se um encoder de hardware realmente funciona rodando um encode mínimo
    (64x64, 1 frame, saída descartada). Retorna True se o processo sair sem erros.
    """
    try:
        cmd = [
            ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=black:s=64x64:r=1:d=0.1",
            "-pix_fmt", "yuv420p",
            "-c:v", encoder_id,
            "-frames:v", "1",
            "-f", "null", "-",
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=8.0)
        return res.returncode == 0
    except Exception:
        return False


def detect_available_encoders(ffmpeg_path: str = "ffmpeg") -> list[dict]:
    """
    Detecta encoders H.264 disponíveis em duas etapas:
    1. Lista encoders reportados pelo FFmpeg.
    2. Proba cada encoder de hardware com um encode real de 1 frame para confirmar
       que o GPU/driver está funcional (encoder listado ≠ encoder funcional).
    Retorna lista de dicts com 'id', 'label' e 'hardware', libx264 sempre incluso.
    """
    stdout = ""
    try:
        res = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        if res.returncode == 0:
            stdout = res.stdout
    except Exception:
        pass

    HW_CANDIDATES = [
        ("h264_nvenc", "Nvidia NVENC (Hardware)"),
        ("h264_amf",   "AMD AMF (Hardware)"),
        ("h264_qsv",   "Intel QSV (Hardware)"),
    ]

    available = []
    for enc_id, label in HW_CANDIDATES:
        if enc_id in stdout:
            if _probe_encoder(ffmpeg_path, enc_id):
                available.append({"id": enc_id, "label": label, "hardware": True})
            else:
                print(f"[host] Encoder {enc_id} listado mas falhou na prova — ignorando.", file=sys.stderr)

    available.append({"id": "libx264", "label": "CPU (Software - libx264)", "hardware": False})
    return available


class HostSession:
    """Encapsula a sessão de transmissão do Host para fácil integração com a UI."""

    def __init__(
        self,
        initial_targets: list[tuple[str, int]] = None,
        target_port: int = 5555,
        monitor_idx: int = 1,
        fps: int = 30,
        quality: str = "source",
        ffmpeg_path: str = "ffmpeg",
        preferred_encoder: str = "libx264",
        metrics_callback=None,
    ):
        self.target_port = target_port
        self.targets_lock = threading.Lock()
        self.targets = []
        if initial_targets:
            for item in initial_targets:
                if isinstance(item, (list, tuple)):
                    ip = item[0]
                    port = item[1] if len(item) > 1 and item[1] is not None else self.target_port
                else:
                    ip = str(item)
                    port = self.target_port
                if not any(t[0] == ip for t in self.targets):
                    self.targets.append((ip, port))

        self.monitor_idx = monitor_idx
        self.fps = fps
        self.quality = quality
        self.ffmpeg_path = ffmpeg_path
        self.preferred_encoder = preferred_encoder or "libx264"
        self.active_encoder = self.preferred_encoder
        self.metrics_callback = metrics_callback

        self.stop_event = threading.Event()
        self.metrics = Metrics()
        self.proc = None
        self.sock = None
        self.threads = []
        self.out_w = 0
        self.out_h = 0

    def add_viewer(self, ip: str, port: int = None):
        """Adiciona um novo (ip, port) à lista de destinos de forma thread-safe sem duplicatas de (ip, port)."""
        p = port if port is not None else self.target_port
        with self.targets_lock:
            if not any(t[0] == ip and t[1] == p for t in self.targets):
                self.targets.append((ip, p))
                print(f"[host] Espectador adicionado à sessão: {ip}:{p}")

    def remove_viewer(self, ip: str, port: int = None):
        """Remove o (ip, port) da lista de destinos de forma thread-safe."""
        with self.targets_lock:
            if port is None:
                self.targets[:] = [t for t in self.targets if t[0] != ip]
            else:
                self.targets[:] = [t for t in self.targets if not (t[0] == ip and t[1] == port)]
            print(f"[host] Espectador removido da sessão.")

    def start(self):
        sct = mss.MSS()
        monitor = sct.monitors[self.monitor_idx]
        native_w, native_h = monitor["width"], monitor["height"]

        self.out_w, self.out_h, scale_filter = resolve_output_dimensions(native_w, native_h, self.quality)
        quality_id = QUALITY_MAP[self.quality]
        gop = self.fps

        def _build_ffmpeg_cmd(encoder_id):
            cmd = [
                self.ffmpeg_path, "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgra",
                "-s", f"{native_w}x{native_h}", "-r", str(self.fps),
                "-i", "-",
            ]

            # Hardware encoders handle yuv420p internally — setting it before the
            # encoder params causes a pipeline error on some drivers. For software
            # (libx264) we keep the explicit conversion.
            is_hw = encoder_id in HW_SCALE_FILTERS or encoder_id == "h264_qsv"
            if not is_hw:
                cmd += ["-pix_fmt", "yuv420p"]

            params = ENCODER_PARAMS.get(encoder_id, ENCODER_PARAMS["libx264"])
            cmd += list(params)
            cmd += ["-g", str(gop)]

            if encoder_id == "libx264":
                # x264-params: repeat SPS/PPS on every IDR so viewer never misses headers
                cmd += ["-x264-params", "repeat-headers=1"]

            # Scaling: use GPU-native filter for NVENC/AMF, CPU scale= for everything else
            if scale_filter:
                hw_scale_tmpl = HW_SCALE_FILTERS.get(encoder_id)
                if hw_scale_tmpl:
                    # Build GPU-side scale filter with resolved dimensions
                    gpu_scale = hw_scale_tmpl.format(w=self.out_w, h=self.out_h)
                    cmd += ["-vf", gpu_scale]
                else:
                    cmd += ["-vf", scale_filter]

            cmd += ["-f", "h264", "-"]
            return cmd

        ffmpeg_cmd = _build_ffmpeg_cmd(self.preferred_encoder)

        self.proc = subprocess.Popen(
            ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        # Checagem de 2.0s para verificar se o processo Popen inicializou sem erros
        time.sleep(2.0)
        if self.proc.poll() is not None:
            err_msg = ""
            try:
                err_msg = self.proc.stderr.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            print(f"[host] AVISO: Encoder '{self.preferred_encoder}' falhou em runtime ({err_msg.strip()}). Fazendo fallback para libx264...", file=sys.stderr)

            ffmpeg_cmd = _build_ffmpeg_cmd("libx264")
            self.proc = subprocess.Popen(
                ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.active_encoder = "libx264 (fallback automático)"
        else:
            self.active_encoder = self.preferred_encoder

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except Exception:
            pass

        self.stop_event.clear()

        def custom_metrics_loop():
            while not self.stop_event.is_set():
                time.sleep(1.0)
                frames, nals, byte_count, keyframes = self.metrics.snapshot_and_reset()
                mbps = (byte_count * 8) / 1_000_000
                if self.metrics_callback:
                    self.metrics_callback(frames, nals, keyframes, mbps)
                else:
                    print(f"[host] fps_capturado={frames} nals_enviados={nals} "
                          f"keyframes={keyframes} bitrate={mbps:.2f}Mbps")

        self.threads = [
            threading.Thread(
                target=ffmpeg_stderr_loop, args=(self.proc, self.stop_event), daemon=True
            ),
            threading.Thread(
                target=handshake_loop,
                args=(self.sock, self.targets_lock, self.targets, self.out_w, self.out_h, self.fps, quality_id, self.stop_event),
                daemon=True,
            ),
            threading.Thread(
                target=capture_loop,
                args=(sct, monitor, self.proc, self.fps, self.metrics, self.stop_event),
                daemon=True,
            ),
            threading.Thread(
                target=send_loop,
                args=(self.proc, self.sock, self.targets_lock, self.targets, self.metrics, self.stop_event),
                daemon=True,
            ),
            threading.Thread(
                target=custom_metrics_loop, daemon=True
            ),
        ]
        for t in self.threads:
            t.start()

    def stop(self):
        self.stop_event.set()
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                pass
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description="StreamShare Host")
    parser.add_argument("--target-ip", required=True, help="IP do Viewer na rede local")
    parser.add_argument("--target-port", type=int, default=5555)
    parser.add_argument("--monitor", type=int, default=1, help="Índice mss (1 = monitor primário)")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument(
        "--quality",
        choices=["source", "720p", "1080p", "1440p"],
        default="source",
        help="Qualidade de saída: source (nativa), 720p, 1080p ou 1440p",
    )
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    args = parser.parse_args()

    session = HostSession(
        initial_targets=[(args.target_ip, args.target_port)],
        target_port=args.target_port,
        monitor_idx=args.monitor,
        fps=args.fps,
        quality=args.quality,
        ffmpeg_path=args.ffmpeg_path,
    )
    session.start()

    print(f"[host] transmitindo para {args.target_ip}:{args.target_port}. Ctrl+C para parar.")
    try:
        while not session.stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[host] encerrando...")
    finally:
        session.stop()


if __name__ == "__main__":
    main()

