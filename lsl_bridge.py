"""
lsl_bridge.py
────────────────────────
PySide6 GUI — reads ADS1115 rowing-power data from an Arduino Micro over
serial and broadcasts it as a LabStreamingLayer stream.

Dependencies:
    pip install PySide6 pyserial pylsl

Usage:
    python arduino_lsl_bridge_qt.py
"""

import sys
import threading
from collections import deque

import serial
import serial.tools.list_ports
from pylsl import StreamInfo, StreamOutlet, cf_int16

from PySide6.QtCore    import Qt, Signal, QObject, QRect
from PySide6.QtGui     import (QColor, QFont, QPainter, QPen,
                                QPainterPath)
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow,
    QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QComboBox, QPushButton,
    QFrame, QSizePolicy, QMessageBox, QTextEdit, QGroupBox
)

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 120
BAUD_RATE     = 500_000
CHANNEL_COUNT = 1
CHANNEL_NAME  = "RowingPower"
WAVEFORM_LEN  = 360   # ~3 s at 120 Hz

# ── Stylesheet ────────────────────────────────────────────────────────────────
STYLESHEET = """
    QMainWindow, QWidget {
        background: #1a1d23;
        color: #d4d8e0;
        font-family: 'Segoe UI', 'Helvetica Neue', sans-serif;
        font-size: 13px;
    }
    QGroupBox {
        border: 1px solid #2e3340;
        border-radius: 6px;
        margin-top: 10px;
        padding-top: 8px;
        font-weight: 600;
        color: #7a8299;
        font-size: 11px;
        letter-spacing: 1px;
        text-transform: uppercase;
    }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; }

    QPushButton {
        background: #252930;
        border: 1px solid #353a47;
        border-radius: 5px;
        padding: 7px 18px;
        color: #d4d8e0;
    }
    QPushButton:hover   { background: #2e3340; border-color: #4a8fe8; }
    QPushButton:pressed { background: #1f2229; }
    QPushButton:disabled { color: #444; border-color: #2a2d35; }

    QPushButton#primary {
        background: #2563c7;
        border-color: #3b7be0;
        color: #ffffff;
        font-weight: 600;
    }
    QPushButton#primary:hover    { background: #3071d9; }
    QPushButton#primary:disabled { background: #1c3a6e; color: #5a7aaa; }

    QPushButton#danger {
        background: #7a1f1f;
        border-color: #a83232;
        color: #ffaaaa;
        font-weight: 600;
    }
    QPushButton#danger:hover    { background: #8f2525; }
    QPushButton#danger:disabled { background: #2e1a1a; color: #5a3333; }

    QLineEdit, QComboBox {
        background: #1e2128;
        border: 1px solid #2e3340;
        border-radius: 5px;
        padding: 6px 10px;
        color: #d4d8e0;
        selection-background-color: #2563c7;
    }
    QLineEdit:focus, QComboBox:focus { border-color: #4a8fe8; }
    QComboBox::drop-down { border: none; width: 24px; }
    QComboBox QAbstractItemView {
        background: #1e2128;
        border: 1px solid #2e3340;
        selection-background-color: #2563c7;
    }

    QTextEdit {
        background: #12141a;
        border: 1px solid #2e3340;
        border-radius: 5px;
        font-family: 'Cascadia Code', 'Consolas', monospace;
        font-size: 12px;
        color: #8fba8f;
    }

    QLabel#hint    { color: #555c70; font-size: 12px; }
    QLabel#accent  { color: #4a8fe8; }
    QLabel#stat    { color: #7a8299; font-size: 12px;
                     font-family: 'Cascadia Code', 'Consolas', monospace; }
    QLabel#bigval  {
        color: #4a8fe8;
        font-family: 'Cascadia Code', 'Consolas', monospace;
        font-size: 26px;
        font-weight: bold;
    }
    QFrame#divider { background: #2e3340; }
"""


# ── Thread-safe signal relay ──────────────────────────────────────────────────
class WorkerSignals(QObject):
    status  = Signal(str)
    sample  = Signal(int)
    stopped = Signal()


