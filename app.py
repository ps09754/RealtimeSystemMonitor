import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import psutil
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

try:
    import GPUtil
except Exception:
    GPUtil = None


APP_NAME = "Device Monitor HUD"
CONFIG_DIR = Path.home() / ".device-monitor-hud"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass(frozen=True)
class MetricDef:
    key: str
    label: str
    color: str


@dataclass
class Reading:
    text: str
    value: Optional[float] = None


METRICS: List[MetricDef] = [
    MetricDef("cpu_usage", "CPU", "#ff6b6b"),
    MetricDef("cpu_temp", "CPU T", "#ff9966"),
    MetricDef("cpu_power", "CPU W", "#ffcc66"),
    MetricDef("gpu_usage", "GPU", "#6cff6c"),
    MetricDef("gpu_temp", "GPU T", "#6cd3ff"),
    MetricDef("gpu_power", "GPU W", "#67b7ff"),
    MetricDef("ram", "RAM", "#8fb1ff"),
    MetricDef("vram", "VRAM", "#ffa64d"),
    MetricDef("ssd", "SSD", "#dddddd"),
    MetricDef("fan", "FAN", "#cccccc"),
]

DEFAULT_CONFIG = {
    "theme": "windows",
    "refresh_ms": 1000,
    "overlay_opacity": 0.92,
    "overlay_font_size": 16,
    "always_on_top": True,
    "metrics": {m.key: {"show": True, "measure": True} for m in METRICS},
    "mac_bar_width": 980,
    "mac_bar_height": 30,
    "windows_panel_width": 260,
}


class ConfigStore:
    def __init__(self):
        self.data = self._load()

    def _load(self) -> Dict:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            loaded = json.loads(CONFIG_PATH.read_text())
        except Exception:
            return json.loads(json.dumps(DEFAULT_CONFIG))

        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        legacy_show = loaded.get("show") if isinstance(loaded, dict) else None
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        if isinstance(legacy_show, dict):
            for key, value in legacy_show.items():
                if key in merged.get("metrics", {}):
                    merged["metrics"][key]["show"] = bool(value)
        return merged

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.data, indent=2))


class MetricsCollector:
    def __init__(self):
        self._last_disk = psutil.disk_io_counters()
        self._last_time = time.time()

    def sample(self, enabled: Dict[str, bool]) -> Dict[str, Reading]:
        readings: Dict[str, Reading] = {key: Reading("OFF") for key in enabled}

        now = time.time()
        elapsed = max(now - self._last_time, 1e-6)

        want_cpu = enabled.get("cpu_usage") or enabled.get("cpu_temp") or enabled.get("cpu_power")
        want_gpu = enabled.get("gpu_usage") or enabled.get("gpu_temp") or enabled.get("gpu_power") or enabled.get("vram")
        want_ram = enabled.get("ram")
        want_ssd = enabled.get("ssd")
        want_fan = enabled.get("fan")

        cpu_percent = psutil.cpu_percent(interval=None) if enabled.get("cpu_usage") else None
        if cpu_percent is not None:
            readings["cpu_usage"] = Reading(f"{cpu_percent:.0f}%", cpu_percent)

        cpu_temp = None
        if enabled.get("cpu_temp"):
            try:
                temps = psutil.sensors_temperatures(fahrenheit=False)
            except Exception:
                temps = None
            if temps:
                for entries in temps.values():
                    for entry in entries:
                        if entry.current:
                            cpu_temp = entry.current
                            break
                    if cpu_temp:
                        break
            readings["cpu_temp"] = Reading(f"{cpu_temp:.0f} C" if cpu_temp else "N/A")

        if enabled.get("cpu_power"):
            readings["cpu_power"] = Reading("N/A")

        if want_ram:
            mem = psutil.virtual_memory()
            readings["ram"] = Reading(f"{format_bytes(mem.used)} / {format_bytes(mem.total)}")

        if want_ssd:
            disk_path = "C:\\" if os.name == "nt" else "/"
            disk = psutil.disk_usage(disk_path)
            disk_io = psutil.disk_io_counters()
            delta_read = disk_io.read_bytes - self._last_disk.read_bytes
            delta_write = disk_io.write_bytes - self._last_disk.write_bytes
            disk_speed = format_bytes((delta_read + delta_write) / elapsed) + "/s"
            self._last_disk = disk_io
            readings["ssd"] = Reading(
                f"{format_bytes(disk.used)} / {format_bytes(disk.total)} ({disk_speed})"
            )

        gpu_usage = None
        gpu_temp = None
        gpu_power = None
        gpu_mem = None
        gpu_mem_total = None
        gpu_fan = None

        if GPUtil and want_gpu:
            try:
                gpus = GPUtil.getGPUs()
                if gpus:
                    g = gpus[0]
                    if enabled.get("gpu_usage"):
                        gpu_usage = g.load * 100.0
                    if enabled.get("gpu_temp"):
                        gpu_temp = g.temperature if g.temperature else None
                    if enabled.get("gpu_power"):
                        gpu_power = getattr(g, "powerDraw", None)
                    if enabled.get("vram"):
                        gpu_mem = g.memoryUsed
                        gpu_mem_total = g.memoryTotal
                    if enabled.get("fan"):
                        if g.fanSpeed is not None:
                            gpu_fan = g.fanSpeed * 100.0
            except Exception:
                pass

        if enabled.get("gpu_usage"):
            readings["gpu_usage"] = Reading(f"{gpu_usage:.0f}%" if gpu_usage is not None else "N/A", gpu_usage)
        if enabled.get("gpu_temp"):
            readings["gpu_temp"] = Reading(f"{gpu_temp:.0f} C" if gpu_temp is not None else "N/A")
        if enabled.get("gpu_power"):
            readings["gpu_power"] = Reading(
                f"{gpu_power:.0f} W" if gpu_power is not None else "N/A"
            )
        if enabled.get("vram"):
            readings["vram"] = Reading(
                f"{gpu_mem:.0f} MB / {gpu_mem_total:.0f} MB"
                if gpu_mem is not None and gpu_mem_total is not None
                else "N/A"
            )

        if want_fan:
            fan_rpm = None
            try:
                fans = psutil.sensors_fans()
            except Exception:
                fans = None
            if fans:
                rpm_values = []
                for entries in fans.values():
                    for entry in entries:
                        if entry.current:
                            rpm_values.append(entry.current)
                if rpm_values:
                    fan_rpm = sum(rpm_values) / len(rpm_values)
            if fan_rpm is not None:
                readings["fan"] = Reading(f"{fan_rpm:.0f} RPM")
            elif gpu_fan is not None:
                readings["fan"] = Reading(f"{gpu_fan:.0f}%")
            else:
                readings["fan"] = Reading("N/A")

        self._last_time = now
        return readings


