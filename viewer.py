"""
StreamShare - Viewer (Fase 1)
UDP -> reconstrução de NALs -> decode H.264 (FFmpeg) -> exibição (OpenCV)

Melhorias:
  - Detecção automática de resolução via pacote de handshake UDP do host
  - Não requer mais --width / --height manuais
  - Janela responsiva: redimensionar a janela não quebra o stream
    (o frame é escalonado por aspect ratio para caber na janela atual)
"""
import argparse
import queue
import socket
import subprocess
import sys
import threading
import time

import cv2
import numpy as np

from protocol import (
    unpack_header, unpack_handshake,
    HEADER_SIZE, FLAG_KEYFRAME, FLAG_HANDSHAKE,
    QUALITY_NAMES,
)

UNIT_TIMEOUT = 0.3   # segundos de espera máxima por fragmentos de uma NAL
HANDSHAKE_TIMEOUT = 30.0  # segundos máximos aguardando handshake do host


def pick_free_udp_port() -> int:
    """Abre um socket UDP temporário com bind em ('0.0.0.0', 0), obtém a porta atribuída pelo SO e a fecha."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]
    finally:
        s.close()


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.packets_received = 0
        self.bytes_received = 0
        self.units_completed = 0
        self.units_dropped = 0
        self.packet_loss = 0
        self.frames_displayed = 0
        self._last_seq = None

    def register_seq(self, seq):
        with self.lock:
            if self._last_seq is not None:
                expected = (self._last_seq + 1) & 0xFFFFFFFF
                if seq != expected:
                    gap = (seq - expected) & 0xFFFFFFFF
                    if gap < 10_000:  # ignora wraparound absurdo
                        self.packet_loss += gap
            self._last_seq = seq

    def snapshot_and_reset(self):
        with self.lock:
            data = (self.packets_received, self.bytes_received, self.units_completed,
                    self.units_dropped, self.packet_loss, self.frames_displayed)
            self.packets_received = self.bytes_received = 0
            self.units_completed = self.units_dropped = 0
            self.packet_loss = 0
            self.frames_displayed = 0
        return data


def wait_for_handshake(sock, timeout=HANDSHAKE_TIMEOUT, stop_event=None):
    """
    Aguarda um pacote de handshake do host e retorna (width, height, fps, quality_id, viewer_count).
    Descarta pacotes de dados normais até receber o handshake.
    """
    print(f"[viewer] aguardando handshake do host (timeout={timeout}s)...")
    deadline = time.time() + timeout
    sock.settimeout(1.0)
    while time.time() < deadline:
        if stop_event and stop_event.is_set():
            raise InterruptedError("Busca por handshake cancelada.")

        try:
            data, addr = sock.recvfrom(65535)
        except (socket.timeout, TimeoutError):
            remaining = int(deadline - time.time())
            if remaining > 0 and remaining % 5 == 0:
                print(f"[viewer] ainda aguardando handshake... ({remaining}s restantes)")
            continue
        except OSError:
            break

        if len(data) < HEADER_SIZE:
            continue

        header = unpack_header(data)
        if header["flags"] & FLAG_HANDSHAKE:
            info = unpack_handshake(data)
            if info:
                quality_name = QUALITY_NAMES.get(info["quality_id"], "?")
                viewer_count = info.get("viewer_count", 1)
                print(f"[viewer] handshake recebido de {addr}: "
                      f"{info['width']}x{info['height']} @ {info['fps']}fps "
                      f"qualidade={quality_name} (viewers={viewer_count})")
                return info["width"], info["height"], info["fps"], info["quality_id"], viewer_count

    raise TimeoutError(
        f"[viewer] handshake não recebido em {timeout}s. "
        "Verifique se o host está rodando e o IP/porta estão corretos."
    )


def ffmpeg_stderr_loop(proc, stop_event):
    """Lê stderr do FFmpeg e exibe no console para diagnóstico."""
    try:
        for line in iter(proc.stderr.readline, b""):
            if stop_event.is_set():
                break
            msg = line.decode("utf-8", errors="replace").rstrip()
            if msg:
                print(f"[ffmpeg-viewer] {msg}", file=sys.stderr)
    except Exception:
        pass


def recv_loop(sock, decoder_proc, frame_queue, metrics, stop_event, viewer_count_callback=None, current_viewer_count=None):
    """Recebe pacotes UDP, remonta NAL units e envia ao decoder FFmpeg."""
    pending = {}  # unit_id -> {"frags": {idx: bytes}, "count": int, "t0": float}
    last_cleanup = time.time()
    recv_start_time = time.time()
    received_video_packet = False
    warned_firewall = False

    def flush_stale():
        now = time.time()
        stale = [uid for uid, u in pending.items() if now - u["t0"] > UNIT_TIMEOUT]
        for uid in stale:
            del pending[uid]
            with metrics.lock:
                metrics.units_dropped += 1

    sock.settimeout(0.5)
    while not stop_event.is_set():
        if not received_video_packet and not warned_firewall:
            if time.time() - recv_start_time > 5.0:
                print(
                    "[viewer] AVISO: Nenhum pacote de vídeo recebido nos últimos 5 segundos.\n"
                    "[viewer] Se o Host já estiver transmitindo, isso pode ser provocado pelo Windows Firewall bloqueando a porta UDP.\n"
                    "[viewer] Certifique-se de criar uma regra de entrada no Firewall para permitir a porta UDP usada.",
                    file=sys.stderr,
                )
                warned_firewall = True

        try:
            data, _ = sock.recvfrom(65535)
        except (socket.timeout, TimeoutError):
            if time.time() - last_cleanup > 0.2:
                flush_stale()
                last_cleanup = time.time()
            continue
        except OSError:
            break

        if len(data) < HEADER_SIZE:
            continue

        header = unpack_header(data)

        if header["flags"] & FLAG_HANDSHAKE:
            info = unpack_handshake(data)
            if info and "viewer_count" in info:
                v_count = info["viewer_count"]
                if current_viewer_count is not None and current_viewer_count[0] != v_count:
                    current_viewer_count[0] = v_count
                    if viewer_count_callback:
                        try:
                            viewer_count_callback(v_count)
                        except Exception as e:
                            print(f"[viewer] erro em viewer_count_callback: {e}", file=sys.stderr)
            continue

        received_video_packet = True
        payload = data[HEADER_SIZE:]

        with metrics.lock:
            metrics.packets_received += 1
            metrics.bytes_received += len(data)
        metrics.register_seq(header["seq"])

        uid = header["unit_id"]
        unit = pending.get(uid)
        if unit is None:
            unit = {"frags": {}, "count": header["frag_count"], "t0": time.time()}
            pending[uid] = unit
        unit["frags"][header["frag_index"]] = payload

        if len(unit["frags"]) == unit["count"]:
            nal = b"".join(unit["frags"][i] for i in range(unit["count"]))
            del pending[uid]
            try:
                decoder_proc.stdin.write(b"\x00\x00\x01" + nal)
            except (BrokenPipeError, OSError):
                print("[viewer] decoder encerrou (stdin fechado)")
                stop_event.set()
                break
            with metrics.lock:
                metrics.units_completed += 1

        if time.time() - last_cleanup > 0.2:
            flush_stale()
            last_cleanup = time.time()


def display_loop(decoder_proc, width, height, metrics, stop_event):
    """Fallback via OpenCV, usado apenas quando não há frame_callback (execução standalone)."""
    frame_size = width * height * 3
    buf = b""

    win_name = "StreamShare Viewer"
    cv2.namedWindow(win_name, cv2.WINDOW_NORMAL)
    init_w = min(width, 1280)
    init_h = int(init_w * height / width)
    cv2.resizeWindow(win_name, init_w, init_h)

    while not stop_event.is_set():
        needed = frame_size - len(buf)
        chunk = decoder_proc.stdout.read(needed)
        if not chunk:
            print("[viewer] decoder encerrou o stdout")
            stop_event.set()
            break
        buf += chunk
        if len(buf) < frame_size:
            continue

        frame_bytes, buf = buf[:frame_size], buf[frame_size:]
        frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((height, width, 3))

        try:
            rect = cv2.getWindowImageRect(win_name)
            win_w, win_h = rect[2], rect[3]
        except Exception:
            win_w, win_h = init_w, init_h

        if win_w > 0 and win_h > 0:
            aspect = width / height
            if win_w / win_h > aspect:
                new_h = win_h
                new_w = int(new_h * aspect)
            else:
                new_w = win_w
                new_h = int(new_w / aspect)
            new_w = max(1, new_w)
            new_h = max(1, new_h)
            resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            canvas = np.zeros((win_h, win_w, 3), dtype=np.uint8)
            y_off = (win_h - new_h) // 2
            x_off = (win_w - new_w) // 2
            canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
            cv2.imshow(win_name, canvas)
        else:
            cv2.imshow(win_name, frame)

        with metrics.lock:
            metrics.frames_displayed += 1

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q") or key == 27:
            stop_event.set()
            break

    cv2.destroyAllWindows()


class ViewerSession:
    """Encapsula a sessão do Viewer para fácil integração com a UI."""

    def __init__(
        self,
        listen_port: int = 5555,
        ffmpeg_path: str = "ffmpeg",
        handshake_timeout: float = HANDSHAKE_TIMEOUT,
        frame_callback=None,
        metrics_callback=None,
        viewer_count_callback=None,
    ):
        self.listen_port = listen_port
        self.ffmpeg_path = ffmpeg_path
        self.handshake_timeout = handshake_timeout
        self.frame_callback = frame_callback
        self.metrics_callback = metrics_callback
        self.viewer_count_callback = viewer_count_callback

        self.stop_event = threading.Event()
        self.metrics = Metrics()
        self.sock = None
        self.proc = None
        self.threads = []
        self.width = 0
        self.height = 0
        self.fps = 0
        self.viewer_count = 1

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.listen_port))
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)

        self.stop_event.clear()

        # Aguarda handshake
        self.width, self.height, self.fps, quality_id, self.viewer_count = wait_for_handshake(
            self.sock, self.handshake_timeout, self.stop_event
        )
        if self.viewer_count_callback:
            try:
                self.viewer_count_callback(self.viewer_count)
            except Exception:
                pass

        ffmpeg_cmd = [
            self.ffmpeg_path, "-hide_banner", "-loglevel", "error",
            "-f", "h264", "-i", "-",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "-",
        ]
        self.proc = subprocess.Popen(
            ffmpeg_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

        def custom_frame_loop():
            frame_size = self.width * self.height * 3
            buf = b""
            while not self.stop_event.is_set():
                needed = frame_size - len(buf)
                chunk = self.proc.stdout.read(needed)
                if not chunk:
                    self.stop_event.set()
                    break
                buf += chunk
                if len(buf) < frame_size:
                    continue
                frame_bytes, buf = buf[:frame_size], buf[frame_size:]
                frame = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((self.height, self.width, 3))
                if self.frame_callback:
                    self.frame_callback(frame, self.width, self.height)
                with self.metrics.lock:
                    self.metrics.frames_displayed += 1

        def custom_metrics_loop():
            while not self.stop_event.is_set():
                time.sleep(1.0)
                pkts, byte_count, units_ok, units_drop, loss, displayed = self.metrics.snapshot_and_reset()
                mbps = (byte_count * 8) / 1_000_000
                if self.metrics_callback:
                    self.metrics_callback(pkts, byte_count, units_ok, units_drop, loss, displayed, mbps)
                else:
                    print(f"[viewer] pacotes={pkts} bitrate={mbps:.2f}Mbps "
                          f"nals_ok={units_ok} nals_perdidas={units_drop} "
                          f"packet_loss~={loss} fps_exibido={displayed}")

        def opencv_display_target():
            display_loop(self.proc, self.width, self.height, self.metrics, self.stop_event)

        display_target = custom_frame_loop if self.frame_callback else opencv_display_target

        current_viewer_count = [self.viewer_count]

        def _on_count_change(v_count):
            self.viewer_count = v_count
            if self.viewer_count_callback:
                self.viewer_count_callback(v_count)

        self.threads = [
            threading.Thread(target=ffmpeg_stderr_loop, args=(self.proc, self.stop_event), daemon=True),
            threading.Thread(
                target=recv_loop,
                args=(self.sock, self.proc, None, self.metrics, self.stop_event, _on_count_change, current_viewer_count),
                daemon=True,
            ),
            threading.Thread(target=display_target, daemon=True),
            threading.Thread(target=custom_metrics_loop, daemon=True),
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
    parser = argparse.ArgumentParser(description="StreamShare Viewer")
    parser.add_argument("--listen-port", type=int, default=5555)
    parser.add_argument("--ffmpeg-path", default="ffmpeg")
    parser.add_argument(
        "--handshake-timeout",
        type=float,
        default=HANDSHAKE_TIMEOUT,
        help="Segundos aguardando o handshake do host antes de desistir",
    )
    args = parser.parse_args()

    session = ViewerSession(
        listen_port=args.listen_port,
        ffmpeg_path=args.ffmpeg_path,
        handshake_timeout=args.handshake_timeout,
    )

    try:
        session.start()
        print(f"[viewer] stream iniciado em {session.width}x{session.height} @ {session.fps}fps")
        print("[viewer] pressione 'q' ou ESC na janela de vídeo para sair, ou Ctrl+C aqui")
        while not session.stop_event.is_set():
            time.sleep(0.5)
    except TimeoutError as e:
        print(e)
    except KeyboardInterrupt:
        print("\n[viewer] encerrando...")
    finally:
        session.stop()


if __name__ == "__main__":
    main()

