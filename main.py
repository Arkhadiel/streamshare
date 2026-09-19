"""
StreamShare - Aplicação Principal (Fase 2 + 4)
Interface Gráfica unificada em PySide6 para Host e Viewer — LAN e Internet.
"""
import sys
import threading
import time

import cv2
import numpy as np

from PySide6.QtCore import Qt, QThread, Signal, QObject, QTimer
from PySide6.QtGui import QImage, QPixmap, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget,
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QSpinBox, QLineEdit, QFrame, QSizePolicy, QFormLayout
)

from host import list_available_monitors, HostSession, detect_available_encoders
from viewer import ViewerSession, pick_free_udp_port
from discovery import (
    generate_session_code, DiscoveryBroadcaster, scan_for_session,
    InternetHostSession, scan_for_session_internet,
)

# --- Estilo Visual Moderno Dark ---
DARK_STYLE = """
QMainWindow {
    background-color: #121214;
}
QWidget {
    background-color: #121214;
    color: #E1E1E6;
    font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px;
}
QFrame.card {
    background-color: #202024;
    border: 1px solid #1C1C1F;
    border-radius: 6px;
    padding: 20px;
}
QLabel.title {
    font-size: 32px;
    font-weight: bold;
    color: #00875F;
}
QLabel.subtitle {
    font-size: 14px;
    color: #8D8D99;
}
QLabel.code-display {
    font-size: 36px;
    font-weight: bold;
    color: #00B37E;
    letter-spacing: 4px;
    background-color: #121214;
    border: 2px dashed #00B37E;
    border-radius: 6px;
    padding: 12px 24px;
}
QPushButton {
    background-color: #00875F;
    color: #FFFFFF;
    font-weight: bold;
    font-size: 15px;
    border-radius: 4px;
    padding: 12px 24px;
    border: none;
}
QPushButton:hover {
    background-color: #00B37E;
}
QPushButton:pressed {
    background-color: #005E43;
}
QPushButton.secondary {
    background-color: #202024;
    color: #E1E1E6;
    border: 1px solid #29292E;
    border-radius: 4px;
}
QPushButton.secondary:hover {
    background-color: #29292E;
}
QPushButton.danger {
    background-color: #AA2834;
    border-radius: 4px;
}
QPushButton.danger:hover {
    background-color: #F75A68;
}
QLineEdit, QComboBox, QSpinBox {
    background-color: #121214;
    border: 1px solid #29292E;
    border-radius: 4px;
    padding: 10px;
    color: #E1E1E6;
    font-size: 14px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #00B37E;
}
QLabel.status-info {
    color: #00B37E;
    font-weight: 500;
}
QLabel.status-warning {
    color: #FBA94C;
    font-weight: 500;
}
QLabel.status-error {
    color: #F75A68;
    font-weight: 500;
}
"""


# --- Workers e Bridges para Threads ---

class DiscoveryWorker(QThread):
    found = Signal(str, int, str)  # host_ip, video_port, host_name
    failed = Signal(str)           # erro

    def __init__(
        self,
        session_code: str,
        viewer_name: str = "Anônimo",
        viewer_video_port: int = 5555,
        timeout: float = 15.0,
        network_mode: str = "lan",
    ):
        super().__init__()
        self.session_code = session_code
        self.viewer_name = viewer_name
        self.viewer_video_port = viewer_video_port
        self.timeout = timeout
        self.network_mode = network_mode
        self.stop_event = threading.Event()

    def run(self):
        try:
            if self.network_mode == "internet":
                host_ip, video_port, host_name = scan_for_session_internet(
                    target_code=self.session_code,
                    viewer_name=self.viewer_name,
                    viewer_video_port=self.viewer_video_port,
                    stop_event=self.stop_event,
                )
            else:
                host_ip, video_port, host_name = scan_for_session(
                    self.session_code,
                    viewer_name=self.viewer_name,
                    viewer_video_port=self.viewer_video_port,
                    timeout=self.timeout,
                    stop_event=self.stop_event,
                )
            if not self.stop_event.is_set():
                self.found.emit(host_ip, video_port, host_name)
        except Exception as e:
            if not self.stop_event.is_set():
                self.failed.emit(str(e))

    def cancel(self):
        self.stop_event.set()