class MetricRow(QtWidgets.QWidget):
    def __init__(self, label: str, color: str, font: QtGui.QFont, parent=None):
        super().__init__(parent)
        self.label = QtWidgets.QLabel(label)
        self.value = QtWidgets.QLabel("...")

        self.label.setFont(font)
        self.value.setFont(font)
        self.label.setStyleSheet(f"color: {color};")
        self.value.setStyleSheet("color: #f5f5f5;")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.label, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.value, 0, QtCore.Qt.AlignmentFlag.AlignRight)


class MetricChip(QtWidgets.QWidget):
    def __init__(self, label: str, color: str, font: QtGui.QFont, parent=None):
        super().__init__(parent)
        self.label = QtWidgets.QLabel(label)
        self.value = QtWidgets.QLabel("...")

        self.label.setFont(font)
        self.value.setFont(font)
        self.label.setStyleSheet(f"color: {color};")
        self.value.setStyleSheet("color: #f5f5f5;")

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(6)
        layout.addWidget(self.label, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.value, 0, QtCore.Qt.AlignmentFlag.AlignRight)


class OverlayWindow(QtWidgets.QWidget):
    def __init__(self, config: ConfigStore, collector: MetricsCollector):
        super().__init__()
        self.config = config
        self.collector = collector
        self._rows: Dict[str, QtWidgets.QLabel] = {}

        self.setWindowTitle(APP_NAME)
        self._apply_window_flags()
        self._build_ui()
        self._apply_geometry()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.update_metrics)
        self.timer.start(self.config.data["refresh_ms"])

        self.update_metrics()

    def _apply_window_flags(self):
        flags = (
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.Tool
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowFlags(flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowOpacity(self.config.data.get("overlay_opacity", 0.9))

    def _build_ui(self):
        for child in self.findChildren(QtWidgets.QWidget):
            child.setParent(None)

        theme = self.config.data.get("theme", "windows")
        font_size = self.config.data.get("overlay_font_size", 16)
        font = QtGui.QFont("Menlo" if theme == "mac" else "Consolas", font_size)

        container = QtWidgets.QWidget(self)
        if theme == "mac":
            layout = QtWidgets.QHBoxLayout(container)
        else:
            layout = QtWidgets.QVBoxLayout(container)

        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6 if theme == "mac" else 4)

        self._rows = {}
        for metric in METRICS:
            if not self.config.data["metrics"].get(metric.key, {}).get("show", True):
                continue
            widget = MetricChip(metric.label, metric.color, font, container) if theme == "mac" else MetricRow(
                metric.label, metric.color, font, container
            )
            self._rows[metric.key] = widget.value
            layout.addWidget(widget)

        overlay = QtWidgets.QVBoxLayout(self)
        overlay.setContentsMargins(0, 0, 0, 0)
        overlay.addWidget(container)

        self.setLayout(overlay)
        self._apply_stylesheet(theme)

    def _apply_stylesheet(self, theme: str):
        if theme == "mac":
            self.setStyleSheet(
                "QWidget {"
                "background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
                "stop:0 rgba(15,15,15,200), stop:1 rgba(40,40,40,200));"
                "border-radius: 8px;"
                "}"
            )
        else:
            self.setStyleSheet(
                "QWidget {"
                "background: rgba(8,8,8,210);"
                "border-radius: 6px;"
                "}"
            )

    def _apply_geometry(self):
        theme = self.config.data.get("theme", "windows")
        screen = QtGui.QGuiApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1280, 720)
        margin = 12
        x = geo.x() + margin
        y = geo.y() + margin
        if theme == "mac":
            width = self.config.data.get("mac_bar_width", 980)
            height = self.config.data.get("mac_bar_height", 30)
        else:
            width = self.config.data.get("windows_panel_width", 260)
            height = self.sizeHint().height()
        self.setGeometry(x, y, width, height)

    def update_metrics(self):
        enabled = {k: v.get("measure", True) for k, v in self.config.data["metrics"].items()}
        data = self.collector.sample(enabled)
        for key, label in self._rows.items():
            label.setText(data.get(key, Reading("N/A")).text)
        self.adjustSize()
        self.raise_()

    def refresh_ui(self):
        self._apply_window_flags()
        self._build_ui()
        self._apply_geometry()
        self.timer.setInterval(self.config.data["refresh_ms"])


