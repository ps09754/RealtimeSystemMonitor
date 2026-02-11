"""
Realtime System Monitor (macOS menu bar + settings UI)
- PySide6 for settings UI
- psutil for system stats
- pyqtgraph for realtime chart in settings
- PyObjC (Cocoa) for menu bar text
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import plistlib
import threading
import re
import getpass
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

import psutil
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

# Cocoa (PyObjC)
from AppKit import (
    NSAttributedString,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageOnly,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import (
    NSObject,
    NSURL,
    NSURLVolumeAvailableCapacityForImportantUsageKey,
    NSURLVolumeAvailableCapacityForOpportunisticUsageKey,
    NSURLVolumeAvailableCapacityKey,
    NSURLVolumeTotalCapacityKey,
)
import objc


APP_NAME = "Realtime System Monitor"
CONFIG_DIR = Path.home() / ".realtime-system-monitor"
CONFIG_PATH = CONFIG_DIR / "config.json"
LAUNCH_AGENT_LABEL = "com.realtime.system.monitor"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

SMART_REQUIRED = True
SUDOERS_FILE = "/etc/sudoers.d/realtime-system-monitor"
ALLOW_ADMIN_PROMPT = False

DEFAULT_CONFIG = {
    "update_ms": 500,
    "start_at_login": False,
    "hide_dock": False,
    "show": {
        "cpu": True,
        "gpu": True,
        "ram": True,
        "disk": True,
        "net": True,
        "chart": True,
    },
}


@dataclass
class SystemSample:
    cpu_percent: float
    gpu_device_percent: float | None
    gpu_render_percent: float | None
    gpu_tiler_percent: float | None
    ram_percent: float
    ram_used: int
    ram_available: int
    ram_total: int
    disk_read_bps: float
    disk_write_bps: float
    net_up_bps: float
    net_down_bps: float


class ConfigStore:
    """JSON config store for settings."""

    def __init__(self) -> None:
        self.data = self._load()

    def _load(self) -> Dict:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            parsed = json.loads(CONFIG_PATH.read_text())
        except Exception:
            return json.loads(json.dumps(DEFAULT_CONFIG))

        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        for key, value in parsed.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(self.data, indent=2))


class SystemStats:
    """Collects system metrics without blocking the UI thread."""

    def __init__(self) -> None:
        self._last_disk = psutil.disk_io_counters()
        self._last_net = psutil.net_io_counters(pernic=True)
        self._net_iface = None
        self._last_time = time.time()
        self._gpu_device = None
        self._gpu_render = None
        self._gpu_tiler = None
        self._gpu_lock = threading.Lock()
        psutil.cpu_percent(interval=None)
        self._start_gpu_sampler()

    def _start_gpu_sampler(self) -> None:
        thread = threading.Thread(target=self._gpu_loop, daemon=True)
        thread.start()

    def _gpu_loop(self) -> None:
        while True:
            metrics = read_gpu_metrics()
            with self._gpu_lock:
                self._gpu_device = metrics.get("device")
                self._gpu_render = metrics.get("render")
                self._gpu_tiler = metrics.get("tiler")
            time.sleep(2.0)

    def _pick_net_iface(self, pernic) -> str | None:
        ignore_prefixes = ("lo", "awdl", "llw", "utun", "bridge", "p2p", "gif", "stf", "ap")
        best_iface = None
        best_bytes = -1
        for name, stats in pernic.items():
            if name.startswith(ignore_prefixes):
                continue
            total = stats.bytes_recv + stats.bytes_sent
            if total > best_bytes:
                best_bytes = total
                best_iface = name
        return best_iface

    def sample(self) -> SystemSample:
        now = time.time()
        elapsed = max(now - self._last_time, 1e-6)

        cpu_percent = psutil.cpu_percent(interval=None)
        with self._gpu_lock:
            gpu_device = self._gpu_device
            gpu_render = self._gpu_render
            gpu_tiler = self._gpu_tiler

        mem = psutil.virtual_memory()
        ram_total = mem.total
        if sys.platform == "darwin":
            ram_used, ram_available, ram_percent = get_macos_ram(ram_total, mem)
        else:
            ram_used = int(mem.total - mem.available)
            ram_available = int(mem.available)
            ram_percent = (ram_used / ram_total * 100.0) if ram_total else mem.percent

        disk = psutil.disk_io_counters()
        disk_read_bps = (disk.read_bytes - self._last_disk.read_bytes) / elapsed
        disk_write_bps = (disk.write_bytes - self._last_disk.write_bytes) / elapsed

        pernic = psutil.net_io_counters(pernic=True)
        if self._net_iface is None or self._net_iface not in pernic:
            self._net_iface = self._pick_net_iface(pernic)
        if self._net_iface and self._net_iface in pernic:
            net = pernic[self._net_iface]
            last = self._last_net.get(self._net_iface, net)
            net_up_bps = (net.bytes_sent - last.bytes_sent) / elapsed
            net_down_bps = (net.bytes_recv - last.bytes_recv) / elapsed
        else:
            net = psutil.net_io_counters()
            net_up_bps = (net.bytes_sent - sum(v.bytes_sent for v in self._last_net.values())) / elapsed
            net_down_bps = (net.bytes_recv - sum(v.bytes_recv for v in self._last_net.values())) / elapsed

        self._last_disk = disk
        self._last_net = pernic
        self._last_time = now

        return SystemSample(
            cpu_percent=cpu_percent,
            gpu_device_percent=gpu_device,
            gpu_render_percent=gpu_render,
            gpu_tiler_percent=gpu_tiler,
            ram_percent=ram_percent,
            ram_used=ram_used,
            ram_available=ram_available,
            ram_total=ram_total,
            disk_read_bps=disk_read_bps,
            disk_write_bps=disk_write_bps,
            net_up_bps=net_up_bps,
            net_down_bps=net_down_bps,
        )


class MetricsHub(QtCore.QObject):
    """Central timer that samples once and broadcasts to all UI consumers."""

    updated = QtCore.Signal(SystemSample)

    def __init__(self, stats: SystemStats, interval_ms: int) -> None:
        super().__init__()
        self.stats = stats
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(interval_ms)

    def _tick(self) -> None:
        self.updated.emit(self.stats.sample())

    def is_active(self) -> bool:
        return self.timer.isActive()


class LiveGuard(QtCore.QObject):
    """Ensures the hub keeps ticking when windows are shown."""

    def __init__(self, hub: MetricsHub) -> None:
        super().__init__()
        self.hub = hub
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poke)
        self.timer.start(2000)

    def _poke(self) -> None:
        if not self.hub.is_active():
            self.hub.set_interval(500)

    def set_interval(self, interval_ms: int) -> None:
        self.timer.setInterval(interval_ms)


class MenuActionHandler(NSObject):
    """Objective-C target for menu actions."""

    def initWithController_(self, controller):
        self = objc.super(MenuActionHandler, self).init()
        if self is None:
            return None
        self.controller = controller
        return self

    def openCpu_(self, _):
        self.controller.show_detail("cpu")

    def openRam_(self, _):
        self.controller.show_detail("ram")

    def openDisk_(self, _):
        self.controller.show_detail("disk")

    def openGpu_(self, _):
        self.controller.show_detail("gpu")

    def openNet_(self, _):
        self.controller.show_detail("net")

    def openSettings_(self, _):
        self.controller.open_settings()

    def quit_(self, _):
        QtWidgets.QApplication.quit()


class MetricStatusItem:
    """One metric shown as its own status item with vertical label/value."""

    def __init__(self, label: str, handler: MenuActionHandler, action: str, value_color: str | None = None) -> None:
        self.label = label
        self.value_color = value_color
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.button = self.item.button()
        if self.button is not None:
            self.button.setTitle_("")
            self.button.setImagePosition_(NSImageOnly)
            self.button.setTarget_(handler)
            self.button.setAction_(action)
        self.update_value("--")

    def set_visible(self, visible: bool) -> None:
        if self.button is not None:
            self.button.setHidden_(not visible)

    def update_value(self, value: str) -> None:
        if self.button is None:
            return
        image = make_metric_image(self.label, value, self.value_color)
        self.button.setImage_(image)
        self.button.setToolTip_(f"{self.label}: {value}")

    def update_net(self, up_value: str, down_value: str) -> None:
        if self.button is None:
            return
        image = make_net_image(up_value, down_value)
        self.button.setImage_(image)
        self.button.setToolTip_(f"{self.label}: ↑ {up_value}  ↓ {down_value}")

    def get_screen_rect(self):
        if self.button is None:
            return None, None
        window = self.button.window()
        if window is None:
            return None, None
        rect = window.convertRectToScreen_(self.button.frame())
        screen = window.screen()
        return rect, screen


class MenuBarController:
    """macOS menu bar status item with realtime text."""

    def __init__(self, config: ConfigStore, action_target) -> None:
        self.config = config
        self.action_target = action_target
        self.handler = MenuActionHandler.alloc().initWithController_(self.action_target)

        self.items = {
            "cpu": MetricStatusItem("CPU", self.handler, "openCpu:", "#ff5c5c"),
            "gpu": MetricStatusItem("GPU", self.handler, "openGpu:", "#7d7bff"),
            "ram": MetricStatusItem("RAM", self.handler, "openRam:", "#66d1ff"),
            "disk": MetricStatusItem("SSD", self.handler, "openDisk:", "#ff5ccf"),
            "net": MetricStatusItem("NET", self.handler, "openNet:"),
        }

        self.refresh_visibility()

    def refresh_visibility(self) -> None:
        show = self.config.data["show"]
        self.items["cpu"].set_visible(show.get("cpu", True))
        self.items["gpu"].set_visible(show.get("gpu", True))
        self.items["ram"].set_visible(show.get("ram", True))
        self.items["disk"].set_visible(show.get("disk", True))
        self.items["net"].set_visible(show.get("net", True))

    def update_from_sample(self, sample: SystemSample) -> None:
        show = self.config.data["show"]
        if show.get("cpu", True):
            self.items["cpu"].update_value(f"{sample.cpu_percent:.0f}%")
        if show.get("gpu", True):
            gpu_value = sample.gpu_device_percent
            if gpu_value is None:
                gpu_value = sample.gpu_render_percent
            gpu_text = f"{gpu_value:.0f}%" if gpu_value is not None else "N/A"
            self.items["gpu"].update_value(gpu_text)
        if show.get("ram", True):
            self.items["ram"].update_value(f"{sample.ram_percent:.0f}%")
        if show.get("disk", True):
            try:
                _, _, percent = get_disk_usage_info()
                self.items["disk"].update_value(f"{percent:.0f}%")
            except Exception:
                value = f"{format_rate_short(sample.disk_read_bps)}/{format_rate_short(sample.disk_write_bps)}"
                self.items["disk"].update_value(value)
        if show.get("net", True):
            up_value = format_rate_short(sample.net_up_bps)
            down_value = format_rate_short(sample.net_down_bps)
            self.items["net"].update_net(up_value, down_value)

    def get_anchor(self, metric_key: str):
        item = self.items.get(metric_key)
        if not item:
            return None
        rect, screen = item.get_screen_rect()
        if rect is None or screen is None:
            return None
        return {
            "x": rect.origin.x,
            "y": rect.origin.y,
            "w": rect.size.width,
            "h": rect.size.height,
            "screen_x": screen.frame().origin.x,
            "screen_y": screen.frame().origin.y,
            "screen_w": screen.frame().size.width,
            "screen_h": screen.frame().size.height,
        }


class MetricPage:
    """Single page UI for a metric group."""

    def __init__(
        self,
        title: str,
        main_label: str,
        chart_title: str,
        chart_color: str,
        detail_fields,
    ) -> None:
        self.widget = QtWidgets.QWidget()

        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.title = QtWidgets.QLabel(title)
        self.title.setObjectName("page-title")
        layout.addWidget(self.title)

        self.main_label = QtWidgets.QLabel(main_label)
        self.main_label.setObjectName("main-label")
        self.main_value = QtWidgets.QLabel("--")
        self.main_value.setObjectName("main-value")

        main_box = QtWidgets.QVBoxLayout()
        main_box.addWidget(self.main_label)
        main_box.addWidget(self.main_value)
        layout.addLayout(main_box)

        self.plot = pg.PlotWidget()
        self.plot.setBackground((20, 20, 20))
        self.plot.setYRange(0, 100)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.setTitle(chart_title, color="#cfcfcf", size="9pt")
        self.curve = self.plot.plot(pen=pg.mkPen(color=chart_color, width=2))
        style_plot(self.plot)
        self.plot.setFixedHeight(70)
        layout.addWidget(self.plot)

        self.details_group = QtWidgets.QGroupBox("Details")
        self.details_layout = QtWidgets.QFormLayout(self.details_group)
        self.details_layout.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        self.details_layout.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        self.details_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        self.detail_labels = {}
        for key, label in detail_fields:
            label_widget = QtWidgets.QLabel(label)
            label_widget.setObjectName("detail-label")
            label_widget.setStyleSheet("font-size: 10px;")
            label_widget.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label = QtWidgets.QLabel("--")
            value_label.setObjectName("detail-value")
            value_label.setStyleSheet("font-size: 10px;")
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            self.details_layout.addRow(label_widget, value_label)
            self.detail_labels[key] = value_label
        layout.addWidget(self.details_group)


class DiskPage:
    """Custom disk page to mimic the SSD panel layout."""

    def __init__(self) -> None:
        self.widget = QtWidgets.QWidget()

        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.title = QtWidgets.QLabel("Disk")
        self.title.setObjectName("page-title")
        layout.addWidget(self.title)

        header = QtWidgets.QHBoxLayout()
        self.volume_name = QtWidgets.QLabel("Macintosh HD")
        self.volume_name.setObjectName("disk-name")
        header.addWidget(self.volume_name)
        header.addStretch(1)
        self.disk_percent = QtWidgets.QLabel("--%")
        self.disk_percent.setObjectName("disk-percent")
        header.addWidget(self.disk_percent)
        layout.addLayout(header)

        self.read_label = QtWidgets.QLabel("0 MB/s")
        self.read_label.setObjectName("disk-read")
        self.write_label = QtWidgets.QLabel("0 MB/s")
        self.write_label.setObjectName("disk-write")
        layout.addWidget(self.read_label)
        layout.addWidget(self.write_label)

        self.plot_read = pg.PlotWidget()
        self.plot_read.setBackground((20, 20, 20))
        self.plot_read.showGrid(x=True, y=True, alpha=0.2)
        self.plot_read.setMouseEnabled(x=False, y=False)
        self.plot_read.hideButtons()
        self.plot_read.setMenuEnabled(False)
        self.plot_read.setTitle("Read", color="#cfcfcf", size="9pt")
        self.read_curve = self.plot_read.plot(pen=pg.mkPen(color="#ff5c5c", width=2))
        style_plot(self.plot_read)
        self.plot_read.setFixedHeight(70)
        layout.addWidget(self.plot_read)

        self.plot_write = pg.PlotWidget()
        self.plot_write.setBackground((20, 20, 20))
        self.plot_write.showGrid(x=True, y=True, alpha=0.2)
        self.plot_write.setMouseEnabled(x=False, y=False)
        self.plot_write.hideButtons()
        self.plot_write.setMenuEnabled(False)
        self.plot_write.setTitle("Write", color="#cfcfcf", size="9pt")
        self.write_curve = self.plot_write.plot(pen=pg.mkPen(color="#4aa3ff", width=2))
        style_plot(self.plot_write)
        self.plot_write.setFixedHeight(70)
        layout.addWidget(self.plot_write)

        usage_row = QtWidgets.QHBoxLayout()
        self.free_label = QtWidgets.QLabel("-- free")
        self.free_label.setObjectName("disk-free")
        self.usage_bar = QtWidgets.QProgressBar()
        self.usage_bar.setRange(0, 100)
        self.usage_bar.setValue(0)
        self.usage_bar.setTextVisible(False)
        usage_row.addWidget(self.free_label)
        usage_row.addWidget(self.usage_bar, 1)
        layout.addLayout(usage_row)

        details = QtWidgets.QGroupBox("Details")
        details_layout = QtWidgets.QFormLayout(details)
        details_layout.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        details_layout.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        details_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        self.detail_labels = {}
        for key, label in [
            ("read", "Read"),
            ("write", "Write"),
            ("total_read", "Total read"),
            ("total_write", "Total written"),
            ("smart_total_read", "SMART total read"),
            ("smart_total_write", "SMART total written"),
            ("temp", "Temperature"),
            ("health", "Health"),
            ("power_cycles", "Power cycles"),
            ("power_on", "Power on hours"),
        ]:
            label_widget = QtWidgets.QLabel(label)
            label_widget.setObjectName("detail-label")
            label_widget.setStyleSheet("font-size: 10px;")
            label_widget.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label = QtWidgets.QLabel("--")
            value_label.setObjectName("detail-value")
            value_label.setStyleSheet("font-size: 10px;")
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            details_layout.addRow(label_widget, value_label)
            self.detail_labels[key] = value_label
        layout.addWidget(details)


class NetPage:
    """Custom network page with separate download/upload charts."""

    def __init__(self) -> None:
        self.widget = QtWidgets.QWidget()

        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.title = QtWidgets.QLabel("Network")
        self.title.setObjectName("page-title")
        layout.addWidget(self.title)

        self.main_label = QtWidgets.QLabel("Down / Up")
        self.main_label.setObjectName("main-label")
        self.main_value = QtWidgets.QLabel("--")
        self.main_value.setObjectName("main-value")

        main_box = QtWidgets.QVBoxLayout()
        main_box.addWidget(self.main_label)
        main_box.addWidget(self.main_value)
        layout.addLayout(main_box)

        self.plot_down = pg.PlotWidget()
        self.plot_down.setBackground((20, 20, 20))
        self.plot_down.setYRange(0, 100)
        self.plot_down.showGrid(x=True, y=True, alpha=0.2)
        self.plot_down.setMouseEnabled(x=False, y=False)
        self.plot_down.hideButtons()
        self.plot_down.setMenuEnabled(False)
        self.plot_down.setTitle("Download", color="#cfcfcf", size="9pt")
        self.curve_down = self.plot_down.plot(pen=pg.mkPen(color="#59a6ff", width=2))
        style_plot(self.plot_down)
        self.plot_down.setFixedHeight(70)
        layout.addWidget(self.plot_down)

        self.plot_up = pg.PlotWidget()
        self.plot_up.setBackground((20, 20, 20))
        self.plot_up.setYRange(0, 100)
        self.plot_up.showGrid(x=True, y=True, alpha=0.2)
        self.plot_up.setMouseEnabled(x=False, y=False)
        self.plot_up.hideButtons()
        self.plot_up.setMenuEnabled(False)
        self.plot_up.setTitle("Upload", color="#cfcfcf", size="9pt")
        self.curve_up = self.plot_up.plot(pen=pg.mkPen(color="#ff5c5c", width=2))
        style_plot(self.plot_up)
        self.plot_up.setFixedHeight(70)
        layout.addWidget(self.plot_up)

        details = QtWidgets.QGroupBox("Details")
        details_layout = QtWidgets.QFormLayout(details)
        details_layout.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        details_layout.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        details_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        self.detail_labels = {}
        for key, label in [
            ("down", "Download"),
            ("up", "Upload"),
            ("total_down", "Total Down"),
            ("total_up", "Total Up"),
        ]:
            label_widget = QtWidgets.QLabel(label)
            label_widget.setObjectName("detail-label")
            label_widget.setStyleSheet("font-size: 10px;")
            label_widget.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label = QtWidgets.QLabel("--")
            value_label.setObjectName("detail-value")
            value_label.setStyleSheet("font-size: 10px;")
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            details_layout.addRow(label_widget, value_label)
            self.detail_labels[key] = value_label
        layout.addWidget(details)


class ArcGauge(QtWidgets.QWidget):
    def __init__(self, color: str = "#4aa3ff", parent=None) -> None:
        super().__init__(parent)
        self._percent = 0.0
        self._text = "--"
        self._color = QtGui.QColor(color)
        self.setFixedSize(74, 74)

    def set_value(self, percent: float | None, text: str | None = None) -> None:
        if percent is None:
            self._percent = 0.0
            self._text = "--"
        else:
            self._percent = max(0.0, min(100.0, percent))
            if text is not None:
                self._text = text
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        rect = self.rect().adjusted(8, 8, -8, -8)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        base_pen = QtGui.QPen(QtGui.QColor("#3a3a3a"), 6)
        base_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(base_pen)
        painter.drawArc(rect, 90 * 16, -360 * 16)

        arc_pen = QtGui.QPen(self._color, 6)
        arc_pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        painter.setPen(arc_pen)
        span = int(-360 * 16 * (self._percent / 100.0))
        painter.drawArc(rect, 90 * 16, span)

        painter.setPen(QtGui.QColor("#e6e6e6"))
        font = painter.font()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, self._text)


class GaugeBox(QtWidgets.QWidget):
    def __init__(self, title: str, color: str) -> None:
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.gauge = ArcGauge(color)
        layout.addWidget(self.gauge, alignment=QtCore.Qt.AlignmentFlag.AlignCenter)
        label = QtWidgets.QLabel(title)
        label.setObjectName("gauge-title")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)


class GpuPage:
    """Custom GPU page styled similar to iStat."""

    def __init__(self) -> None:
        self.widget = QtWidgets.QWidget()
        self.freq_max_mhz = 0.0
        self.power_max_mw = 0.0

        layout = QtWidgets.QVBoxLayout(self.widget)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.title = QtWidgets.QLabel("GPU")
        self.title.setObjectName("page-title")
        self.title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)

        top = QtWidgets.QHBoxLayout()
        self.model_label = QtWidgets.QLabel("Apple GPU")
        self.model_label.setObjectName("gpu-model")
        top.addWidget(self.model_label)
        top.addStretch(1)
        self.details_label = QtWidgets.QLabel("DETAILS")
        self.details_label.setObjectName("gpu-details")
        top.addWidget(self.details_label)
        layout.addLayout(top)

        details = QtWidgets.QGroupBox()
        details.setObjectName("gpu-details-box")
        details_layout = QtWidgets.QFormLayout(details)
        details_layout.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        details_layout.setFormAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
        )
        details_layout.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        details_layout.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.DontWrapRows)
        details_layout.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        self.detail_labels = {}
        for key, label in [
            ("model", "Model"),
            ("cores", "Cores"),
            ("status", "Status"),
            ("util", "Utilization"),
            ("render", "Render utilization"),
            ("tiler", "Tiler utilization"),
        ]:
            label_widget = QtWidgets.QLabel(label)
            label_widget.setObjectName("detail-label")
            label_widget.setStyleSheet("font-size: 10px;")
            label_widget.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label = QtWidgets.QLabel("--")
            value_label.setObjectName("detail-value")
            value_label.setStyleSheet("font-size: 10px;")
            value_label.setAlignment(
                QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )
            details_layout.addRow(label_widget, value_label)
            self.detail_labels[key] = value_label
        layout.addWidget(details)

        gauges = QtWidgets.QHBoxLayout()
        self.util_gauge = GaugeBox("Utilization", "#4aa3ff")
        self.render_gauge = GaugeBox("Render", "#7d7bff")
        self.tiler_gauge = GaugeBox("Tiler", "#ffb74d")
        gauges.addWidget(self.util_gauge)
        gauges.addWidget(self.render_gauge)
        gauges.addWidget(self.tiler_gauge)
        layout.addLayout(gauges)

        self.plot = pg.PlotWidget()
        self.plot.setBackground((20, 20, 20))
        self.plot.setYRange(0, 100)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.plot.setTitle("GPU Usage", color="#cfcfcf", size="9pt")
        self.curve = self.plot.plot(pen=pg.mkPen(color="#7d7bff", width=2))
        style_plot(self.plot)
        self.plot.setFixedHeight(70)
        layout.addWidget(self.plot)

class DetailWindow(QtWidgets.QWidget):
    """Floating details panel shown when clicking a metric in the menu bar."""

    def __init__(self, config: ConfigStore, on_open_settings, on_quit) -> None:
        super().__init__()
        self.config = config
        self.on_open_settings = on_open_settings
        self.on_quit = on_quit
        self._drag_pos = None

        self.history = {
            "cpu": [0.0] * 120,
            "gpu": [0.0] * 120,
            "ram": [0.0] * 120,
            "disk_read": [0.0] * 120,
            "disk_write": [0.0] * 120,
            "net": [0.0] * 120,
            "net_down": [0.0] * 120,
            "net_up": [0.0] * 120,
        }
        self._list_last = {"cpu": 0.0, "ram": 0.0}

        self.setWindowTitle("System Details")
        self._build_ui()
        self._apply_style()

    def _build_ui(self) -> None:
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        panel = QtWidgets.QFrame()
        panel.setObjectName("panel")
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 10, 12, 12)
        panel_layout.setSpacing(8)
        root.addWidget(panel)

        header = QtWidgets.QHBoxLayout()
        self.header_icon = QtWidgets.QLabel("C")
        self.header_icon.setFixedSize(22, 22)
        self.header_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.header_icon.setObjectName("header-icon")
        header.addWidget(self.header_icon)
        self.header_title = QtWidgets.QLabel("Details")
        self.header_title.setObjectName("header-title")
        header.addWidget(self.header_title)
        header.addStretch(1)

        settings_btn = QtWidgets.QPushButton("Settings")
        close_btn = QtWidgets.QPushButton("Close")
        quit_btn = QtWidgets.QPushButton("Quit")
        header.addWidget(settings_btn)
        header.addWidget(close_btn)
        header.addWidget(quit_btn)

        settings_btn.clicked.connect(self.on_open_settings)
        close_btn.clicked.connect(self.hide)
        quit_btn.clicked.connect(self.on_quit)

        panel_layout.addLayout(header)

        self.stack = QtWidgets.QStackedWidget()
        self.stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Preferred,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        panel_layout.addWidget(self.stack)

        self.pages = {
            "cpu": MetricPage(
                "CPU",
                "Usage",
                "CPU Usage",
                "#4aa3ff",
                [
                    ("load_1", "Load 1m"),
                    ("load_5", "Load 5m"),
                    ("load_15", "Load 15m"),
                    ("cores", "Cores"),
                    ("uptime", "Uptime"),
                ],
            ),
            "gpu": GpuPage(),
            "ram": MetricPage(
                "RAM",
                "Usage",
                "RAM Usage",
                "#66d1ff",
                [
                    ("used", "Used"),
                    ("available", "Available"),
                    ("total", "Total"),
                    ("swap", "Swap Used"),
                ],
            ),
            "disk": DiskPage(),
            "net": NetPage(),
        }

        for page in self.pages.values():
            self.stack.addWidget(page.widget)

        cpu_page = self.pages["cpu"]
        cpu_top = QtWidgets.QGroupBox("Top CPU Apps")
        cpu_top_layout = QtWidgets.QVBoxLayout(cpu_top)
        cpu_top_layout.setContentsMargins(8, 6, 8, 6)
        cpu_top_layout.setSpacing(4)
        cpu_page.top_labels = []
        for _ in range(10):
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(8, 2, 8, 2)
            row_layout.setSpacing(6)

            name_label = QtWidgets.QLabel("--")
            name_label.setStyleSheet("font-size: 9px;")
            name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            name_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )

            value_label = QtWidgets.QLabel("--")
            value_label.setStyleSheet("font-size: 9px; color: #4aa3ff;")
            value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )

            row_layout.addWidget(name_label)
            row_layout.addWidget(value_label)
            cpu_top_layout.addWidget(row)
            row.setVisible(False)
            cpu_page.top_labels.append((row, name_label, value_label))
        cpu_page.widget.layout().addWidget(cpu_top)

        ram_page = self.pages["ram"]
        ram_top = QtWidgets.QGroupBox("Top RAM Apps")
        ram_top_layout = QtWidgets.QVBoxLayout(ram_top)
        ram_top_layout.setContentsMargins(8, 6, 8, 6)
        ram_top_layout.setSpacing(4)
        ram_page.top_labels = []
        for _ in range(10):
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(8, 2, 8, 2)
            row_layout.setSpacing(6)

            name_label = QtWidgets.QLabel("--")
            name_label.setStyleSheet("font-size: 9px;")
            name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            name_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )

            value_label = QtWidgets.QLabel("--")
            value_label.setStyleSheet("font-size: 9px; color: #66d1ff;")
            value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            value_label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed,
            )

            row_layout.addWidget(name_label)
            row_layout.addWidget(value_label)
            ram_top_layout.addWidget(row)
            row.setVisible(False)
            ram_page.top_labels.append((row, name_label, value_label))
        ram_page.widget.layout().addWidget(ram_top)

        if hasattr(ram_page, "details_layout"):
            ram_page.details_layout.setContentsMargins(8, 6, 8, 6)
            ram_page.details_layout.setVerticalSpacing(4)
            ram_page.details_layout.setLabelAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            ram_page.details_layout.setFormAlignment(
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop
            )
            for value_label in ram_page.detail_labels.values():
                label_widget = ram_page.details_layout.labelForField(value_label)
                if label_widget is not None:
                    label_widget.setStyleSheet("font-size: 10px;")
                    label_widget.setAlignment(
                        QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                value_label.setStyleSheet("font-size: 10px; color: #66d1ff;")
                value_label.setAlignment(
                    QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter
                )
                value_label.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
        self.stack.currentChanged.connect(self._on_stack_changed)

    def _apply_style(self) -> None:
        flags = (
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setWindowFlags(flags)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

        self.setStyleSheet(
            """
            QWidget { 
                background: transparent;
                color: #e6e6e6; 
            }
            QFrame#panel {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #2b2d30, stop:1 #1e1f22);
                border: 1px solid #1a1b1e;
                border-radius: 10px;
            }
            QLabel#header-title { font-size: 14px; font-weight: 600; }
            QLabel#page-title { font-size: 16px; font-weight: 600; }
            QLabel#main-label { font-size: 11px; color: #bdbdbd; }
            QLabel#main-value { font-size: 28px; font-weight: 700; }
            QLabel#detail-value { font-weight: 600; }
            QLabel#disk-name { font-size: 12px; color: #cfcfcf; }
            QLabel#disk-percent { font-size: 12px; color: #cfcfcf; }
            QLabel#disk-read { color: #ff5c5c; font-weight: 600; }
            QLabel#disk-write { color: #4aa3ff; font-weight: 600; }
            QLabel#disk-free { color: #cfcfcf; }
            QLabel#gpu-model { font-size: 12px; color: #cfcfcf; }
            QLabel#gpu-details { font-size: 10px; color: #9aa0a6; }
            QLabel#gauge-title { font-size: 10px; color: #bdbdbd; }
            QGroupBox#gpu-details-box { border: 1px solid #3a3a3a; border-radius: 8px; }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 8px;
                margin-top: 12px;
                padding: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #cfcfcf;
            }
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover { background: #4a4a4a; }
            QProgressBar {
                background: #2a2a2a;
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                height: 10px;
            }
            QProgressBar::chunk {
                background: #4aa3ff;
                border-radius: 6px;
            }
            """
        )

        screen = QtWidgets.QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else QtCore.QRect(0, 0, 1280, 720)
        self._fixed_width = 320
        self.setFixedWidth(self._fixed_width)
        self.adjustSize()
        height = self.sizeHint().height()
        self.resize(self._fixed_width, height)
        self.move(geo.x() + geo.width() - self._fixed_width - 12, geo.y() + 28)

    def _resize_to_page(self, page_widget: QtWidgets.QWidget) -> None:
        page_widget.adjustSize()
        if page_widget.layout() is not None:
            page_widget.layout().invalidate()
            page_widget.layout().activate()
            page_height = page_widget.layout().sizeHint().height()
        else:
            page_height = page_widget.sizeHint().height()
        if page_height > 0:
            self.stack.setFixedHeight(page_height)
        if self.layout() is not None:
            self.layout().invalidate()
            self.layout().activate()
        self.adjustSize()
        height = self.sizeHint().height()
        self.resize(self._fixed_width, height)

    def _schedule_resize(self, page_widget: QtWidgets.QWidget) -> None:
        self._resize_to_page(page_widget)
        QtCore.QTimer.singleShot(0, lambda: self._resize_to_page(page_widget))
        QtCore.QTimer.singleShot(50, lambda: self._resize_to_page(page_widget))

    def _on_stack_changed(self, _index: int) -> None:
        page = self.stack.currentWidget()
        if page is not None:
            self._schedule_resize(page)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        page = self.stack.currentWidget()
        if page is not None:
            self._schedule_resize(page)

    def show_page(self, key: str, anchor: Dict | None = None) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        self.header_title.setText(page.title.text())
        icon_map = {
            "cpu": ("C", "#4aa3ff"),
            "gpu": ("G", "#7d7bff"),
            "ram": ("R", "#66d1ff"),
            "disk": ("D", "#ff6b6b"),
            "net": ("N", "#6cff6c"),
        }
        label, color = icon_map.get(key, ("?", "#888888"))
        self.header_icon.setText(label)
        self.header_icon.setStyleSheet(
            f"background: {color}; color: #0b0b0b; border-radius: 11px; font-weight: 700;"
        )
        self.stack.setCurrentWidget(page.widget)
        self._schedule_resize(page.widget)
        if key == "net":
            self._refresh_net_charts()
        if anchor:
            self.position_below_anchor(anchor)
        self.show()
        self.raise_()
        if hasattr(self, "_on_show"):
            self._on_show()

    def _refresh_net_charts(self) -> None:
        net_page = self.pages.get("net")
        if net_page is None or not hasattr(net_page, "curve_down"):
            return
        net_page.curve_down.setData(self.history["net_down"])
        net_page.curve_up.setData(self.history["net_up"])
        if hasattr(net_page, "plot_down"):
            net_page.plot_down.repaint()
        if hasattr(net_page, "plot_up"):
            net_page.plot_up.repaint()

    def update_from_sample(self, sample: SystemSample) -> None:
        try:
            cpu_page = self.pages["cpu"]
            cpu_page.main_value.setText(f"{sample.cpu_percent:.0f}%")
            self.history["cpu"] = (self.history["cpu"] + [sample.cpu_percent])[-len(self.history["cpu"]) :]
            cpu_page.curve.setData(self.history["cpu"])

            try:
                load_1, load_5, load_15 = os.getloadavg()
            except Exception:
                load_1, load_5, load_15 = 0.0, 0.0, 0.0
            cpu_page.detail_labels["load_1"].setText(f"{load_1:.2f}")
            cpu_page.detail_labels["load_5"].setText(f"{load_5:.2f}")
            cpu_page.detail_labels["load_15"].setText(f"{load_15:.2f}")
            cpu_page.detail_labels["cores"].setText(str(psutil.cpu_count(logical=True)))
            uptime_seconds = int(time.time() - psutil.boot_time())
            cpu_page.detail_labels["uptime"].setText(format_uptime(uptime_seconds))
            if hasattr(cpu_page, "top_labels"):
                now = time.time()
                interval_s = max(0.5, float(self.config.data.get("update_ms", 1000)) / 1000.0)
                if (
                    now - self._list_last["cpu"] >= interval_s
                    and self.stack.currentWidget() == cpu_page.widget
                ):
                    self._list_last["cpu"] = now
                    top = get_top_cpu_processes(10)
                    for idx, pair in enumerate(cpu_page.top_labels):
                        row, name_label, value_label = pair
                        if idx < len(top):
                            name, cpu = top[idx]
                            name_label.setText(name)
                            value_label.setText(f"{cpu:.1f}%")
                            row.setVisible(True)
                        else:
                            row.setVisible(False)
        except Exception as exc:
            log_error("cpu_update", exc)

        try:
            ram_page = self.pages["ram"]
            ram_page.main_value.setText(f"{sample.ram_percent:.0f}%")
            self.history["ram"] = (self.history["ram"] + [sample.ram_percent])[-len(self.history["ram"]) :]
            ram_page.curve.setData(self.history["ram"])
            ram_page.detail_labels["used"].setText(format_bytes(sample.ram_used))
            ram_page.detail_labels["available"].setText(format_bytes(sample.ram_available))
            ram_page.detail_labels["total"].setText(format_bytes(sample.ram_total))
            swap = psutil.swap_memory()
            ram_page.detail_labels["swap"].setText(format_bytes(swap.used))
            if hasattr(ram_page, "top_labels"):
                now = time.time()
                interval_s = max(0.5, float(self.config.data.get("update_ms", 1000)) / 1000.0)
                if (
                    now - self._list_last["ram"] >= interval_s
                    and self.stack.currentWidget() == ram_page.widget
                ):
                    self._list_last["ram"] = now
                    top = get_top_ram_processes(10)
                    for idx, pair in enumerate(ram_page.top_labels):
                        row, name_label, value_label = pair
                        if idx < len(top):
                            name, rss = top[idx]
                            name_label.setText(name)
                            value_label.setText(format_bytes(rss))
                            row.setVisible(True)
                        else:
                            row.setVisible(False)
        except Exception as exc:
            log_error("ram_update", exc)

        disk_page = self.pages["disk"]
        try:
            total_b, free_b, percent = get_disk_usage_info()
            # macOS storage UI uses decimal GB (1e9 bytes)
            free_gb = free_b / 1_000_000_000
            total_gb = total_b / 1_000_000_000
            disk_page.disk_percent.setText(f"{percent:.0f}%")
            disk_page.free_label.setText(f"{free_gb:.1f} GB of {total_gb:.1f} GB free")
            disk_page.usage_bar.setValue(int(percent))

            read_mb = sample.disk_read_bps / 1024 / 1024
            write_mb = sample.disk_write_bps / 1024 / 1024
            disk_page.read_label.setText(f"{read_mb:.1f} MB/s")
            disk_page.write_label.setText(f"{write_mb:.1f} MB/s")

            self.history["disk_read"] = (self.history["disk_read"] + [read_mb])[-len(self.history["disk_read"]) :]
            self.history["disk_write"] = (self.history["disk_write"] + [write_mb])[-len(self.history["disk_write"]) :]
            disk_page.read_curve.setData(self.history["disk_read"])
            disk_page.write_curve.setData(self.history["disk_write"])

            disk_page.detail_labels["read"].setText(format_rate(sample.disk_read_bps))
            disk_page.detail_labels["write"].setText(format_rate(sample.disk_write_bps))
            disk_io = psutil.disk_io_counters()
            disk_page.detail_labels["total_read"].setText(format_bytes(disk_io.read_bytes))
            disk_page.detail_labels["total_write"].setText(format_bytes(disk_io.write_bytes))

            meta = get_disk_meta()
            disk_page.volume_name.setText(meta.get("volume_name") or "Macintosh HD")
            disk_page.detail_labels["smart_total_read"].setText(meta.get("smart_total_read", "N/A"))
            disk_page.detail_labels["smart_total_write"].setText(meta.get("smart_total_write", "N/A"))
            disk_page.detail_labels["temp"].setText(meta.get("temperature", "N/A"))
            disk_page.detail_labels["health"].setText(meta.get("health", "N/A"))
            disk_page.detail_labels["power_cycles"].setText(meta.get("power_cycles", "N/A"))
            disk_page.detail_labels["power_on"].setText(meta.get("power_on_hours", "N/A"))
            if meta.get("smart_error"):
                log_error("smartctl", Exception(meta["smart_error"]))
        except Exception:
            disk_page.disk_percent.setText("N/A")
            disk_page.free_label.setText("N/A")
            disk_page.read_label.setText("N/A")
            disk_page.write_label.setText("N/A")
            disk_page.detail_labels["read"].setText("N/A")
            disk_page.detail_labels["write"].setText("N/A")
            disk_page.detail_labels["total_read"].setText("N/A")
            disk_page.detail_labels["total_write"].setText("N/A")
            disk_page.detail_labels["smart_total_read"].setText("N/A")
            disk_page.detail_labels["smart_total_write"].setText("N/A")
            disk_page.detail_labels["temp"].setText("N/A")
            disk_page.detail_labels["health"].setText("N/A")
            disk_page.detail_labels["power_cycles"].setText("N/A")
            disk_page.detail_labels["power_on"].setText("N/A")

        net_page = self.pages["net"]
        try:
            net_page.main_value.setText(
                f"{format_rate_short(sample.net_down_bps)}/{format_rate_short(sample.net_up_bps)}"
            )
            net_down = max(0.0, sample.net_down_bps / 1024)
            net_up = max(0.0, sample.net_up_bps / 1024)
            self.history["net"] = (self.history["net"] + [net_down + net_up])[-len(self.history["net"]) :]
            self.history["net_down"] = (self.history["net_down"] + [net_down])[-len(self.history["net_down"]) :]
            self.history["net_up"] = (self.history["net_up"] + [net_up])[-len(self.history["net_up"]) :]
            if hasattr(net_page, "curve_down") and hasattr(net_page, "curve_up"):
                if hasattr(net_page, "plot_down"):
                    down_max = max(1.0, max(self.history["net_down"]) * 1.2)
                    net_page.plot_down.setYRange(0, down_max)
                if hasattr(net_page, "plot_up"):
                    up_max = max(1.0, max(self.history["net_up"]) * 1.2)
                    net_page.plot_up.setYRange(0, up_max)
                net_page.curve_down.setData(self.history["net_down"])
                net_page.curve_up.setData(self.history["net_up"])
            else:
                net_page.curve.setData(self.history["net"])
            net_page.detail_labels["down"].setText(format_rate(sample.net_down_bps))
            net_page.detail_labels["up"].setText(format_rate(sample.net_up_bps))
            net_io = psutil.net_io_counters()
            net_page.detail_labels["total_down"].setText(format_bytes(net_io.bytes_recv))
            net_page.detail_labels["total_up"].setText(format_bytes(net_io.bytes_sent))
        except Exception as exc:
            log_error("net_update", exc)
            net_page.main_value.setText("N/A")
            net_page.detail_labels["down"].setText("N/A")
            net_page.detail_labels["up"].setText("N/A")
            net_page.detail_labels["total_down"].setText("N/A")
            net_page.detail_labels["total_up"].setText("N/A")

        try:
            gpu_page = self.pages["gpu"]
            if isinstance(gpu_page, GpuPage):
                info = get_gpu_static_info()
                gpu_page.model_label.setText(info.get("model") or "Apple GPU")
                gpu_page.detail_labels["model"].setText(info.get("model") or "--")
                gpu_page.detail_labels["cores"].setText(str(info.get("cores")) if info.get("cores") else "--")

                if sample.gpu_device_percent is None and sample.gpu_render_percent is None:
                    gpu_page.detail_labels["status"].setText("No GPU data")
                    gpu_page.detail_labels["util"].setText("N/A")
                    gpu_page.detail_labels["render"].setText("N/A")
                    gpu_page.detail_labels["tiler"].setText("N/A")
                    gpu_page.util_gauge.gauge.set_value(None, None)
                    gpu_page.render_gauge.gauge.set_value(None, None)
                    gpu_page.tiler_gauge.gauge.set_value(None, None)
                    self.history["gpu"] = (self.history["gpu"] + [0.0])[-len(self.history["gpu"]) :]
                    gpu_page.curve.setData(self.history["gpu"])
                else:
                    gpu_page.detail_labels["status"].setText("Active")
                    device = sample.gpu_device_percent
                    render = sample.gpu_render_percent
                    tiler = sample.gpu_tiler_percent

                    if device is not None:
                        gpu_page.detail_labels["util"].setText(f"{device:.1f}%")
                        gpu_page.util_gauge.gauge.set_value(device, f"{device:.0f}%")
                        self.history["gpu"] = (self.history["gpu"] + [device])[-len(self.history["gpu"]) :]
                        gpu_page.curve.setData(self.history["gpu"])
                    else:
                        gpu_page.detail_labels["util"].setText("N/A")
                        gpu_page.util_gauge.gauge.set_value(None, None)

                    if render is not None:
                        gpu_page.detail_labels["render"].setText(f"{render:.1f}%")
                        gpu_page.render_gauge.gauge.set_value(render, f"{render:.0f}%")
                    else:
                        gpu_page.detail_labels["render"].setText("N/A")
                        gpu_page.render_gauge.gauge.set_value(None, None)

                    if tiler is not None:
                        gpu_page.detail_labels["tiler"].setText(f"{tiler:.1f}%")
                        gpu_page.tiler_gauge.gauge.set_value(tiler, f"{tiler:.0f}%")
                    else:
                        gpu_page.detail_labels["tiler"].setText("N/A")
                        gpu_page.tiler_gauge.gauge.set_value(None, None)
            else:
                if sample.gpu_device_percent is None:
                    gpu_page.main_value.setText("N/A")
                    gpu_page.detail_labels["status"].setText("No GPU data")
                    self.history["gpu"] = (self.history["gpu"] + [0.0])[-len(self.history["gpu"]) :]
                    gpu_page.curve.setData(self.history["gpu"])
                else:
                    gpu_page.main_value.setText(f"{sample.gpu_device_percent:.0f}%")
                    gpu_page.detail_labels["status"].setText("OK")
                    self.history["gpu"] = (self.history["gpu"] + [sample.gpu_device_percent])[-len(self.history["gpu"]) :]
                    gpu_page.curve.setData(self.history["gpu"])
        except Exception as exc:
            log_error("gpu_update", exc)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        event.ignore()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        event.ignore()

    def position_below_anchor(self, anchor: Dict) -> None:
        """Position panel directly under the clicked menu bar item."""
        qt_x = anchor["x"]
        qt_y = anchor["screen_y"] + anchor["screen_h"] - anchor["y"] - anchor["h"]

        screen = QtGui.QGuiApplication.screenAt(QtCore.QPoint(int(qt_x), int(qt_y)))
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()

        width = self.width()
        height = self.height()
        target_x = qt_x + anchor["w"] / 2 - width / 2
        target_y = qt_y + anchor["h"] + 6

        target_x = max(geo.x() + 8, min(target_x, geo.x() + geo.width() - width - 8))
        target_y = max(geo.y() + 8, min(target_y, geo.y() + geo.height() - height - 8))

        self.move(int(target_x), int(target_y))


class SettingsWindow(QtWidgets.QWidget):
    """Settings UI to toggle visible metrics and show a live chart."""

    def __init__(self, config: ConfigStore, on_apply, on_quit, on_enable_priv, on_open_dashboard) -> None:
        super().__init__()
        self.config = config
        self.on_apply = on_apply
        self.on_quit = on_quit
        self.on_enable_priv = on_enable_priv
        self.on_open_dashboard = on_open_dashboard
        self.cpu_history = [0.0] * 120

        self.setWindowTitle("Monitor Settings")
        self.resize(520, 420)

        self._build_ui()

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("Realtime System Monitor")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        metrics_group = QtWidgets.QGroupBox("Menu Bar Metrics")
        metrics_layout = QtWidgets.QVBoxLayout(metrics_group)

        self.cpu_check = QtWidgets.QCheckBox("CPU usage")
        self.gpu_check = QtWidgets.QCheckBox("GPU usage")
        self.ram_check = QtWidgets.QCheckBox("RAM usage")
        self.disk_check = QtWidgets.QCheckBox("Disk read/write")
        self.net_check = QtWidgets.QCheckBox("Network up/down")

        show = self.config.data["show"]
        self.cpu_check.setChecked(show.get("cpu", True))
        self.gpu_check.setChecked(show.get("gpu", True))
        self.ram_check.setChecked(show.get("ram", True))
        self.disk_check.setChecked(show.get("disk", True))
        self.net_check.setChecked(show.get("net", True))

        metrics_layout.addWidget(self.cpu_check)
        metrics_layout.addWidget(self.gpu_check)
        metrics_layout.addWidget(self.ram_check)
        metrics_layout.addWidget(self.disk_check)
        metrics_layout.addWidget(self.net_check)

        layout.addWidget(metrics_group)

        chart_group = QtWidgets.QGroupBox("Live Chart")
        chart_layout = QtWidgets.QVBoxLayout(chart_group)

        self.chart_check = QtWidgets.QCheckBox("Show CPU chart")
        self.chart_check.setChecked(self.config.data["show"].get("chart", True))
        chart_layout.addWidget(self.chart_check)

        self.plot = pg.PlotWidget()
        self.plot.setBackground((0, 0, 0))
        self.plot.setYRange(0, 100)
        self.plot.showGrid(x=True, y=True, alpha=0.2)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setMenuEnabled(False)
        self.cpu_curve = self.plot.plot(pen=pg.mkPen(color="#ff6b6b", width=2))
        style_plot(self.plot)
        chart_layout.addWidget(self.plot)

        layout.addWidget(chart_group)

        status_group = QtWidgets.QGroupBox("Status")
        status_layout = QtWidgets.QFormLayout(status_group)
        self.brew_status = QtWidgets.QLabel("--")
        self.smartctl_status = QtWidgets.QLabel("--")
        self.smart_status = QtWidgets.QLabel("--")
        self.gpu_status = QtWidgets.QLabel("--")
        status_layout.addRow("Homebrew", self.brew_status)
        status_layout.addRow("smartctl", self.smartctl_status)
        status_layout.addRow("SMART access", self.smart_status)
        status_layout.addRow("GPU access", self.gpu_status)
        layout.addWidget(status_group)

        app_group = QtWidgets.QGroupBox("App Options")
        app_layout = QtWidgets.QVBoxLayout(app_group)
        self.startup_check = QtWidgets.QCheckBox("Start at login")
        self.hide_dock_check = QtWidgets.QCheckBox("Hide app in Dock")
        self.startup_check.setChecked(self.config.data.get("start_at_login", False))
        self.hide_dock_check.setChecked(self.config.data.get("hide_dock", False))
        app_layout.addWidget(self.startup_check)
        app_layout.addWidget(self.hide_dock_check)
        layout.addWidget(app_group)

        interval_group = QtWidgets.QGroupBox("Update speed")
        interval_layout = QtWidgets.QVBoxLayout(interval_group)
        self.interval_buttons = {}
        choices = [
            ("slow", "Chậm (5s)", 5000),
            ("medium", "Trung bình (3s)", 3000),
            ("fast", "Nhanh (1.5s)", 1500),
            ("fast1s", "Nhanh (1s)", 1000),
            ("ultra", "Siêu nhanh (0.5s)", 500),
        ]
        current_ms = self.config.data.get("update_ms", 1000)
        best_id = min(choices, key=lambda item: abs(item[2] - current_ms))[0]
        for key, label, ms in choices:
            btn = QtWidgets.QRadioButton(label)
            btn.setChecked(key == best_id)
            btn.setProperty("interval_ms", ms)
            interval_layout.addWidget(btn)
            self.interval_buttons[key] = btn
        layout.addWidget(interval_group)

        buttons = QtWidgets.QHBoxLayout()
        enable_btn = QtWidgets.QPushButton("Enable GPU/SMART")
        dashboard_btn = QtWidgets.QPushButton("Open Dashboard")
        apply_btn = QtWidgets.QPushButton("Apply")
        quit_btn = QtWidgets.QPushButton("Quit")
        close_btn = QtWidgets.QPushButton("Close")
        buttons.addWidget(enable_btn)
        buttons.addWidget(dashboard_btn)
        buttons.addWidget(apply_btn)
        buttons.addWidget(quit_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        enable_btn.clicked.connect(self.on_enable_priv)
        dashboard_btn.clicked.connect(self.on_open_dashboard)
        apply_btn.clicked.connect(self.apply)
        quit_btn.clicked.connect(self.on_quit)
        close_btn.clicked.connect(self.close)

        self.setStyleSheet(
            """
            QWidget {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #2b2d30, stop:1 #1e1f22);
                color: #e6e6e6;
            }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 8px;
                margin-top: 12px;
                padding: 6px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #cfcfcf;
            }
            QPushButton {
                background: #3a3a3a;
                border: 1px solid #4a4a4a;
                border-radius: 6px;
                padding: 4px 10px;
            }
            QPushButton:hover { background: #4a4a4a; }
            QSpinBox {
                background: #1c1d20;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 2px 6px;
            }
            """
        )

        self._apply_chart_visibility()
        self.chart_check.toggled.connect(self._apply_chart_visibility)
        self.update_status_labels()


    def _apply_chart_visibility(self) -> None:
        self.plot.setVisible(self.chart_check.isChecked())

    def update_from_sample(self, sample: SystemSample) -> None:
        if not self.chart_check.isChecked():
            return
        self.cpu_history = (self.cpu_history + [sample.cpu_percent])[-len(self.cpu_history) :]
        self.cpu_curve.setData(self.cpu_history)

    def apply(self) -> None:
        self.config.data["show"]["cpu"] = self.cpu_check.isChecked()
        self.config.data["show"]["gpu"] = self.gpu_check.isChecked()
        self.config.data["show"]["ram"] = self.ram_check.isChecked()
        self.config.data["show"]["disk"] = self.disk_check.isChecked()
        self.config.data["show"]["net"] = self.net_check.isChecked()
        self.config.data["show"]["chart"] = self.chart_check.isChecked()
        self.config.data["start_at_login"] = self.startup_check.isChecked()
        self.config.data["hide_dock"] = self.hide_dock_check.isChecked()
        interval_ms = None
        for btn in self.interval_buttons.values():
            if btn.isChecked():
                interval_ms = btn.property("interval_ms")
                break
        self.config.data["update_ms"] = int(interval_ms) if interval_ms else 1000
        self.config.save()
        self.on_apply()

    def update_status_labels(self) -> None:
        brew = find_brew() is not None
        smartctl = find_smartctl() is not None
        smart_access = can_run_smartctl()
        gpu_access = can_read_gpu_ioreg()

        self._set_status(self.brew_status, brew)
        self._set_status(self.smartctl_status, smartctl)
        self._set_status(self.smart_status, smart_access)
        self._set_status(self.gpu_status, gpu_access)

    def _set_status(self, label: QtWidgets.QLabel, ok: bool) -> None:
        label.setText("Enabled" if ok else "Disabled")
        label.setStyleSheet("color: #6cff6c;" if ok else "color: #ff6b6b;")


class DashboardWindow(QtWidgets.QWidget):
    def __init__(self, config: ConfigStore, on_show, on_close) -> None:
        super().__init__()
        self.config = config
        self.on_show = on_show
        self.on_close = on_close
        self._list_last = {"cpu": 0.0, "ram": 0.0}
        self.cpu_hist = [0.0] * 120
        self.ram_hist = [0.0] * 120
        self.gpu_hist = [0.0] * 120
        self.disk_read_hist = [0.0] * 120
        self.disk_write_hist = [0.0] * 120
        self.net_down_hist = [0.0] * 120
        self.net_up_hist = [0.0] * 120

        self.setWindowTitle("System Dashboard")
        self.setWindowModality(QtCore.Qt.WindowModality.NonModal)
        self.setWindowFlag(QtCore.Qt.WindowType.Window, True)
        self.resize(1100, 720)
        self.setObjectName("dashboard-root")

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Dashboard")
        title.setObjectName("dashboard-title")
        header.addWidget(title)
        header.addStretch(1)
        root.addLayout(header)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        root.addLayout(body, 1)

        left = QtWidgets.QFrame()
        left.setObjectName("dash-left")
        left_layout = QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        left_layout.addWidget(left_scroll, 1)

        left_content = QtWidgets.QWidget()
        left_content_layout = QtWidgets.QVBoxLayout(left_content)
        left_content_layout.setContentsMargins(0, 0, 0, 0)
        left_content_layout.setSpacing(10)
        left_scroll.setWidget(left_content)

        body.addWidget(left, 1)

        right = QtWidgets.QFrame()
        right.setObjectName("dash-right")
        right_layout = QtWidgets.QGridLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        body.addWidget(right, 3)

        # CPU
        self.cpu_box, self.cpu_layout = self._make_section("CPU")
        self.cpu_usage = self._add_row(self.cpu_layout, "Usage")
        self.cpu_load1 = self._add_row(self.cpu_layout, "Load 1m")
        self.cpu_load5 = self._add_row(self.cpu_layout, "Load 5m")
        self.cpu_load15 = self._add_row(self.cpu_layout, "Load 15m")
        self.cpu_cores = self._add_row(self.cpu_layout, "Cores")
        self.cpu_uptime = self._add_row(self.cpu_layout, "Uptime")
        self.cpu_plot = self._make_chart(self.cpu_box, "#4aa3ff", fixed_height=70, y_range=(0, 100))

        self.cpu_list_box, self.cpu_list_rows = self._make_list_section(
            "Top CPU Apps", value_color="#4aa3ff"
        )
        left_content_layout.addWidget(self.cpu_list_box)

        # RAM
        self.ram_box, self.ram_layout = self._make_section("RAM")
        self.ram_usage = self._add_row(self.ram_layout, "Usage")
        self.ram_used = self._add_row(self.ram_layout, "Used")
        self.ram_avail = self._add_row(self.ram_layout, "Available")
        self.ram_total = self._add_row(self.ram_layout, "Total")
        self.ram_swap = self._add_row(self.ram_layout, "Swap Used")
        self.ram_plot = self._make_chart(self.ram_box, "#66d1ff", fixed_height=70, y_range=(0, 100))

        self.ram_list_box, self.ram_list_rows = self._make_list_section(
            "Top RAM Apps", value_color="#66d1ff"
        )
        left_content_layout.addWidget(self.ram_list_box)
        left_content_layout.addStretch(1)

        # Disk
        self.disk_box, self.disk_layout = self._make_section("Disk")
        self.disk_usage = self._add_row(self.disk_layout, "Usage")
        self.disk_free = self._add_row(self.disk_layout, "Free")
        self.disk_read = self._add_row(self.disk_layout, "Read")
        self.disk_write = self._add_row(self.disk_layout, "Write")
        self.disk_total_read = self._add_row(self.disk_layout, "Total read")
        self.disk_total_write = self._add_row(self.disk_layout, "Total written")
        self.disk_smart_read = self._add_row(self.disk_layout, "SMART total read")
        self.disk_smart_write = self._add_row(self.disk_layout, "SMART total written")
        self.disk_temp = self._add_row(self.disk_layout, "Temperature")
        self.disk_health = self._add_row(self.disk_layout, "Health")
        self.disk_cycles = self._add_row(self.disk_layout, "Power cycles")
        self.disk_hours = self._add_row(self.disk_layout, "Power on hours")
        self.disk_plot = self._make_dual_chart(
            self.disk_box,
            "#ff5c5c",
            "#4aa3ff",
            fixed_height=70,
        )

        # Network
        self.net_box, self.net_layout = self._make_section("Network")
        self.net_down = self._add_row(self.net_layout, "Download")
        self.net_up = self._add_row(self.net_layout, "Upload")
        self.net_total_down = self._add_row(self.net_layout, "Total Down")
        self.net_total_up = self._add_row(self.net_layout, "Total Up")
        self.net_plot = self._make_dual_chart(
            self.net_box,
            "#59a6ff",
            "#ff5c5c",
            fixed_height=70,
        )

        # GPU
        self.gpu_box, self.gpu_layout = self._make_section("GPU")
        self.gpu_model = self._add_row(self.gpu_layout, "Model")
        self.gpu_cores = self._add_row(self.gpu_layout, "Cores")
        self.gpu_status = self._add_row(self.gpu_layout, "Status")
        self.gpu_util = self._add_row(self.gpu_layout, "Utilization")
        self.gpu_render = self._add_row(self.gpu_layout, "Render utilization")
        self.gpu_tiler = self._add_row(self.gpu_layout, "Tiler utilization")
        self.gpu_plot = self._make_chart(self.gpu_box, "#7d7bff", fixed_height=70, y_range=(0, 100))

        # Fan / Temps / Power
        self.fan_box, self.fan_layout = self._make_section("Fan")
        self.fan_rpm = self._add_row(self.fan_layout, "RPM")
        self.fan_percent = self._add_row(self.fan_layout, "Percent")

        self.temp_box, self.temp_layout = self._make_section("Temperatures")
        self.temp_cpu = self._add_row(self.temp_layout, "CPU")
        self.temp_gpu = self._add_row(self.temp_layout, "GPU")
        self.temp_ssd = self._add_row(self.temp_layout, "SSD")

        self.power_box, self.power_layout = self._make_section("Power")
        self.power_cpu = self._add_row(self.power_layout, "CPU")
        self.power_gpu = self._add_row(self.power_layout, "GPU")

        # Battery
        self.batt_box, self.batt_layout = self._make_section("Battery")
        self.batt_percent = self._add_row(self.batt_layout, "Percent")
        self.batt_state = self._add_row(self.batt_layout, "Status")
        # Right grid placement
        right_layout.addWidget(self.cpu_box, 0, 0)
        right_layout.addWidget(self.ram_box, 0, 1)
        right_layout.addWidget(self.gpu_box, 0, 2)
        right_layout.addWidget(self.disk_box, 1, 0)
        right_layout.addWidget(self.net_box, 1, 1)
        right_layout.addWidget(self.batt_box, 1, 2)
        right_layout.addWidget(self.fan_box, 2, 0)
        right_layout.addWidget(self.temp_box, 2, 1)
        right_layout.addWidget(self.power_box, 2, 2)

        self.setStyleSheet(
            """
            #dashboard-root {
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #1d1f21, stop:1 #151618);
                color: #e6e6e6;
            }
            QLabel#dashboard-title { font-size: 22px; font-weight: 700; }
            QFrame#dash-left { background: #1b1d1f; border-radius: 10px; }
            QGroupBox {
                border: 1px solid #2a2b2e;
                border-radius: 10px;
                margin-top: 10px;
                padding: 6px;
                background: #1f2123;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #cfcfcf;
            }
            QLabel.dash-label { font-size: 10px; color: #bdbdbd; }
            QLabel.dash-value { font-size: 10px; color: #e6e6e6; }
            """
        )

    def _make_section(self, title: str):
        box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QFormLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setVerticalSpacing(4)
        layout.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.setFormAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return box, layout

    def _make_chart(self, box: QtWidgets.QGroupBox, color: str, fixed_height: int = 70, y_range=None):
        plot = pg.PlotWidget()
        plot.setBackground((20, 20, 20))
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setMenuEnabled(False)
        if y_range is not None:
            plot.setYRange(y_range[0], y_range[1])
        curve = plot.plot(pen=pg.mkPen(color=color, width=2))
        style_plot(plot)
        plot.setFixedHeight(fixed_height)
        box.layout().addRow(QtWidgets.QLabel("Chart"), plot)
        return (plot, curve)

    def _make_dual_chart(self, box: QtWidgets.QGroupBox, color_a: str, color_b: str, fixed_height: int = 70):
        plot = pg.PlotWidget()
        plot.setBackground((20, 20, 20))
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setMenuEnabled(False)
        curve_a = plot.plot(pen=pg.mkPen(color=color_a, width=2))
        curve_b = plot.plot(pen=pg.mkPen(color=color_b, width=2))
        style_plot(plot)
        plot.setFixedHeight(fixed_height)
        box.layout().addRow(QtWidgets.QLabel("Chart"), plot)
        return (plot, curve_a, curve_b)

    def _add_row(self, layout: QtWidgets.QFormLayout, label_text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(label_text)
        label.setObjectName("dash-label")
        label.setProperty("class", "dash-label")
        label.setStyleSheet("font-size: 10px; color: #bdbdbd;")
        label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
        value = QtWidgets.QLabel("--")
        value.setObjectName("dash-value")
        value.setProperty("class", "dash-value")
        value.setStyleSheet("font-size: 10px; color: #e6e6e6;")
        value.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
        value.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        layout.addRow(label, value)
        return value

    def _make_list_section(self, title: str, value_color: str):
        box = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        rows = []
        for _ in range(10):
            row = QtWidgets.QWidget()
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(8, 2, 8, 2)
            row_layout.setSpacing(6)
            name_label = QtWidgets.QLabel("--")
            name_label.setStyleSheet("font-size: 9px;")
            name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter)
            name_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
            value_label = QtWidgets.QLabel("--")
            value_label.setStyleSheet(f"font-size: 9px; color: {value_color};")
            value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            value_label.setSizePolicy(QtWidgets.QSizePolicy.Policy.Fixed, QtWidgets.QSizePolicy.Policy.Fixed)
            row_layout.addWidget(name_label)
            row_layout.addWidget(value_label)
            row.setVisible(False)
            layout.addWidget(row)
            rows.append((row, name_label, value_label))
        return box, rows

    def _update_list(self, rows, items, formatter):
        for idx, triple in enumerate(rows):
            row, name_label, value_label = triple
            if idx < len(items):
                name, value = items[idx]
                name_label.setText(name)
                value_label.setText(formatter(value))
                row.setVisible(True)
            else:
                row.setVisible(False)

    def update_from_sample(self, sample: SystemSample) -> None:
        try:
            self.cpu_usage.setText(f"{sample.cpu_percent:.0f}%")
            try:
                load_1, load_5, load_15 = os.getloadavg()
            except Exception:
                load_1, load_5, load_15 = 0.0, 0.0, 0.0
            self.cpu_load1.setText(f"{load_1:.2f}")
            self.cpu_load5.setText(f"{load_5:.2f}")
            self.cpu_load15.setText(f"{load_15:.2f}")
            self.cpu_cores.setText(str(psutil.cpu_count(logical=True)))
            uptime_seconds = int(time.time() - psutil.boot_time())
            self.cpu_uptime.setText(format_uptime(uptime_seconds))
            self.cpu_hist = (self.cpu_hist + [sample.cpu_percent])[-len(self.cpu_hist) :]
            self.cpu_plot[1].setData(self.cpu_hist)

            self.ram_usage.setText(f"{sample.ram_percent:.0f}%")
            self.ram_used.setText(format_bytes(sample.ram_used))
            self.ram_avail.setText(format_bytes(sample.ram_available))
            self.ram_total.setText(format_bytes(sample.ram_total))
            swap = psutil.swap_memory()
            self.ram_swap.setText(format_bytes(swap.used))
            self.ram_hist = (self.ram_hist + [sample.ram_percent])[-len(self.ram_hist) :]
            self.ram_plot[1].setData(self.ram_hist)

            total_b, free_b, percent = get_disk_usage_info()
            used_b = max(0, total_b - free_b)
            self.disk_usage.setText(f"{percent:.0f}%")
            self.disk_free.setText(format_bytes(free_b))
            self.disk_read.setText(format_rate(sample.disk_read_bps))
            self.disk_write.setText(format_rate(sample.disk_write_bps))
            disk_io = psutil.disk_io_counters()
            self.disk_total_read.setText(format_bytes(disk_io.read_bytes))
            self.disk_total_write.setText(format_bytes(disk_io.write_bytes))
            meta = get_disk_meta()
            self.disk_smart_read.setText(meta.get("smart_total_read", "N/A"))
            self.disk_smart_write.setText(meta.get("smart_total_write", "N/A"))
            self.disk_temp.setText(meta.get("temperature", "N/A"))
            self.disk_health.setText(meta.get("health", "N/A"))
            self.disk_cycles.setText(meta.get("power_cycles", "N/A"))
            self.disk_hours.setText(meta.get("power_on_hours", "N/A"))
            read_kb = sample.disk_read_bps / 1024
            write_kb = sample.disk_write_bps / 1024
            self.disk_read_hist = (self.disk_read_hist + [read_kb])[-len(self.disk_read_hist) :]
            self.disk_write_hist = (self.disk_write_hist + [write_kb])[-len(self.disk_write_hist) :]
            self.disk_plot[1].setData(self.disk_read_hist)
            self.disk_plot[2].setData(self.disk_write_hist)

            self.net_down.setText(format_rate(sample.net_down_bps))
            self.net_up.setText(format_rate(sample.net_up_bps))
            net_io = psutil.net_io_counters()
            self.net_total_down.setText(format_bytes(net_io.bytes_recv))
            self.net_total_up.setText(format_bytes(net_io.bytes_sent))
            down_kb = max(0.0, sample.net_down_bps / 1024)
            up_kb = max(0.0, sample.net_up_bps / 1024)
            self.net_down_hist = (self.net_down_hist + [down_kb])[-len(self.net_down_hist) :]
            self.net_up_hist = (self.net_up_hist + [up_kb])[-len(self.net_up_hist) :]
            self.net_plot[1].setData(self.net_down_hist)
            self.net_plot[2].setData(self.net_up_hist)

            info = get_gpu_static_info()
            self.gpu_model.setText(info.get("model") or "Apple GPU")
            self.gpu_cores.setText(str(info.get("cores")) if info.get("cores") else "N/A")
            if sample.gpu_device_percent is None and sample.gpu_render_percent is None:
                self.gpu_status.setText("No GPU data")
                self.gpu_util.setText("N/A")
                self.gpu_render.setText("N/A")
                self.gpu_tiler.setText("N/A")
            else:
                self.gpu_status.setText("Active")
                if sample.gpu_device_percent is not None:
                    self.gpu_util.setText(f"{sample.gpu_device_percent:.1f}%")
                else:
                    self.gpu_util.setText("N/A")
                if sample.gpu_render_percent is not None:
                    self.gpu_render.setText(f"{sample.gpu_render_percent:.1f}%")
                else:
                    self.gpu_render.setText("N/A")
                if sample.gpu_tiler_percent is not None:
                    self.gpu_tiler.setText(f"{sample.gpu_tiler_percent:.1f}%")
                else:
                    self.gpu_tiler.setText("N/A")
            gpu_value = sample.gpu_device_percent or sample.gpu_render_percent or 0.0
            self.gpu_hist = (self.gpu_hist + [gpu_value])[-len(self.gpu_hist) :]
            self.gpu_plot[1].setData(self.gpu_hist)

            fan = get_fan_status()
            if fan.get("rpm") is None:
                self.fan_rpm.setText("N/A")
                self.fan_percent.setText("N/A")
            else:
                self.fan_rpm.setText(f"{fan.get('rpm'):.0f} RPM")
                percent = fan.get("percent") or 0.0
                self.fan_percent.setText(f"{percent:.0f}%")

            temps = get_thermal_info()
            self.temp_cpu.setText(temps.get("cpu") or "N/A")
            self.temp_gpu.setText(temps.get("gpu") or "N/A")
            self.temp_ssd.setText(temps.get("ssd") or "N/A")

            power = get_power_info()
            self.power_cpu.setText(power.get("cpu") or "N/A")
            self.power_gpu.setText(power.get("gpu") or "N/A")

            batt = get_battery_info()
            if batt.get("percent") is not None:
                self.batt_percent.setText(f"{batt['percent']:.0f}%")
            else:
                self.batt_percent.setText("N/A")
            self.batt_state.setText(batt.get("state") or "No battery")

            now = time.time()
            interval_s = max(0.5, float(self.config.data.get("update_ms", 1000)) / 1000.0)
            if now - self._list_last["cpu"] >= interval_s:
                self._list_last["cpu"] = now
                cpu_top = get_top_cpu_processes(10)
                self._update_list(self.cpu_list_rows, cpu_top, lambda v: f"{v:.1f}%")
            if now - self._list_last["ram"] >= interval_s:
                self._list_last["ram"] = now
                ram_top = get_top_ram_processes(10)
                self._update_list(self.ram_list_rows, ram_top, format_bytes)
        except Exception as exc:
            log_error("dashboard_update", exc)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self.on_show:
            self.on_show()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.on_close:
            self.on_close()
        super().closeEvent(event)


class AppController(QtCore.QObject):
    """Wires together config, menu bar, settings UI, and metrics hub."""

    def __init__(self) -> None:
        super().__init__()
        self.config = ConfigStore()
        self.stats = SystemStats()
        self.hub = MetricsHub(self.stats, self.config.data["update_ms"])
        self.guard = LiveGuard(self.hub)
        self.last_sample: SystemSample | None = None
        self._startup_state = {"start_at_login": None, "hide_dock": None}
        self._auto_priv_started = False

        self.menu_bar = MenuBarController(self.config, self)
        self.detail_window = DetailWindow(self.config, self.open_settings, QtWidgets.QApplication.quit)
        self.settings = None
        self.dashboard = None

        self.hub.updated.connect(self._on_sample)
        self.detail_window._on_show = self._on_detail_show
        self.apply_startup_options(force=True)
        QtCore.QTimer.singleShot(1200, self.auto_enable_privileges)

    def open_settings(self) -> None:
        if self.settings is None:
            self.settings = SettingsWindow(
                self.config,
                self.apply_settings,
                QtWidgets.QApplication.quit,
                self.enable_privileges,
                self.open_dashboard,
            )
        self.settings.show()
        self.settings.raise_()

    def open_dashboard(self) -> None:
        if self.dashboard is None:
            self.dashboard = DashboardWindow(
                self.config,
                lambda: apply_dock_visibility(False),
                lambda: apply_dock_visibility(self.config.data.get("hide_dock", False)),
            )
        self.dashboard.show()
        self.dashboard.raise_()
        self.dashboard.activateWindow()
        try:
            NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        except Exception:
            pass
        if self.last_sample:
            self.dashboard.update_from_sample(self.last_sample)

    def apply_settings(self) -> None:
        self.hub.set_interval(self.config.data["update_ms"])
        self.menu_bar.refresh_visibility()
        self.apply_startup_options()

    def apply_startup_options(self, force: bool = False) -> None:
        desired_start = bool(self.config.data.get("start_at_login", False))
        desired_hide = bool(self.config.data.get("hide_dock", False))

        if force or desired_start != self._startup_state["start_at_login"]:
            ok, msg = set_start_at_login(desired_start)
            if not ok:
                QtWidgets.QMessageBox.warning(
                    self.settings or self.detail_window,
                    "Start at login",
                    msg,
                )
            self._startup_state["start_at_login"] = desired_start

        if force or desired_hide != self._startup_state["hide_dock"]:
            apply_dock_visibility(desired_hide)
            self._startup_state["hide_dock"] = desired_hide

    def show_detail(self, metric_key: str) -> None:
        anchor = self.menu_bar.get_anchor(metric_key)
        self.detail_window.show_page(metric_key, anchor)
        self.menu_bar.refresh_visibility()
        if self.last_sample:
            self.detail_window.update_from_sample(self.last_sample)

    def _on_sample(self, sample: SystemSample) -> None:
        self.last_sample = sample
        self.menu_bar.update_from_sample(sample)
        self.detail_window.update_from_sample(sample)
        if self.dashboard is not None:
            self.dashboard.update_from_sample(sample)
        if self.settings is not None:
            self.settings.update_from_sample(sample)

    def _on_detail_show(self) -> None:
        # Force a refresh when the detail window is shown
        if not self.hub.is_active():
            self.hub.set_interval(self.config.data["update_ms"])

    def enable_privileges(self, auto: bool = False) -> None:
        parent = self.settings or self.detail_window
        dialog = InstallProgressDialog(parent)
        worker = PrivilegeWorker()
        self._priv_dialog = dialog
        self._priv_worker = worker
        global ALLOW_ADMIN_PROMPT
        ALLOW_ADMIN_PROMPT = True

        worker.status.connect(dialog.set_status)
        worker.done.connect(self._on_priv_done)
        worker.start()
        dialog.show()

    def auto_enable_privileges(self) -> None:
        if self._auto_priv_started:
            return
        if can_run_powermetrics() and can_run_smartctl():
            return
        self._auto_priv_started = True
        self.enable_privileges(auto=True)

    def _on_priv_done(self, ok: bool, message: str) -> None:
        if hasattr(self, "_priv_dialog") and self._priv_dialog:
            self._priv_dialog.close()
        if self.settings is not None:
            self.settings.update_status_labels()
        if ok:
            QtWidgets.QMessageBox.information(
                self.settings,
                "Enabled",
                message,
            )
        else:
            QtWidgets.QMessageBox.warning(
                self.settings,
                "Failed",
                message,
            )

def _nscolor_from_hex(value: str) -> NSColor:
    value = value.lstrip("#")
    if len(value) != 6:
        return NSColor.whiteColor()
    r = int(value[0:2], 16) / 255.0
    g = int(value[2:4], 16) / 255.0
    b = int(value[4:6], 16) / 255.0
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


def make_metric_image(label: str, value: str, value_color: str | None = None) -> NSImage:
    """Render vertical label (small) + value (large) into an NSImage."""
    bar_height = NSStatusBar.systemStatusBar().thickness()
    label_font = NSFont.systemFontOfSize_(8.0)
    value_font = NSFont.monospacedDigitSystemFontOfSize_weight_(11.0, 0.3)

    label_attr = {
        NSFontAttributeName: label_font,
        NSForegroundColorAttributeName: NSColor.colorWithCalibratedWhite_alpha_(0.7, 1.0),
    }
    value_nscolor = _nscolor_from_hex(value_color) if value_color else NSColor.whiteColor()
    value_attr = {
        NSFontAttributeName: value_font,
        NSForegroundColorAttributeName: value_nscolor,
    }

    label_str = NSAttributedString.alloc().initWithString_attributes_(label, label_attr)
    value_str = NSAttributedString.alloc().initWithString_attributes_(value, value_attr)

    label_size = label_str.size()
    value_size = value_str.size()

    width = max(label_size.width, value_size.width) + 10
    height = bar_height

    image = NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()

    label_x = (width - label_size.width) / 2
    value_x = (width - value_size.width) / 2
    label_y = max(height - label_size.height - 1, 0)
    value_y = 1

    label_str.drawAtPoint_((label_x, label_y))
    value_str.drawAtPoint_((value_x, value_y))

    image.unlockFocus()
    return image


def make_net_image(up_value: str, down_value: str) -> NSImage:
    """Render upload/download arrows + values for the menu bar."""
    bar_height = NSStatusBar.systemStatusBar().thickness()
    line_font = NSFont.monospacedDigitSystemFontOfSize_weight_(9.0, 0.2)

    up_attr = {
        NSFontAttributeName: line_font,
        NSForegroundColorAttributeName: NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.35, 0.35, 1.0),
    }
    down_attr = {
        NSFontAttributeName: line_font,
        NSForegroundColorAttributeName: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.35, 0.65, 1.0, 1.0),
    }

    up_str = NSAttributedString.alloc().initWithString_attributes_(f"↑ {up_value}", up_attr)
    down_str = NSAttributedString.alloc().initWithString_attributes_(f"↓ {down_value}", down_attr)

    up_size = up_str.size()
    down_size = down_str.size()

    width = max(up_size.width, down_size.width) + 10
    height = bar_height

    image = NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()

    up_x = (width - up_size.width) / 2
    down_x = (width - down_size.width) / 2
    up_y = max(height - up_size.height - 1, 0)
    down_y = 1

    up_str.drawAtPoint_((up_x, up_y))
    down_str.drawAtPoint_((down_x, down_y))

    image.unlockFocus()
    return image


def format_rate_short(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024 / 1024:.1f}MB"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.0f}KB"
    return f"{bytes_per_sec:.0f}B"


def format_rate(bytes_per_sec: float) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / 1024 / 1024:.2f} MB/s"
    if bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec:.0f} B/s"


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(value)
    for unit in units:
        if size < 1024:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def format_power_watts(value_w: float) -> str:
    if value_w >= 1.0:
        return f"{value_w:.2f} W"
    return f"{value_w * 1000.0:.0f} mW"


def get_top_ram_processes(limit: int = 10) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    for proc in psutil.process_iter(attrs=["name", "pid", "memory_info"]):
        try:
            info = proc.info
            name = info.get("name") or f"PID {info.get('pid')}"
            mem = info.get("memory_info")
            if mem is None:
                continue
            rss = int(mem.rss)
            items.append((name, rss))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]


def get_top_cpu_processes(limit: int = 10) -> list[tuple[str, float]]:
    items: list[tuple[str, float]] = []
    for proc in psutil.process_iter(attrs=["name", "pid"]):
        try:
            name = proc.info.get("name") or f"PID {proc.info.get('pid')}"
            cpu = proc.cpu_percent(interval=None)
            items.append((name, float(cpu)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    items.sort(key=lambda x: x[1], reverse=True)
    return items[:limit]


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def style_plot(plot: pg.PlotWidget) -> None:
    axis_pen = pg.mkPen("#3b3f45")
    text_pen = pg.mkPen("#9aa0a6")
    plot.getAxis("bottom").setPen(axis_pen)
    plot.getAxis("left").setPen(axis_pen)
    plot.getAxis("bottom").setTextPen(text_pen)
    plot.getAxis("left").setTextPen(text_pen)
    # Hide axis labels/ticks for a cleaner sparkline look
    plot.getAxis("bottom").setStyle(showValues=False)
    plot.getAxis("left").setStyle(showValues=False)


def log_error(context: str, exc: Exception) -> None:
    try:
        with open("/tmp/realtime_system_monitor.log", "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {context}: {exc}\n")
    except Exception:
        pass


_POWERMETRICS_CACHE: dict[str, dict[str, object]] = {}
_BATTERY_CACHE: dict[str, object] = {"ts": 0.0, "data": None}


def _read_powermetrics(samplers: str, ttl: float = 1.0) -> str:
    now = time.time()
    cached = _POWERMETRICS_CACHE.get(samplers)
    if cached and (now - cached["ts"] < ttl):
        return cached.get("text", "") or ""

    powermetrics = "/usr/bin/powermetrics"
    if not os.path.exists(powermetrics):
        _POWERMETRICS_CACHE[samplers] = {"ts": now, "text": ""}
        return ""

    def _run(cmd):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception as exc:
            log_error("powermetrics_run", exc)
            return None

    output = ""
    result = _run(["sudo", "-n", powermetrics, "--samplers", samplers, "-n", "1", "-i", "1000"])
    if result is not None:
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")

    if result is None or result.returncode != 0 or not output.strip():
        result = _run([powermetrics, "--samplers", samplers, "-n", "1", "-i", "1000"])
        output = ""
        if result is not None:
            output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")

    if result is None or result.returncode != 0:
        output = ""

    _POWERMETRICS_CACHE[samplers] = {"ts": now, "text": output}
    return output


def get_fan_status() -> dict:
    try:
        output = _read_powermetrics("smc")
        rpm = None
        max_rpm = None
        for line in output.splitlines():
            if not re.search(r"fan", line, re.IGNORECASE):
                continue
            if rpm is None:
                match = re.search(r"([0-9.]+)\s*rpm", line, re.IGNORECASE)
                if match:
                    rpm = float(match.group(1))
            if max_rpm is None:
                match = re.search(r"max[^0-9]*([0-9.]+)\s*rpm", line, re.IGNORECASE)
                if match:
                    max_rpm = float(match.group(1))
        percent = None
        if rpm is not None and max_rpm:
            percent = max(0.0, min(100.0, rpm / max_rpm * 100.0))
        return {"rpm": rpm, "percent": percent}
    except Exception as exc:
        log_error("fan_status", exc)
        return {"rpm": None, "percent": None}


def _find_temp(output: str, patterns: list[str]) -> float | None:
    for line in output.splitlines():
        for pattern in patterns:
            if re.search(pattern, line, re.IGNORECASE):
                match = re.search(r"([0-9.]+)\s*(?:°?C|c)", line)
                if match:
                    return float(match.group(1))
    return None


def get_thermal_info() -> dict:
    temps = {"cpu": None, "gpu": None, "ssd": None}
    try:
        output = _read_powermetrics("smc")
        cpu = _find_temp(output, ["CPU die temperature", "CPU temperature", r"\bCPU Temp"])
        gpu = _find_temp(output, ["GPU die temperature", "GPU temperature", r"\bGPU Temp"])
        if cpu is not None:
            temps["cpu"] = f"{cpu:.0f}°C"
        if gpu is not None:
            temps["gpu"] = f"{gpu:.0f}°C"
    except Exception as exc:
        log_error("thermal_info", exc)
    try:
        ssd = get_disk_meta().get("temperature")
        if ssd:
            temps["ssd"] = ssd
    except Exception:
        pass
    return temps


def _find_power(output: str, label: str) -> float | None:
    for line in output.splitlines():
        if not re.search(label, line, re.IGNORECASE):
            continue
        if not re.search("power", line, re.IGNORECASE):
            continue
        match = re.search(r"([0-9.]+)\s*(mW|W)", line, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            unit = match.group(2).lower()
            return value / 1000.0 if unit == "mw" else value
    return None


def get_power_info() -> dict:
    try:
        output = _read_powermetrics("cpu_power,gpu_power")
        cpu_w = _find_power(output, "CPU")
        gpu_w = _find_power(output, "GPU")
        return {
            "cpu": format_power_watts(cpu_w) if cpu_w is not None else None,
            "gpu": format_power_watts(gpu_w) if gpu_w is not None else None,
        }
    except Exception as exc:
        log_error("power_info", exc)
        return {"cpu": None, "gpu": None}


def get_battery_info(ttl: float = 5.0) -> dict:
    now = time.time()
    cached = _BATTERY_CACHE.get("data")
    if cached and (now - float(_BATTERY_CACHE.get("ts", 0.0)) < ttl):
        return cached

    data = {"percent": None, "state": None}
    try:
        batt = psutil.sensors_battery()
        if batt:
            data["percent"] = batt.percent
            data["state"] = "Charging" if batt.power_plugged else "On battery"
            _BATTERY_CACHE.update({"ts": now, "data": data})
            return data
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        text = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result.returncode == 0 and text:
            lower = text.lower()
            match = re.search(r"(\d+)%", lower)
            if match:
                data["percent"] = float(match.group(1))
            if "charging" in lower or "ac power" in lower:
                data["state"] = "Charging"
            elif "discharging" in lower or "battery power" in lower:
                data["state"] = "On battery"
            elif "charged" in lower:
                data["state"] = "Charged"
    except Exception as exc:
        log_error("battery_info", exc)

    _BATTERY_CACHE.update({"ts": now, "data": data})
    return data


def get_app_bundle_path() -> Path | None:
    exe = Path(sys.executable).resolve()
    for part in exe.parents:
        if part.name.endswith(".app"):
            return part
    return None


def set_start_at_login(enabled: bool) -> tuple[bool, str]:
    if not enabled:
        try:
            if LAUNCH_AGENT_PATH.exists():
                try:
                    subprocess.run(
                        ["/bin/launchctl", "unload", "-w", str(LAUNCH_AGENT_PATH)],
                        capture_output=True,
                        text=True,
                        timeout=4,
                    )
                except Exception:
                    pass
                LAUNCH_AGENT_PATH.unlink(missing_ok=True)
            return True, "Disabled."
        except Exception as exc:
            return False, str(exc)

    app_path = get_app_bundle_path()
    if app_path is None:
        return False, "Start at login requires the .app bundle."

    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-a", str(app_path), "-g"],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    try:
        LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LAUNCH_AGENT_PATH, "wb") as handle:
            plistlib.dump(plist, handle)
        try:
            subprocess.run(
                ["/bin/launchctl", "load", "-w", str(LAUNCH_AGENT_PATH)],
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception:
            pass
        return True, "Enabled."
    except Exception as exc:
        return False, str(exc)


def apply_dock_visibility(hidden: bool) -> None:
    try:
        app = NSApplication.sharedApplication()
        policy = NSApplicationActivationPolicyAccessory if hidden else NSApplicationActivationPolicyRegular
        app.setActivationPolicy_(policy)
    except Exception:
        pass


_GPU_INFO_CACHE = {"ts": 0.0, "data": {}}


def get_gpu_static_info() -> Dict:
    now = time.time()
    if now - _GPU_INFO_CACHE["ts"] < 300:
        return _GPU_INFO_CACHE["data"]

    data: Dict = {}
    try:
        hw = subprocess.check_output(
            ["/usr/sbin/system_profiler", "SPHardwareDataType", "-json"],
            text=True,
            timeout=4,
        )
        hw_json = json.loads(hw)
        hw_info = (hw_json.get("SPHardwareDataType") or [{}])[0]
        chip = hw_info.get("chip_type") or hw_info.get("machine_model")
        if chip:
            data["model"] = str(chip)
    except Exception:
        pass

    try:
        disp = subprocess.check_output(
            ["/usr/sbin/system_profiler", "SPDisplaysDataType", "-json"],
            text=True,
            timeout=4,
        )
        disp_json = json.loads(disp)
        cores = None

        def scan(obj):
            nonlocal cores
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if cores is None and "core" in k.lower():
                        if isinstance(v, (int, float)):
                            cores = int(v)
                        elif isinstance(v, str) and v.isdigit():
                            cores = int(v)
                    scan(v)
            elif isinstance(obj, list):
                for item in obj:
                    scan(item)

        scan(disp_json.get("SPDisplaysDataType", []))
        if cores:
            data["cores"] = cores
    except Exception:
        pass

    _GPU_INFO_CACHE["ts"] = now
    _GPU_INFO_CACHE["data"] = data
    return data


def read_vm_stat() -> tuple[int, dict]:
    output = subprocess.check_output(["/usr/bin/vm_stat"], text=True)
    page_size = 4096
    stats: dict[str, int] = {}
    for line in output.splitlines():
        if "page size of" in line:
            match = re.search(r"page size of (\d+) bytes", line)
            if match:
                page_size = int(match.group(1))
            continue
        match = re.match(r"^(.+?):\s+([\d,]+)\.", line)
        if match:
            key = match.group(1).strip()
            stats[key] = int(match.group(2).replace(",", ""))
    return page_size, stats


def get_macos_ram(total_bytes: int, mem) -> tuple[int, int, float]:
    """Return (used, available, percent) aligned with Activity Monitor."""
    try:
        page_size, stats = read_vm_stat()
        pages_free = stats.get("Pages free", 0)
        pages_spec = stats.get("Pages speculative", 0)
        pages_active = stats.get("Pages active", 0)
        pages_inactive = stats.get("Pages inactive", 0)
        pages_wired = stats.get("Pages wired down", 0)
        pages_compressed = stats.get("Pages occupied by compressor", 0)
        pages_file_backed = stats.get("File-backed pages", 0)

        available = (pages_file_backed + pages_free + pages_spec) * page_size
        used = max(0, total_bytes - available)
        percent = (used / total_bytes * 100.0) if total_bytes else mem.percent
        return int(used), int(available), percent
    except Exception as exc:
        log_error("macos_ram", exc)
        used = int(mem.total - mem.available)
        percent = (used / mem.total * 100.0) if mem.total else mem.percent
        return used, int(mem.available), percent


_DISK_META_CACHE = {"ts": 0.0, "data": {}}
_DISK_USAGE_CACHE = {"ts": 0.0, "data": None}
_GPU_DEBUG = {"logged": False}


def get_disk_meta() -> Dict:
    """Disk metadata via diskutil + smartctl (macOS). Cached for 60s."""
    now = time.time()
    if now - _DISK_META_CACHE["ts"] < 60:
        return _DISK_META_CACHE["data"]

    data: Dict = {}
    try:
        vol_info = subprocess.check_output(
            ["/usr/sbin/diskutil", "info", "-plist", "/"], text=False
        )
        vol_plist = plistlib.loads(vol_info)
        data["volume_name"] = vol_plist.get("VolumeName")
        device = vol_plist.get("DeviceIdentifier")
        if device:
            # Try whole disk for SMART info
            whole = get_whole_disk(device)
            disk_info = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", whole], text=False
            )
            disk_plist = plistlib.loads(disk_info)
            data["smart_status"] = disk_plist.get("SmartStatus") or disk_plist.get("SMARTStatus")
            data["health"] = data.get("smart_status")

            smart_json, smart_err = run_smartctl_json(f"/dev/{whole}")
            if smart_json:
                log = smart_json.get("nvme_smart_health_information_log", {})
                units_read = to_int(log.get("data_units_read"))
                units_written = to_int(log.get("data_units_written"))
                if units_read is not None:
                    data["smart_total_read"] = format_bytes(units_read * 512000)
                if units_written is not None:
                    data["smart_total_write"] = format_bytes(units_written * 512000)
                temp = to_int(log.get("temperature"))
                if temp is None:
                    temp = to_int(smart_json.get("temperature", {}).get("current"))
                if temp is not None:
                    data["temperature"] = f"{temp:.0f}°C"
                power_cycles = to_int(log.get("power_cycles")) or to_int(smart_json.get("power_cycle_count"))
                if power_cycles is not None:
                    data["power_cycles"] = str(power_cycles)
                power_on = to_int(log.get("power_on_hours")) or to_int(
                    smart_json.get("power_on_time", {}).get("hours")
                )
                if power_on is not None:
                    data["power_on_hours"] = str(power_on)
                # Health percent: prefer percentage_used (NVMe), fallback to available_spare
                pct_used = to_int(log.get("percentage_used"))
                if pct_used is None:
                    pct_used = to_int(smart_json.get("endurance_used", {}).get("current_percent"))
                if pct_used is not None:
                    health_pct = max(0, 100 - pct_used)
                    data["health"] = f"{health_pct}%"
                else:
                    spare_pct = to_int(log.get("available_spare"))
                    if spare_pct is None:
                        spare_pct = to_int(smart_json.get("spare_available", {}).get("current_percent"))
                    if spare_pct is not None:
                        data["health"] = f"{spare_pct}%"
                smart_status = smart_json.get("smart_status", {})
                if "passed" in smart_status:
                    if "health" not in data:
                        data["health"] = "OK" if smart_status["passed"] else "FAIL"
            elif smart_err:
                data["smart_error"] = smart_err
                if not data.get("health"):
                    data["health"] = "ERR"
    except Exception:
        data = {}

    _DISK_META_CACHE["ts"] = now
    _DISK_META_CACHE["data"] = data
    return data


def get_disk_usage_info() -> tuple[int, int, float]:
    """Return (total_bytes, free_bytes, percent_used) using diskutil when possible."""
    now = time.time()
    cached = _DISK_USAGE_CACHE.get("data")
    if cached and (now - _DISK_USAGE_CACHE["ts"] < 30):
        return cached

    def _parse_size(value) -> int | None:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"\(([\d,]+)\s+Bytes\)", value)
            if match:
                return int(match.group(1).replace(",", ""))
        return None

    try:
        # Prefer NSURL resource values to match macOS Storage "Available" exactly.
        try:
            url = NSURL.fileURLWithPath_("/")
            available = None
            for key in (
                NSURLVolumeAvailableCapacityForImportantUsageKey,
                NSURLVolumeAvailableCapacityForOpportunisticUsageKey,
                NSURLVolumeAvailableCapacityKey,
            ):
                ok, value, _err = url.getResourceValue_forKey_error_(None, key, None)
                if ok and value is not None:
                    available = int(value)
                    break
            ok, total_val, _err = url.getResourceValue_forKey_error_(None, NSURLVolumeTotalCapacityKey, None)
            total = int(total_val) if ok and total_val is not None else None
            if total and available and available > 0 and available <= total:
                percent = (1.0 - (available / total)) * 100.0
                percent = max(0.0, min(100.0, percent))
                result = (total, available, percent)
                _DISK_USAGE_CACHE["ts"] = now
                _DISK_USAGE_CACHE["data"] = result
                return result
        except Exception:
            pass

        vol_info = subprocess.check_output(
            ["/usr/sbin/diskutil", "info", "-plist", "/"], text=False
        )
        vol_plist = plistlib.loads(vol_info)

        total = None
        for key in ("TotalSize", "ContainerTotalSize", "VolumeTotalSpace"):
            value = vol_plist.get(key)
            if isinstance(value, int) and value > 0:
                total = value
                break

        free_candidates = []
        for key in (
            "AvailableSpaceForOpportunisticUsage",
            "AvailableSpaceForImportantUsage",
            "AvailableSpace",
            "ContainerFreeSpace",
            "FreeSpace",
            "VolumeFreeSpace",
        ):
            value = vol_plist.get(key)
            if isinstance(value, int) and value > 0:
                free_candidates.append(value)

        if total and free_candidates:
            free = max(free_candidates)
            if free <= total:
                percent = (1.0 - (free / total)) * 100.0
                percent = max(0.0, min(100.0, percent))
                result = (total, free, percent)
                _DISK_USAGE_CACHE["ts"] = now
                _DISK_USAGE_CACHE["data"] = result
                return result
    except Exception:
        pass

    try:
        root_uuid = None
        root_bsd = None
        try:
            root_info = subprocess.check_output(
                ["/usr/sbin/diskutil", "info", "-plist", "/"], text=False
            )
            root_plist = plistlib.loads(root_info)
            root_uuid = root_plist.get("VolumeUUID")
            root_bsd = root_plist.get("DeviceIdentifier")
        except Exception:
            pass

        sp = subprocess.check_output(
            ["/usr/sbin/system_profiler", "SPStorageDataType", "-json"],
            text=True,
            timeout=6,
        )
        data = json.loads(sp)
        entries = data.get("SPStorageDataType", [])

        def iter_volumes():
            for entry in entries:
                volumes = entry.get("volumes")
                if isinstance(volumes, list):
                    for vol in volumes:
                        yield vol
                else:
                    yield entry

        target = None
        if root_uuid:
            for vol in iter_volumes():
                if str(vol.get("volume_uuid", "")).lower() == str(root_uuid).lower():
                    target = vol
                    break
        if target is None and root_bsd:
            for vol in iter_volumes():
                if str(vol.get("bsd_name", "")).lower() == str(root_bsd).lower():
                    target = vol
                    break
        if target is None:
            for vol in iter_volumes():
                if vol.get("mount_point") == "/":
                    target = vol
                    break

        if target:
            total = _parse_size(target.get("size_in_bytes")) or _parse_size(target.get("size"))
            available = (
                _parse_size(target.get("available_space_in_bytes"))
                or _parse_size(target.get("free_space_in_bytes"))
                or _parse_size(target.get("available"))
                or _parse_size(target.get("free"))
            )
            if total and available:
                percent = (1.0 - (available / total)) * 100.0
                percent = max(0.0, min(100.0, percent))
                result = (total, available, percent)
                _DISK_USAGE_CACHE["ts"] = now
                _DISK_USAGE_CACHE["data"] = result
                return result
    except Exception:
        pass

    try:
        df_out = subprocess.check_output(["/bin/df", "-k", "/"], text=True)
        lines = [line for line in df_out.strip().splitlines() if line.strip()]
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 5:
                total_k = int(parts[1])
                free_k = int(parts[3])
                total = total_k * 1024
                free = free_k * 1024
                if total > 0 and free >= 0:
                    percent = (1.0 - (free / total)) * 100.0
                    percent = max(0.0, min(100.0, percent))
                    result = (total, free, percent)
                    _DISK_USAGE_CACHE["ts"] = now
                    _DISK_USAGE_CACHE["data"] = result
                    return result
    except Exception:
        pass

    usage = psutil.disk_usage("/")
    total = int(usage.total)
    free = int(usage.free)
    percent = usage.percent
    result = (total, free, percent)
    _DISK_USAGE_CACHE["ts"] = now
    _DISK_USAGE_CACHE["data"] = result
    return result


def _parse_gpu_metrics(output: str) -> dict:
    percent = None
    freq_mhz = None
    power_mw = None
    lines = output.splitlines()
    for line in lines:
        if re.search(r"gpu.*active\s*residency", line, re.IGNORECASE):
            match = re.search(r"([0-9.]+)\s*%", line)
            if match:
                percent = float(match.group(1))
                break
    if percent is None:
        for line in lines:
            if re.search(r"gpu.*idle\s*residency", line, re.IGNORECASE):
                match = re.search(r"([0-9.]+)\s*%", line)
                if match:
                    idle = float(match.group(1))
                    percent = max(0.0, min(100.0, 100.0 - idle))
                    break
    for line in lines:
        if re.search(r"gpu.*active\s*frequency", line, re.IGNORECASE):
            match = re.search(r"([0-9.]+)\s*MHz", line, re.IGNORECASE)
            if match:
                freq_mhz = float(match.group(1))
                break
    for line in lines:
        if re.search(r"gpu.*power", line, re.IGNORECASE):
            match = re.search(r"([0-9.]+)\s*(mW|W)", line, re.IGNORECASE)
            if match:
                value = float(match.group(1))
                unit = match.group(2).lower()
                power_mw = value * 1000.0 if unit == "w" else value
                break
    return {"percent": percent, "freq_mhz": freq_mhz, "power_mw": power_mw}


def read_gpu_metrics_powermetrics() -> dict:
    """Return GPU metrics via powermetrics (Apple Silicon)."""
    powermetrics = "/usr/bin/powermetrics"
    if not os.path.exists(powermetrics):
        return {"percent": None, "freq_mhz": None, "power_mw": None}

    def _run(cmd):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=4,
            )
        except Exception as exc:
            if not _GPU_DEBUG["logged"]:
                _GPU_DEBUG["logged"] = True
                log_error("gpu_powermetrics_run", exc)
            return None

    try:
        result = _run(["sudo", "-n", powermetrics, "--samplers", "gpu_power", "-n", "1", "-i", "1000"])
        output = ""
        if result is not None:
            output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        if result is None or result.returncode != 0 or not output.strip():
            result = _run([powermetrics, "--samplers", "gpu_power", "-n", "1", "-i", "1000"])
            output = ""
            if result is not None:
                output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")

        if result is None or result.returncode != 0 or not output.strip():
            if not _GPU_DEBUG["logged"]:
                _GPU_DEBUG["logged"] = True
                log_error("gpu_powermetrics", Exception(f"rc={getattr(result, 'returncode', None)} out={output[:400]}"))
            return {"percent": None, "freq_mhz": None, "power_mw": None}

        metrics = _parse_gpu_metrics(output)
        if metrics["percent"] is None and metrics["freq_mhz"] is None and metrics["power_mw"] is None:
            if not _GPU_DEBUG["logged"]:
                _GPU_DEBUG["logged"] = True
                log_error("gpu_powermetrics_parse", Exception(output[:400]))
        return metrics
    except Exception as exc:
        if not _GPU_DEBUG["logged"]:
            _GPU_DEBUG["logged"] = True
            log_error("gpu_powermetrics_exc", exc)
        return {"percent": None, "freq_mhz": None, "power_mw": None}


def read_gpu_usage_powermetrics() -> float | None:
    """Return GPU active residency % via powermetrics (Apple Silicon)."""
    return read_gpu_metrics_powermetrics().get("percent")


def read_gpu_perfstats_ioreg() -> dict:
    """Read GPU utilization stats from IORegistry (Apple Silicon)."""
    try:
        result = subprocess.run(
            ["/usr/sbin/ioreg", "-l", "-w", "0", "-r", "-c", "IOAccelerator"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode != 0:
            return {"device": None, "render": None, "tiler": None}
        text = result.stdout or ""
        device = None
        render = None
        tiler = None
        match = re.search(r'"Device Utilization %"\s*=\s*([0-9.]+)', text)
        if match:
            device = float(match.group(1))
        match = re.search(r'"Renderer Utilization %"\s*=\s*([0-9.]+)', text)
        if match:
            render = float(match.group(1))
        match = re.search(r'"Tiler Utilization %"\s*=\s*([0-9.]+)', text)
        if match:
            tiler = float(match.group(1))
        # Some systems report "GPU Utilization %"
        if device is None:
            match = re.search(r'"GPU Utilization %"\s*=\s*([0-9.]+)', text)
            if match:
                device = float(match.group(1))
        return {"device": device, "render": render, "tiler": tiler}
    except Exception:
        return {"device": None, "render": None, "tiler": None}


def read_gpu_metrics() -> dict:
    """Return GPU utilization metrics (device/render/tiler)."""
    stats = read_gpu_perfstats_ioreg()
    return {
        "device": stats.get("device"),
        "render": stats.get("render"),
        "tiler": stats.get("tiler"),
    }


def can_read_gpu_ioreg() -> bool:
    stats = read_gpu_perfstats_ioreg()
    return any(value is not None for value in stats.values())


def find_smartctl() -> str | None:
    brew = find_brew()
    if brew:
        try:
            prefix = subprocess.check_output([brew, "--prefix"], text=True, timeout=3).strip()
            if prefix:
                candidate = Path(prefix) / "sbin" / "smartctl"
                if candidate.exists():
                    return str(candidate)
        except Exception:
            pass
    candidates = [
        shutil.which("smartctl"),
        "/opt/homebrew/sbin/smartctl",
        "/opt/homebrew/bin/smartctl",
        "/usr/local/sbin/smartctl",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def run_smartctl_json(device: str) -> tuple[dict | None, str | None]:
    """Run smartctl and return parsed JSON, with admin prompt if allowed."""
    smartctl_path = find_smartctl()
    if not smartctl_path:
        return None, "smartctl not found"

    def _run(cmd):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            return out, err
        except Exception as exc:
            return "", str(exc)

    def _parse_json(output: str) -> tuple[dict | None, str | None]:
        if not output:
            return None, None
        # Try raw JSON first
        try:
            return json.loads(output), None
        except Exception:
            pass
        # Extract JSON block
        start = output.find("{")
        end = output.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(output[start : end + 1]), None
            except Exception as exc:
                return None, f"smartctl parse failed: {exc}"
        return None, "smartctl output not JSON"

    stdout, stderr = _run(["sudo", "-n", smartctl_path, "-a", "-j", "-d", "nvme", device])
    combined = (stdout or "") + ("\n" + stderr if stderr else "")
    parsed, err = _parse_json(combined)
    if parsed:
        return parsed, None

    # Fallback without -d nvme
    stdout, stderr = _run(["sudo", "-n", smartctl_path, "-a", "-j", device])
    combined = (stdout or "") + ("\n" + stderr if stderr else "")
    parsed, err2 = _parse_json(combined)
    if parsed:
        return parsed, None
    if err2:
        err = err2

    if ALLOW_ADMIN_PROMPT:
        cmd = f'"{smartctl_path}" -a -j -d nvme {device} 2>/dev/null'
        ok, out = run_osascript(cmd, admin=True, timeout=10)
        if ok and out:
            parsed, err3 = _parse_json(out)
            if parsed:
                return parsed, None
            err = err3
        # Try without -d nvme
        cmd = f'"{smartctl_path}" -a -j {device} 2>/dev/null'
        ok, out = run_osascript(cmd, admin=True, timeout=10)
        if ok and out:
            parsed, err4 = _parse_json(out)
            if parsed:
                return parsed, None
            err = err4
        return None, err or "smartctl failed"

    return None, err or stderr or "smartctl failed"


def get_whole_disk(device_identifier: str) -> str:
    cleaned = device_identifier.replace("/dev/", "")
    match = re.search(r"(disk\d+)", cleaned)
    if match:
        return match.group(1)
    # Fallback: split at first 's' (diskXsY...)
    if cleaned.startswith("disk") and "s" in cleaned:
        return cleaned.split("s", 1)[0]
    return cleaned


def get_root_disk_device() -> str:
    """Return the whole disk device for root volume, e.g. /dev/disk0."""
    try:
        vol_info = subprocess.check_output(
            ["/usr/sbin/diskutil", "info", "-plist", "/"], text=False
        )
        vol_plist = plistlib.loads(vol_info)
        device = vol_plist.get("DeviceIdentifier") or "disk0"
        whole = get_whole_disk(device)
        return f"/dev/{whole}"
    except Exception:
        return "/dev/disk0"


def to_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "")
        match = re.search(r"(\\d+)", cleaned)
        if match:
            return int(match.group(1))
    return None


def find_brew() -> str | None:
    brew = "/opt/homebrew/bin/brew"
    return brew if os.path.exists(brew) else None


def _escape_osascript(cmd: str) -> str:
    return cmd.replace("\\", "\\\\").replace("\"", "\\\"")


def run_osascript(cmd: str, admin: bool = False, timeout: int = 600) -> tuple[bool, str]:
    escaped = _escape_osascript(cmd)
    if admin:
        script = f'do shell script "{escaped}" with administrator privileges'
    else:
        script = f'do shell script "{escaped}"'
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = ""
    if result.stdout:
        output += result.stdout.strip()
    if result.stderr:
        output += ("\n" if output else "") + result.stderr.strip()
    return result.returncode == 0, output


class InstallProgressDialog(QtWidgets.QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Enabling GPU/SMART")
        self.setModal(True)
        self.setFixedSize(360, 140)

        layout = QtWidgets.QVBoxLayout(self)
        self.label = QtWidgets.QLabel("Preparing...")
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.label)
        layout.addWidget(self.progress)

    def set_status(self, text: str) -> None:
        self.label.setText(text)


class PrivilegeWorker(QtCore.QThread):
    status = QtCore.Signal(str)
    done = QtCore.Signal(bool, str)

    def run(self) -> None:
        if can_run_powermetrics() and can_run_smartctl():
            self.done.emit(True, "Already enabled.")
            return

        if not find_brew():
            self.status.emit("Installing Homebrew...")
            ok, msg = ensure_homebrew()
            if not ok:
                self.done.emit(False, msg)
                return

        self.status.emit("Installing smartmontools...")
        ok, msg = ensure_smartmontools()
        if not ok:
            self.done.emit(False, msg)
            return

        self.status.emit("Configuring permissions...")
        ok, msg = setup_privileged_access(None)
        if not ok:
            self.done.emit(False, msg)
            return

        self.done.emit(True, "Enabled.")


def ensure_homebrew() -> tuple[bool, str]:
    """Ensure Homebrew is installed (Apple Silicon)."""
    if find_brew():
        return True, "Homebrew already installed."
    username = getpass.getuser()
    script = (
        f"/bin/mkdir -p /opt/homebrew; "
        f"/usr/sbin/chown -R {username}:admin /opt/homebrew; "
        f"/usr/bin/su -l {username} -c "
        f"\"NONINTERACTIVE=1 /bin/bash -c '\\\"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\\\"'\""
    )
    ok, out = run_osascript(script, admin=True, timeout=900)
    if ok and find_brew():
        return True, out or "Homebrew installed."
    return False, out or "Homebrew install failed."


def ensure_smartmontools() -> tuple[bool, str]:
    """Ensure smartmontools is installed."""
    if find_smartctl():
        return True, "smartmontools already installed."
    brew = find_brew()
    if not brew:
        return False, "Homebrew not found."
    env = os.environ.copy()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    try:
        result = subprocess.run(
            [brew, "install", "smartmontools"],
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        combined = output + ("\n" + error if error else "")
        if find_smartctl():
            return True, combined or "smartmontools installed."
        if "already installed" in combined.lower():
            return True, combined
    except Exception as exc:
        combined = f"Direct install failed: {exc}"

    # Fallback: run via osascript with explicit PATH
    cmd = f'export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; {brew} install smartmontools 2>&1'
    ok, out = run_osascript(cmd, admin=False, timeout=600)
    if find_smartctl():
        return True, out or "smartmontools installed."
    return False, out or combined or "smartmontools install failed."


def can_run_powermetrics() -> bool:
    try:
        result = subprocess.run(
            ["sudo", "-n", "/usr/bin/powermetrics", "--samplers", "gpu_power", "-n", "1", "-i", "1000"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        return result.returncode == 0
    except Exception:
        return False


def can_run_smartctl() -> bool:
    smartctl_path = find_smartctl()
    if not smartctl_path:
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", smartctl_path, "-a", "-j", "-d", "nvme", get_root_disk_device()],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.returncode == 0 or bool(result.stdout)
    except Exception:
        return False


def setup_privileged_access(parent: QtWidgets.QWidget | None = None) -> tuple[bool, str]:
    username = getpass.getuser()
    smartctl_path = find_smartctl()
    if not smartctl_path:
        return False, "smartctl not found."

    sudoers_line = (
        f"{username} ALL=(root) NOPASSWD: /usr/bin/powermetrics, {smartctl_path}"
    )
    cmd = (
        f"mkdir -p /etc/sudoers.d; "
        f"echo '{sudoers_line}' > {SUDOERS_FILE}; "
        f"chmod 440 {SUDOERS_FILE}"
    )
    ok, out = run_osascript(cmd, admin=True, timeout=30)
    return ok, out or ("Permissions enabled." if ok else "Permission setup failed.")


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("This build is for macOS only.")
    if platform.machine() != "arm64":
        raise SystemExit("This build is for Apple Silicon (arm64) only.")

    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = AppController()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