class HostSessionBridge(QObject):
    viewer_connected_signal = Signal(str, str, int)   # viewer_ip, viewer_name, viewer_video_port
    metrics_signal = Signal(int, int, int, float) # frames, nals, keyframes, mbps


class ViewerSessionWorker(QThread):
    connected = Signal(int, int, int)  # width, height, fps
    frame_ready = Signal(object, int, int)  # frame ndarray, width, height
    metrics_signal = Signal(int, int, int, int, int, int, float)
    viewer_count_changed = Signal(int)
    failed = Signal(str)

    def __init__(self, video_port: int = 5555, timeout: float = 15.0):
        super().__init__()
        self.video_port = video_port
        self.timeout = timeout
        self.session = None

    def run(self):
        def frame_cb(frame, w, h):
            self.frame_ready.emit(frame, w, h)

        def metrics_cb(pkts, byte_count, units_ok, units_drop, loss, displayed, mbps):
            self.metrics_signal.emit(pkts, byte_count, units_ok, units_drop, loss, displayed, mbps)

        def count_cb(count):
            self.viewer_count_changed.emit(count)

        self.session = ViewerSession(
            listen_port=self.video_port,
            handshake_timeout=self.timeout,
            frame_callback=frame_cb,
            metrics_callback=metrics_cb,
            viewer_count_callback=count_cb,
        )

        try:
            self.session.start()
            self.connected.emit(self.session.width, self.session.height, self.session.fps)
        except Exception as e:
            self.failed.emit(str(e))

    def stop(self):
        if self.session:
            self.session.stop()


# --- Custom Video Render Widget ---

class VideoDisplayWidget(QLabel):
    """
    Widget de exibição de vídeo responsivo com letterbox (canvas preto) centralizado
    e overlay flutuante auto-escondido no topo com nome do Host, botão '⛶ Tela Cheia' e '← Voltar'.
    """

    def __init__(self, on_back_callback=None, on_fullscreen_callback=None, parent=None):
        super().__init__(parent)
        self.on_back_callback = on_back_callback
        self.on_fullscreen_callback = on_fullscreen_callback
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: #000000; border-radius: 0px;")
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._last_frame_data = None

        # Container do Overlay Flutuante no topo do vídeo
        self.overlay_header = QWidget(self)
        self.overlay_header.setStyleSheet(
            "QWidget {"
            "  background-color: rgba(20, 20, 24, 210);"
            "  border: 1px solid rgba(255, 255, 255, 40);"
            "  border-radius: 6px;"
            "}"
        )
        overlay_layout = QHBoxLayout(self.overlay_header)
        overlay_layout.setContentsMargins(8, 6, 16, 6)
        overlay_layout.setSpacing(14)

        # Botão sobreposto '← Voltar'
        self.btn_floating_back = QPushButton("← Voltar")
        self.btn_floating_back.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(40, 40, 48, 220);"
            "  color: #E1E1E6;"
            "  border: 1px solid rgba(255, 255, 255, 40);"
            "  border-radius: 4px;"
            "  padding: 6px 14px;"
            "  font-weight: bold;"
            "  font-size: 13px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(0, 179, 126, 230);"
            "  color: #FFFFFF;"
            "}"
        )
        if self.on_back_callback:
            self.btn_floating_back.clicked.connect(self.on_back_callback)

        # Label do nome do Host no topo do vídeo
        self.lbl_overlay_name = QLabel("🟢 Transmissão de Anônimo")
        self.lbl_overlay_name.setStyleSheet(
            "color: #00B37E; font-weight: bold; font-size: 14px; background: transparent; border: none;"
        )

        # Botão de alternar Tela Cheia (Fullscreen)
        self.btn_floating_fullscreen = QPushButton("⛶ Tela Cheia")
        self.btn_floating_fullscreen.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(40, 40, 48, 220);"
            "  color: #E1E1E6;"
            "  border: 1px solid rgba(255, 255, 255, 40);"
            "  border-radius: 4px;"
            "  padding: 6px 14px;"
            "  font-weight: bold;"
            "  font-size: 13px;"
            "}"
            "QPushButton:hover {"
            "  background-color: rgba(0, 179, 126, 230);"
            "  color: #FFFFFF;"
            "}"
        )
        if self.on_fullscreen_callback:
            self.btn_floating_fullscreen.clicked.connect(self.on_fullscreen_callback)

        overlay_layout.addWidget(self.btn_floating_back)
        overlay_layout.addWidget(self.lbl_overlay_name)
        overlay_layout.addWidget(self.btn_floating_fullscreen)

        self.overlay_header.move(20, 20)
        self.overlay_header.adjustSize()
        self.overlay_header.hide()

        # Temporizador para ocultar overlay após ~2.5s sem movimento do mouse
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.setInterval(2500)
        self.hide_timer.timeout.connect(self._hide_floating_overlay)

    def set_connected_host_name(self, host_name: str):
        self.lbl_overlay_name.setText(f"🟢 Transmissão de {host_name}")
        self.overlay_header.adjustSize()

    def update_fullscreen_state(self, is_fullscreen: bool):
        if is_fullscreen:
            self.btn_floating_fullscreen.setText("⛶ Sair da Tela Cheia")
        else:
            self.btn_floating_fullscreen.setText("⛶ Tela Cheia")
        self.overlay_header.adjustSize()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        self._show_floating_overlay()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._show_floating_overlay()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._hide_floating_overlay()

    def _show_floating_overlay(self):
        self.overlay_header.show()
        self.overlay_header.raise_()
        self.hide_timer.start()

    def _hide_floating_overlay(self):
        self.overlay_header.hide()

    def update_frame(self, frame: np.ndarray, native_w: int, native_h: int):
        self._last_frame_data = (frame, native_w, native_h)
        self._render_last_frame()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_last_frame()

    def _render_last_frame(self):
        if self._last_frame_data is None:
            return

        frame, width, height = self._last_frame_data
        win_w = self.width()
        win_h = self.height()

        if win_w <= 0 or win_h <= 0 or width <= 0 or height <= 0:
            return

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

        h, w, _ = canvas.shape
        qimg = QImage(canvas.data, w, h, w * 3, QImage.Format.Format_BGR888)
        self.setPixmap(QPixmap.fromImage(qimg))