class ControlWindow(QtWidgets.QWidget):
    def __init__(self, config: ConfigStore, overlay: OverlayWindow, collector: MetricsCollector):
        super().__init__()
        self.config = config
        self.overlay = overlay
        self.collector = collector

        self.setWindowTitle("Device Monitor Controller")
        self.resize(920, 620)
        self.setObjectName("control-root")

        self._build_ui()
        self._start_timer()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        header = QtWidgets.QLabel("Device Monitor Controller")
        header.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(header)

        content = QtWidgets.QHBoxLayout()
        layout.addLayout(content, 1)

        left = QtWidgets.QVBoxLayout()
        right = QtWidgets.QVBoxLayout()
        content.addLayout(left, 3)
        content.addLayout(right, 2)

        overlay_group = QtWidgets.QGroupBox("Overlay Settings")
        overlay_form = QtWidgets.QFormLayout(overlay_group)

        self.theme = QtWidgets.QComboBox()
        self.theme.addItems(["windows", "mac"])
        self.theme.setCurrentText(self.config.data.get("theme", "windows"))
        overlay_form.addRow("Theme", self.theme)

        self.refresh = QtWidgets.QSpinBox()
        self.refresh.setRange(200, 5000)
        self.refresh.setValue(self.config.data.get("refresh_ms", 1000))
        overlay_form.addRow("Refresh (ms)", self.refresh)

        self.font_size = QtWidgets.QSpinBox()
        self.font_size.setRange(10, 32)
        self.font_size.setValue(self.config.data.get("overlay_font_size", 16))
        overlay_form.addRow("Font Size", self.font_size)

        self.opacity = QtWidgets.QDoubleSpinBox()
        self.opacity.setRange(0.3, 1.0)
        self.opacity.setSingleStep(0.05)
        self.opacity.setValue(self.config.data.get("overlay_opacity", 0.92))
        overlay_form.addRow("Opacity", self.opacity)

        left.addWidget(overlay_group)

        metrics_group = QtWidgets.QGroupBox("Metrics (Measure / Show)")
        metrics_layout = QtWidgets.QVBoxLayout(metrics_group)

        self.metrics_table = QtWidgets.QTableWidget(0, 3)
        self.metrics_table.setHorizontalHeaderLabels(["Metric", "Measure", "Show"])
        self.metrics_table.verticalHeader().setVisible(False)
        self.metrics_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.metrics_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.metrics_table.horizontalHeader().setStretchLastSection(True)
        self.metrics_table.setColumnWidth(0, 200)
        self.metrics_table.setColumnWidth(1, 80)
        self.metrics_table.setColumnWidth(2, 80)

        self.metrics_table.setRowCount(len(METRICS))
        for row, metric in enumerate(METRICS):
            config = self.config.data["metrics"].get(metric.key, {"show": True, "measure": True})
            label_item = QtWidgets.QTableWidgetItem(metric.label)
            self.metrics_table.setItem(row, 0, label_item)

            measure_item = QtWidgets.QTableWidgetItem()
            measure_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            measure_item.setCheckState(
                QtCore.Qt.CheckState.Checked if config.get("measure", True) else QtCore.Qt.CheckState.Unchecked
            )
            self.metrics_table.setItem(row, 1, measure_item)

            show_item = QtWidgets.QTableWidgetItem()
            show_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            show_item.setCheckState(
                QtCore.Qt.CheckState.Checked if config.get("show", True) else QtCore.Qt.CheckState.Unchecked
            )
            self.metrics_table.setItem(row, 2, show_item)

        metrics_layout.addWidget(self.metrics_table)
        left.addWidget(metrics_group, 1)

        button_row = QtWidgets.QHBoxLayout()
        self.apply_show = QtWidgets.QPushButton("Apply + Show Overlay")
        self.hide_overlay = QtWidgets.QPushButton("Hide Overlay")
        button_row.addWidget(self.apply_show)
        button_row.addWidget(self.hide_overlay)
        left.addLayout(button_row)

        graphs_group = QtWidgets.QGroupBox("Live Graphs")
        graphs_layout = QtWidgets.QVBoxLayout(graphs_group)

        self.cpu_plot = pg.PlotWidget()
        self.cpu_plot.setBackground((18, 18, 18))
        self.cpu_plot.setYRange(0, 100)
        self.cpu_plot.setTitle("CPU Usage %", color="#ffffff", size="10pt")
        self.cpu_curve = self.cpu_plot.plot(pen=pg.mkPen(color="#ff6b6b", width=2))

        self.gpu_plot = pg.PlotWidget()
        self.gpu_plot.setBackground((18, 18, 18))
        self.gpu_plot.setYRange(0, 100)
        self.gpu_plot.setTitle("GPU Usage %", color="#ffffff", size="10pt")
        self.gpu_curve = self.gpu_plot.plot(pen=pg.mkPen(color="#6cff6c", width=2))

        graphs_layout.addWidget(self.cpu_plot)
        graphs_layout.addWidget(self.gpu_plot)
        right.addWidget(graphs_group, 1)

        self.apply_show.clicked.connect(self.apply_changes)
        self.hide_overlay.clicked.connect(self.overlay.hide)

    def _start_timer(self):
        self.history_len = 120
        self.cpu_history = [0.0] * self.history_len
        self.gpu_history = [0.0] * self.history_len

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._update_graphs)
        self.timer.start(1000)

    def _update_graphs(self):
        enabled = {"cpu_usage": True, "gpu_usage": True}
        data = self.collector.sample(enabled)
        cpu_val = data.get("cpu_usage", Reading("", 0)).value or 0.0
        gpu_val = data.get("gpu_usage", Reading("", 0)).value or 0.0

        self.cpu_history = (self.cpu_history + [cpu_val])[-self.history_len :]
        self.gpu_history = (self.gpu_history + [gpu_val])[-self.history_len :]

        self.cpu_curve.setData(self.cpu_history)
        self.gpu_curve.setData(self.gpu_history)

    def apply_changes(self):
        self.config.data["theme"] = self.theme.currentText()
        self.config.data["refresh_ms"] = self.refresh.value()
        self.config.data["overlay_font_size"] = self.font_size.value()
        self.config.data["overlay_opacity"] = self.opacity.value()

        for row, metric in enumerate(METRICS):
            measure_state = self.metrics_table.item(row, 1).checkState() == QtCore.Qt.CheckState.Checked
            show_state = self.metrics_table.item(row, 2).checkState() == QtCore.Qt.CheckState.Checked
            self.config.data["metrics"][metric.key] = {"measure": measure_state, "show": show_state}

        self.config.save()
        self.overlay.refresh_ui()
        self.overlay.show()
        self.overlay.raise_()