# ── Serial → LSL worker thread ────────────────────────────────────────────────
class BridgeWorker(threading.Thread):
    def __init__(self, port: str, stream_name: str, signals: WorkerSignals):
        super().__init__(daemon=True)
        self.port         = port
        self.stream_name  = stream_name
        self.signals      = signals
        self._stop_event  = threading.Event()
        self.sample_count = 0

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            ser = serial.Serial(self.port, BAUD_RATE, timeout=2)
        except serial.SerialException as exc:
            self.signals.status.emit(f"Serial error: {exc}")
            self.signals.stopped.emit()
            return

        info = StreamInfo(
            name           = self.stream_name,
            type           = "Rowing",
            channel_count  = CHANNEL_COUNT,
            nominal_srate  = SAMPLE_RATE,
            channel_format = cf_int16,
            source_id      = f"ArduinoMicro_{self.port}",
        )
        chans = info.desc().append_child("channels")
        ch    = chans.append_child("channel")
        ch.append_child_value("label", CHANNEL_NAME)
        ch.append_child_value("unit",  "ADS1115_raw")
        ch.append_child_value("type",  "Rowing")

        outlet = StreamOutlet(info)
        self.signals.status.emit(
            f"Streaming  ·  {self.stream_name}  →  LSL @ {SAMPLE_RATE} Hz"
        )

        ser.reset_input_buffer()
        ser.readline()   # discard first partial line

        while not self._stop_event.is_set():
            try:
                raw = ser.readline()
            except serial.SerialException as exc:
                self.signals.status.emit(f"Read error: {exc}")
                break
            if not raw:
                continue
            try:
                value = int(raw.decode("ascii", errors="ignore").strip())
            except ValueError:
                continue
            outlet.push_sample([value])
            self.sample_count += 1
            self.signals.sample.emit(value)

        ser.close()
        self.signals.status.emit("Stopped.")
        self.signals.stopped.emit()


# ── Waveform strip ────────────────────────────────────────────────────────────
class WaveformWidget(QWidget):
    """Scrolling signal strip that matches the dark panel aesthetic."""

    _C_BG    = QColor("#12141a")
    _C_GRID  = QColor("#1e2128")
    _C_TRACE = QColor("#4a8fe8")
    _C_GLOW  = QColor(74, 143, 232, 30)
    _C_IDLE  = QColor("#2e3340")
    _C_LABEL = QColor("#555c70")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._buf: deque[int] = deque(maxlen=WAVEFORM_LEN)
        self._running = False

    def push(self, value: int):
        self._buf.append(value)
        self.update()

    def set_running(self, running: bool):
        self._running = running
        self.update()

    def clear(self):
        self._buf.clear()
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, self._C_BG)

        # Subtle grid
        p.setPen(QPen(self._C_GRID, 1))
        for row in range(1, 4):
            y = int(h * row / 4)
            p.drawLine(0, y, w, y)
        for col in range(1, 9):
            x = int(w * col / 9)
            p.drawLine(x, 0, x, h)

        if len(self._buf) < 2:
            p.setPen(QPen(self._C_LABEL))
            p.setFont(QFont("Cascadia Code", 9))
            p.drawText(QRect(0, 0, w, h), Qt.AlignCenter, "awaiting signal …")
            return

        samples = list(self._buf)
        mn, mx  = min(samples), max(samples)
        span    = mx - mn or 1
        margin  = 6
        dx      = w / (WAVEFORM_LEN - 1)
        offset  = WAVEFORM_LEN - len(samples)

        def to_y(v):
            return margin + int((1.0 - (v - mn) / span) * (h - 2 * margin))

        path = QPainterPath()
        path.moveTo(offset * dx, to_y(samples[0]))
        for i, v in enumerate(samples[1:], 1):
            path.lineTo((offset + i) * dx, to_y(v))

        # Soft glow pass
        p.setPen(QPen(self._C_GLOW, 7))
        p.drawPath(path)
        # Main trace
        colour = self._C_TRACE if self._running else self._C_IDLE
        p.setPen(QPen(colour, 1.5))
        p.drawPath(path)
        p.end()