# --- Telas da Aplicação ---

class HomeScreen(QWidget):
    """Tela Inicial: Transmitir ou Assistir."""

    def __init__(self, on_host_clicked, on_viewer_clicked, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(24)

        card = QFrame()
        card.setProperty("class", "card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(20)
        card_layout.setContentsMargins(40, 40, 40, 40)

        title = QLabel("StreamShare")
        title.setProperty("class", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel("Transmissão de Tela em Rede Local (LAN)")
        subtitle.setProperty("class", "subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_host = QPushButton("Transmitir Tela")
        btn_host.setMinimumHeight(50)
        btn_host.clicked.connect(on_host_clicked)

        btn_viewer = QPushButton("Assistir Transmissão")
        btn_viewer.setProperty("class", "secondary")
        btn_viewer.setMinimumHeight(50)
        btn_viewer.clicked.connect(on_viewer_clicked)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(16)
        card_layout.addWidget(btn_host)
        card_layout.addWidget(btn_viewer)

        layout.addWidget(card)


class HostWidget(QWidget):
    """Tela do Host: Configuração, Descoberta e Transmissão Unicast."""

    def __init__(self, on_back_clicked, parent=None):
        super().__init__(parent)
        self.on_back_clicked_cb = on_back_clicked

        self.broadcaster = None
        self.internet_host = None
        self._network_mode = "lan"
        self.session = None
        self.bridge = HostSessionBridge()
        self.bridge.viewer_connected_signal.connect(self._on_viewer_connected)
        self.bridge.metrics_signal.connect(self._update_metrics_ui)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)

        # Top bar
        top_bar = QHBoxLayout()
        btn_back = QPushButton("← Voltar")
        btn_back.setProperty("class", "secondary")
        btn_back.clicked.connect(self.stop_host)
        btn_back.clicked.connect(on_back_clicked)

        lbl_title = QLabel("Transmitir Tela")
        lbl_title.setStyleSheet("font-size: 22px; font-weight: bold; color: #E1E1E6;")

        top_bar.addWidget(btn_back)
        top_bar.addSpacing(16)
        top_bar.addWidget(lbl_title)
        top_bar.addStretch()

        layout.addLayout(top_bar)

        # Configuration Card
        self.card_config = QFrame()
        self.card_config.setProperty("class", "card")
        form_layout = QFormLayout(self.card_config)
        form_layout.setSpacing(16)

        self.input_host_name = QLineEdit()
        self.input_host_name.setPlaceholderText("Seu nome (opcional)")
        self.input_host_name.setMaxLength(20)

        self.combo_monitor = QComboBox()
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["source", "720p", "1080p", "1440p"])

        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(10, 60)
        self.spin_fps.setValue(30)

        self.combo_encoder = QComboBox()
        self.lbl_encoder_hint = QLabel("Se o encoder de hardware falhar, o app reverte para CPU automaticamente.")
        self.lbl_encoder_hint.setStyleSheet("color: #8D8D99; font-size: 12px; background: transparent;")

        # Modo de rede: LAN ou Internet
        self.combo_network_mode = QComboBox()
        self.combo_network_mode.addItem("🏠  Rede Local (LAN)", "lan")
        self.combo_network_mode.addItem("🌐  Internet (via servidor de sinalização)", "internet")
        self.lbl_network_hint = QLabel("Internet: requer servidor de sinalização configurado (ver signaling_server/).")
        self.lbl_network_hint.setStyleSheet("color: #8D8D99; font-size: 12px; background: transparent;")

        form_layout.addRow("Seu Nome:", self.input_host_name)
        form_layout.addRow("Monitor:", self.combo_monitor)
        form_layout.addRow("Qualidade:", self.combo_quality)
        form_layout.addRow("FPS:", self.spin_fps)
        form_layout.addRow("Encoder:", self.combo_encoder)
        form_layout.addRow("", self.lbl_encoder_hint)
        form_layout.addRow("Rede:", self.combo_network_mode)
        form_layout.addRow("", self.lbl_network_hint)

        self.btn_start = QPushButton("Iniciar Transmissão")
        self.btn_start.clicked.connect(self.start_host)
        form_layout.addRow("", self.btn_start)

        layout.addWidget(self.card_config)

        # Active Session Card
        self.card_session = QFrame()
        self.card_session.setProperty("class", "card")
        session_layout = QVBoxLayout(self.card_session)
        session_layout.setSpacing(16)
        session_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Barra de status com badge de conexão + contador de espectadores (olho)
        status_bar_layout = QHBoxLayout()
        status_bar_layout.setContentsMargins(0, 0, 0, 0)
        status_bar_layout.setSpacing(12)

        self.lbl_connected_viewer_badge = QLabel("🟡 Aguardando conexão do Viewer na rede local...")
        self.lbl_connected_viewer_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #FBA94C; border-radius: 6px; "
            "padding: 10px 20px; font-weight: bold; color: #FBA94C; font-size: 15px;"
        )
        self.lbl_connected_viewer_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_viewer_count_badge = QLabel("👁️ 0")
        self.lbl_viewer_count_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #29292E; border-radius: 6px; "
            "padding: 10px 18px; font-weight: bold; color: #8D8D99; font-size: 15px;"
        )
        self.lbl_viewer_count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

        status_bar_layout.addWidget(self.lbl_connected_viewer_badge, stretch=1)
        status_bar_layout.addWidget(self.lbl_viewer_count_badge)

        lbl_code_title = QLabel("Código de Conexão:")
        lbl_code_title.setStyleSheet("font-size: 16px; color: #8D8D99;")
        lbl_code_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_code = QLabel("XXXX-XXXX")
        self.lbl_code.setProperty("class", "code-display")
        self.lbl_code.setAlignment(Qt.AlignmentFlag.AlignCenter)

        btn_copy = QPushButton("Copiar Código")
        btn_copy.setProperty("class", "secondary")
        btn_copy.clicked.connect(self._copy_code)

        self.lbl_viewer_list = QLabel("")
        self.lbl_viewer_list.setStyleSheet(
            "color: #8D8D99; font-size: 13px; font-weight: 500; background: transparent;"
        )
        self.lbl_viewer_list.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_metrics = QLabel("FPS: -- | Bitrate: -- Mbps | Keyframes: --")
        self.lbl_metrics.setStyleSheet("font-size: 14px; font-weight: bold; color: #00B37E;")
        self.lbl_metrics.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.lbl_encoder_status = QLabel("Encoder Ativo: --")
        self.lbl_encoder_status.setStyleSheet("color: #8D8D99; font-size: 13px; font-weight: 500;")
        self.lbl_encoder_status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_stop = QPushButton("Parar Transmissão")
        self.btn_stop.setProperty("class", "danger")
        self.btn_stop.clicked.connect(self.stop_host)

        session_layout.addLayout(status_bar_layout)
        session_layout.addSpacing(10)
        session_layout.addWidget(lbl_code_title)
        session_layout.addWidget(self.lbl_code)
        session_layout.addWidget(btn_copy, alignment=Qt.AlignmentFlag.AlignCenter)
        session_layout.addSpacing(10)
        session_layout.addWidget(self.lbl_viewer_list)
        session_layout.addSpacing(10)
        session_layout.addWidget(self.lbl_metrics)
        session_layout.addWidget(self.lbl_encoder_status)
        session_layout.addSpacing(10)
        session_layout.addWidget(self.btn_stop)

        self.card_session.hide()
        layout.addWidget(self.card_session)
        layout.addStretch()

        self.connected_viewers = {}
        self.refresh_monitors()

    def refresh_monitors(self):
        self.combo_monitor.clear()
        monitors = list_available_monitors()
        for m in monitors:
            self.combo_monitor.addItem(m["name"], m["index"])

        self.combo_encoder.clear()
        encoders = detect_available_encoders()
        for enc in encoders:
            self.combo_encoder.addItem(enc["label"], enc["id"])

    def start_host(self):
        host_name = self.input_host_name.text().strip() or "Anônimo"
        session_code = generate_session_code()
        self.lbl_code.setText(session_code)
        self.connected_viewers = {}
        self.lbl_viewer_list.setText("")
        self._network_mode = self.combo_network_mode.currentData() or "lan"

        self.lbl_connected_viewer_badge.setText("🟡 Aguardando solicitação de conexão do Viewer...")
        self.lbl_connected_viewer_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #FBA94C; border-radius: 6px; "
            "padding: 10px 20px; font-weight: bold; color: #FBA94C; font-size: 15px;"
        )
        self.lbl_viewer_count_badge.setText("👁️ 0")
        self.lbl_viewer_count_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #29292E; border-radius: 6px; "
            "padding: 10px 18px; font-weight: bold; color: #8D8D99; font-size: 15px;"
        )
        self.lbl_metrics.setText("FPS: -- | Bitrate: -- Mbps | Keyframes: --")

        def on_connect_cb(viewer_ip, viewer_name, viewer_video_port):
            self.bridge.viewer_connected_signal.emit(viewer_ip, viewer_name, viewer_video_port)

        if self._network_mode == "internet":
            # Modo Internet: STUN + sinalização HTTP
            self.broadcaster = None
            self.internet_host = InternetHostSession(
                session_code=session_code,
                video_port=5555,
                host_name=host_name,
                on_viewer_connected=on_connect_cb,
            )
            try:
                public_ip, public_port = self.internet_host.start()
                self.lbl_connected_viewer_badge.setText(
                    f"🟡 Aguardando Viewer... IP público: {public_ip}"
                )
            except RuntimeError as e:
                self.lbl_connected_viewer_badge.setText(f"❌ Erro Internet: {e}")
                self.lbl_connected_viewer_badge.setStyleSheet(
                    "background-color: #121214; border: 1px solid #F75A68; border-radius: 6px; "
                    "padding: 10px 20px; font-weight: bold; color: #F75A68; font-size: 13px;"
                )
                self.internet_host = None
        else:
            # Modo LAN: broadcast UDP
            self.internet_host = None
            self.broadcaster = DiscoveryBroadcaster(
                session_code,
                video_port=5555,
                host_name=host_name,
                on_viewer_connected=on_connect_cb,
            )
            self.broadcaster.start()

        self.card_config.hide()
        self.card_session.show()

    def _on_viewer_connected(self, viewer_ip: str, viewer_name: str, viewer_video_port: int):
        """Disparado quando um Viewer envia Connect Request com código válido."""
        target_key = (viewer_ip, viewer_video_port)
        self.connected_viewers[target_key] = viewer_name
        count = len(self.connected_viewers)

        self.lbl_connected_viewer_badge.setText(f"🟢 Transmitindo para {count} espectador(es)")
        self.lbl_connected_viewer_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #00B37E; border-radius: 6px; "
            "padding: 10px 20px; font-weight: bold; color: #00B37E; font-size: 15px;"
        )
        self.lbl_viewer_count_badge.setText(f"👁️ {count}")
        self.lbl_viewer_count_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #00B37E; border-radius: 6px; "
            "padding: 10px 18px; font-weight: bold; color: #00B37E; font-size: 15px;"
        )

        viewers_str = "Espectadores:\n" + "\n".join([f"• {name} ({ip}:{port})" for (ip, port), name in self.connected_viewers.items()])
        self.lbl_viewer_list.setText(viewers_str)

        monitor_idx = self.combo_monitor.currentData() or 1
        quality = self.combo_quality.currentText()
        fps = self.spin_fps.value()
        chosen_encoder = self.combo_encoder.currentData() or "libx264"

        def metrics_cb(frames, nals, keyframes, mbps):
            self.bridge.metrics_signal.emit(frames, nals, keyframes, mbps)

        if self.session is None:
            # Primeiro Viewer: cria a HostSession normalmente
            self.session = HostSession(
                initial_targets=[(viewer_ip, viewer_video_port)],
                target_port=viewer_video_port,
                monitor_idx=monitor_idx,
                fps=fps,
                quality=quality,
                preferred_encoder=chosen_encoder,
                metrics_callback=metrics_cb,
            )
            self.session.start()
            self.lbl_encoder_status.setText(f"Encoder Ativo: {self.session.active_encoder}")
        else:
            # Viewer adicional: adiciona o destino à HostSession já em execução
            self.session.add_viewer(viewer_ip, viewer_video_port)

    def stop_host(self):
        if self.broadcaster:
            self.broadcaster.stop()
            self.broadcaster = None
        if self.internet_host:
            self.internet_host.stop()
            self.internet_host = None
        if self.session:
            self.session.stop()
            self.session = None

        self.connected_viewers = {}
        self.lbl_viewer_list.setText("")
        self.lbl_viewer_count_badge.setText("👁️ 0")
        self.lbl_viewer_count_badge.setStyleSheet(
            "background-color: #121214; border: 1px solid #29292E; border-radius: 6px; "
            "padding: 10px 18px; font-weight: bold; color: #8D8D99; font-size: 15px;"
        )
        self.lbl_metrics.setText("FPS: -- | Bitrate: -- Mbps | Keyframes: --")
        self.card_session.hide()
        self.card_config.show()

    def _copy_code(self):
        QApplication.clipboard().setText(self.lbl_code.text())

    def _update_metrics_ui(self, frames, nals, keyframes, mbps):
        self.lbl_metrics.setText(
            f"FPS: {frames} | Bitrate: {mbps:.2f} Mbps | NALs: {nals} | Keyframes: {keyframes}"
        )