def format_bytes(value: float) -> str:
    if value is None:
        return "N/A"
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(
        """
        #control-root {
            background: #151515;
        }
        QLabel {
            color: #e6e6e6;
        }
        QGroupBox {
            border: 1px solid #2e2e2e;
            border-radius: 8px;
            margin-top: 12px;
            padding: 6px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 12px;
            padding: 0 6px;
            color: #d8d8d8;
        }
        QTableWidget {
            background: #101010;
            gridline-color: #2c2c2c;
        }
        QHeaderView::section {
            background: #1f1f1f;
            color: #dcdcdc;
            padding: 6px;
            border: 1px solid #2b2b2b;
        }
        QPushButton {
            background: #2a2a2a;
            border: 1px solid #3b3b3b;
            padding: 6px 12px;
            border-radius: 6px;
        }
        QPushButton:hover {
            background: #353535;
        }
        QComboBox, QSpinBox, QDoubleSpinBox {
            background: #1b1b1b;
            border: 1px solid #333333;
            border-radius: 4px;
            padding: 4px 6px;
        }
        """
    )

    config = ConfigStore()
    collector = MetricsCollector()
    overlay = OverlayWindow(config, collector)
    control = ControlWindow(config, overlay, collector)

    overlay.show()
    overlay.raise_()
    control.show()

    if platform.system() == "Darwin":
        overlay.raise_()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