# ── Main window ───────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self._worker: BridgeWorker | None = None
        self._sample_count = 0
        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────────────────────
    def _setup_ui(self):
        self.setWindowTitle("Rowing → LSL Bridge")
        self.setMinimumSize(500, 570)
        self.setStyleSheet(STYLESHEET)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        # Title
        title = QLabel("Rowing  →  LSL")
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Light))
        title.setObjectName("accent")
        title.setStyleSheet("color: #4a8fe8; letter-spacing: 1px;")
        layout.addWidget(title)

        subtitle = QLabel(
            f"ADS1115 differential  ·  {SAMPLE_RATE} Hz nominal  ·  stream type: Rowing"
        )
        subtitle.setObjectName("hint")
        layout.addWidget(subtitle)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFixedHeight(1)
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        # ── Configuration ──────────────────────────────────────────────────
        cfg_group = QGroupBox("Configuration")
        cfg_layout = QVBoxLayout(cfg_group)
        cfg_layout.setSpacing(10)

        port_row = QHBoxLayout()
        port_lbl = QLabel("COM Port")
        port_lbl.setObjectName("hint")
        port_lbl.setFixedWidth(100)
        self._port_combo = QComboBox()
        self._port_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._refresh_btn = QPushButton("⟳  Refresh")
        self._refresh_btn.setFixedWidth(100)
        self._refresh_btn.clicked.connect(self._refresh_ports)
        port_row.addWidget(port_lbl)
        port_row.addWidget(self._port_combo)
        port_row.addWidget(self._refresh_btn)
        cfg_layout.addLayout(port_row)

        name_row = QHBoxLayout()
        name_lbl = QLabel("Stream Name")
        name_lbl.setObjectName("hint")
        name_lbl.setFixedWidth(100)
        self._name_edit = QLineEdit("RowSimPower")
        name_row.addWidget(name_lbl)
        name_row.addWidget(self._name_edit)
        cfg_layout.addLayout(name_row)

        layout.addWidget(cfg_group)

        # ── Live signal ────────────────────────────────────────────────────
        live_group = QGroupBox("Live Signal")
        live_layout = QVBoxLayout(live_group)
        live_layout.setSpacing(8)

        self._waveform = WaveformWidget()
        live_layout.addWidget(self._waveform)

        stats_row = QHBoxLayout()
        self._value_label = QLabel("—")
        self._value_label.setObjectName("bigval")
        self._count_label = QLabel("0 samples received")
        self._count_label.setObjectName("stat")
        self._count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        stats_row.addWidget(self._value_label)
        stats_row.addWidget(self._count_label)
        live_layout.addLayout(stats_row)

        layout.addWidget(live_group)

        # ── Log ────────────────────────────────────────────────────────────
        self._log_edit = QTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setFixedHeight(120)
        layout.addWidget(self._log_edit)

        # ── Buttons ────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._start_btn = QPushButton("Start Stream")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self._start)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("danger")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)

        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._stop_btn)
        layout.addLayout(btn_row)

        self._refresh_ports()

    # ── Port discovery ────────────────────────────────────────────────────────
    def _refresh_ports(self):
        current = self._port_combo.currentText()
        self._port_combo.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if ports:
            self._port_combo.addItems(ports)
            if current in ports:
                self._port_combo.setCurrentText(current)
            self._log_msg("Ports: " + ", ".join(ports))
        else:
            self._log_msg("No COM ports found — check connection.", error=True)

    # ── Stream control ────────────────────────────────────────────────────────
    def _start(self):
        port = self._port_combo.currentText().strip()
        name = self._name_edit.text().strip()
        if not port:
            QMessageBox.warning(self, "No port", "Please select a COM port.")
            return
        if not name:
            QMessageBox.warning(self, "No name", "Please enter a stream name.")
            return

        self._sample_count = 0
        self._waveform.clear()
        self._waveform.set_running(True)
        self._value_label.setText("—")
        self._count_label.setText("0 samples received")

        signals = WorkerSignals()
        signals.status.connect(self._log_msg)
        signals.sample.connect(self._on_sample)
        signals.stopped.connect(self._on_stopped)

        self._worker = BridgeWorker(port, name, signals)
        self._worker.start()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._port_combo.setEnabled(False)
        self._name_edit.setEnabled(False)
        self._refresh_btn.setEnabled(False)
        self._log_msg(f"Connecting to {port} at {BAUD_RATE:,} baud …")

    def _stop(self):
        if self._worker:
            self._worker.stop()

    def _on_stopped(self):
        self._worker = None
        self._waveform.set_running(False)
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._port_combo.setEnabled(True)
        self._name_edit.setEnabled(True)
        self._refresh_btn.setEnabled(True)

    # ── Sample callback ───────────────────────────────────────────────────────
    def _on_sample(self, value: int):
        self._sample_count += 1
        self._waveform.push(value)
        self._value_label.setText(f"{value:,}")
        self._count_label.setText(f"{self._sample_count:,} samples received")

    # ── Log ───────────────────────────────────────────────────────────────────
    def _log_msg(self, msg: str, error: bool = False):
        colour = "#e05555" if error else "#8fba8f"
        self._log_edit.append(f'<span style="color:{colour}">{msg}</span>')
        sb = self._log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Close ─────────────────────────────────────────────────────────────────
    def closeEvent(self, event):
        if self._worker:
            self._worker.stop()
        event.accept()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())