class ViewerWidget(QWidget):
    """Tela do Viewer: Conexão e Exibição de Vídeo."""

    def __init__(self, on_back_clicked, parent=None):
        super().__init__(parent)
        self.on_back_clicked_cb = on_back_clicked

        self.discovery_worker = None
        self.viewer_worker = None
        self.current_host_name = "Host"
        self.current_w = 0
        self.current_h = 0
        self.current_fps = 0
        self.current_viewer_count = 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # Top bar Widget
        self.top_bar_widget = QWidget()
        top_bar = QHBoxLayout(self.top_bar_widget)
        top_bar.setContentsMargins(0, 0, 0, 0)
        self.btn_back = QPushButton("← Voltar")
        self.btn_back.setProperty("class", "secondary")
        self.btn_back.clicked.connect(self.disconnect_viewer)
        self.btn_back.clicked.connect(on_back_clicked)

        self.lbl_title = QLabel("Assistir Transmissão")
        self.lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #E1E1E6;")

        self.lbl_status = QLabel("")
        self.lbl_status.setProperty("class", "status-info")

        top_bar.addWidget(self.btn_back)
        top_bar.addSpacing(16)
        top_bar.addWidget(self.lbl_title)
        top_bar.addSpacing(20)
        top_bar.addWidget(self.lbl_status)
        top_bar.addStretch()

        layout.addWidget(self.top_bar_widget)

        # Connect Card
        self.card_connect = QFrame()
        self.card_connect.setProperty("class", "card")
        connect_layout = QVBoxLayout(self.card_connect)
        connect_layout.setSpacing(16)
        connect_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_name_title = QLabel("Seu Nome (opcional):")
        lbl_name_title.setStyleSheet("font-size: 14px; color: #8D8D99;")

        self.input_viewer_name = QLineEdit()
        self.input_viewer_name.setPlaceholderText("Seu nome (opcional)")
        self.input_viewer_name.setMaxLength(20)
        self.input_viewer_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input_viewer_name.setStyleSheet("max-width: 280px;")

        lbl_input_title = QLabel("Digite o Código da Sessão:")
        lbl_input_title.setStyleSheet("font-size: 16px; color: #8D8D99;")

        self.input_code = QLineEdit()
        self.input_code.setPlaceholderText("XXXX-XXXX")
        self.input_code.setMaxLength(10)
        self.input_code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.input_code.setStyleSheet(
            "font-size: 24px; font-weight: bold; letter-spacing: 3px; max-width: 280px;"
        )

        # Modo de rede
        lbl_network_title = QLabel("Modo de Rede:")
        lbl_network_title.setStyleSheet("font-size: 14px; color: #8D8D99;")
        self.combo_viewer_network = QComboBox()
        self.combo_viewer_network.addItem("🏠  Rede Local (LAN)", "lan")
        self.combo_viewer_network.addItem("🌐  Internet (via servidor de sinalização)", "internet")
        self.combo_viewer_network.setStyleSheet("max-width: 320px;")

        self.btn_connect = QPushButton("Conectar")
        self.btn_connect.clicked.connect(self.start_connection)

        self.lbl_error = QLabel("")
        self.lbl_error.setProperty("class", "status-error")
        self.lbl_error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_error.setWordWrap(True)

        connect_layout.addWidget(lbl_name_title, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addWidget(self.input_viewer_name, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addSpacing(8)
        connect_layout.addWidget(lbl_input_title, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addWidget(self.input_code, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addSpacing(4)
        connect_layout.addWidget(lbl_network_title, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addWidget(self.combo_viewer_network, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addSpacing(4)
        connect_layout.addWidget(self.btn_connect, alignment=Qt.AlignmentFlag.AlignCenter)
        connect_layout.addWidget(self.lbl_error)

        layout.addWidget(self.card_connect)

        # Video Display Container com callbacks para '← Voltar' e '⛶ Tela Cheia'
        def on_floating_back_clicked():
            self.disconnect_viewer()
            if self.on_back_clicked_cb:
                self.on_back_clicked_cb()

        self.video_display = VideoDisplayWidget(
            on_back_callback=on_floating_back_clicked,
            on_fullscreen_callback=self.toggle_fullscreen,
        )
        self.video_display.hide()
        layout.addWidget(self.video_display)

    def toggle_fullscreen(self):
        """Alterna entre modo janela e modo Tela Cheia."""
        if not self.window():
            return
        if self.window().isFullScreen():
            self.window().showNormal()
            self.video_display.update_fullscreen_state(is_fullscreen=False)
            self.top_bar_widget.show()
        else:
            self.window().showFullScreen()
            self.video_display.update_fullscreen_state(is_fullscreen=True)
            self.top_bar_widget.hide()

    def start_connection(self):
        code = self.input_code.text().strip()
        if not code:
            self.lbl_error.setText("Por favor, digite o código da sessão.")
            return

        viewer_name = self.input_viewer_name.text().strip() or "Anônimo"
        network_mode = self.combo_viewer_network.currentData() or "lan"
        self.my_video_port = pick_free_udp_port()

        self.lbl_error.setText("")
        mode_label = "rede local (LAN)" if network_mode == "lan" else "internet (sinalização)"
        self.lbl_status.setText(f"Buscando transmissão via {mode_label} (porta {self.my_video_port})...")
        self.lbl_status.setProperty("class", "status-info")
        self.btn_connect.setEnabled(False)

        self.discovery_worker = DiscoveryWorker(
            code,
            viewer_name=viewer_name,
            viewer_video_port=self.my_video_port,
            timeout=15.0,
            network_mode=network_mode,
        )
        self.discovery_worker.found.connect(self._on_session_found)
        self.discovery_worker.failed.connect(self._on_session_failed)
        self.discovery_worker.start()

    def _on_session_found(self, host_ip: str, video_port: int, host_name: str):
        self.current_host_name = host_name
        self.video_display.set_connected_host_name(host_name)
        self.lbl_status.setText(f"Encontrado: transmissão de {host_name}! Conectando vídeo...")

        self.viewer_worker = ViewerSessionWorker(video_port=self.my_video_port, timeout=15.0)
        self.viewer_worker.connected.connect(self._on_video_connected)
        self.viewer_worker.frame_ready.connect(self._on_frame_ready)
        self.viewer_worker.metrics_signal.connect(self._on_metrics_updated)
        self.viewer_worker.viewer_count_changed.connect(self._on_viewer_count_changed)
        self.viewer_worker.failed.connect(self._on_session_failed)
        self.viewer_worker.start()

    def _update_viewer_status(self):
        text = f"Assistindo {self.current_host_name} — {self.current_w}x{self.current_h} @ {self.current_fps}fps"
        if self.current_viewer_count > 1:
            text += f" • assistindo com mais {self.current_viewer_count - 1} pessoa(s)"
        self.lbl_status.setText(text)

    def _on_video_connected(self, w: int, h: int, fps: int):
        self.current_w = w
        self.current_h = h
        self.current_fps = fps
        self._update_viewer_status()
        self.top_bar_widget.hide()
        self.card_connect.hide()
        self.video_display.show()

        # O viewer conecta em modo janela por padrão e decide se quer Tela Cheia pelo botão
        is_fs = self.window().isFullScreen() if self.window() else False
        self.video_display.update_fullscreen_state(is_fs)

    def _on_viewer_count_changed(self, count: int):
        self.current_viewer_count = count
        self._update_viewer_status()

    def _on_frame_ready(self, frame: np.ndarray, w: int, h: int):
        self.video_display.update_frame(frame, w, h)

    def _on_metrics_updated(self, pkts, byte_count, units_ok, units_drop, loss, displayed, mbps):
        status_info = f"FPS: {displayed} | Bitrate: {mbps:.2f} Mbps | Loss: {loss}"
        if self.current_viewer_count > 1:
            status_info += f" • com mais {self.current_viewer_count - 1} pessoa(s)"
        self.lbl_status.setText(f"Assistindo {self.current_host_name} | {status_info}")

    def _on_session_failed(self, err_msg: str):
        self.lbl_status.setText("")
        self.lbl_error.setText(err_msg)
        self.lbl_error.setProperty("class", "status-error")
        self.btn_connect.setEnabled(True)
        self.disconnect_viewer()

    def disconnect_viewer(self):
        if self.discovery_worker:
            self.discovery_worker.cancel()
            self.discovery_worker = None

        if self.viewer_worker:
            self.viewer_worker.stop()
            self.viewer_worker = None

        if self.window():
            self.window().showNormal()

        self.video_display.update_fullscreen_state(False)
        self.lbl_status.setText("")
        self.video_display.hide()
        self.top_bar_widget.show()
        self.card_connect.show()
        self.btn_connect.setEnabled(True)


# --- Janela Principal ---

class MainWindow(QMainWindow):
    """Janela Principal com QStackedWidget."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("StreamShare — Transmissão de Tela LAN")
        self.resize(1024, 680)

        self.stacked_widget = QStackedWidget()
        self.setCentralWidget(self.stacked_widget)

        # Instancia Telas
        self.screen_home = HomeScreen(
            on_host_clicked=self.show_host,
            on_viewer_clicked=self.show_viewer,
        )
        self.screen_host = HostWidget(on_back_clicked=self.show_home)
        self.screen_viewer = ViewerWidget(on_back_clicked=self.show_home)

        self.stacked_widget.addWidget(self.screen_home)
        self.stacked_widget.addWidget(self.screen_host)
        self.stacked_widget.addWidget(self.screen_viewer)

        self.show_home()

    def show_home(self):
        self.stacked_widget.setCurrentWidget(self.screen_home)

    def show_host(self):
        self.screen_host.refresh_monitors()
        self.stacked_widget.setCurrentWidget(self.screen_host)

    def show_viewer(self):
        self.stacked_widget.setCurrentWidget(self.screen_viewer)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_STYLE)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
