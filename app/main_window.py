"""Cua so chinh: danh sach the module ben trai, tab log ben phai."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .config import THIS_DIR
from .constants import CRASHED
from .log_pane import LogPane
from .service_card import ServiceCard
from .service_process import ServiceProcess
from .style import STYLESHEET


class ManagerWindow(QMainWindow):
    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__()
        self.setWindowTitle("Service Manager - Camera / VRS")
        self.resize(1420, 860)

        self.config = config
        self.max_lines = int(config.get("max_log_lines", 4000))
        self.start_gap = float(config.get("start_all_gap_sec", 3))

        python_exe = str(config.get("python") or sys.executable)
        if not Path(python_exe).exists():
            python_exe = sys.executable
        self.python_exe = python_exe

        log_dir = Path(config.get("log_dir", "logs"))
        if not log_dir.is_absolute():
            log_dir = THIS_DIR / log_dir
        self.log_dir = log_dir

        self.services: Dict[str, ServiceProcess] = {}
        self.cards: Dict[str, ServiceCard] = {}
        self.panes: Dict[str, LogPane] = {}
        self._start_queue: List[str] = []

        self._build_ui(config.get("services", []))

        self.ui_timer = QTimer(self)
        self.ui_timer.timeout.connect(self._refresh_all)
        self.ui_timer.start(1000)

        interval = int(config.get("health_interval_sec", 5)) * 1000
        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(self._run_health_checks)
        self.health_timer.start(max(2000, interval))

    # --- dung giao dien -------------------------------------------------
    def _build_ui(self, service_cfgs: List[Dict[str, Any]]) -> None:
        # Thanh cong cu tren cung
        top = QHBoxLayout()
        title = QLabel("Service Manager")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setBold(True)
        title.setFont(title_font)

        self.summary = QLabel("")
        self.summary.setObjectName("summary")

        btn_start_all = QPushButton("Bat tat ca")
        btn_start_all.setObjectName("primaryBtn")
        btn_start_all.clicked.connect(self.start_all)

        btn_stop_all = QPushButton("Dung tat ca")
        btn_stop_all.setProperty("variant", "stop")
        btn_stop_all.clicked.connect(self.stop_all)

        btn_logs = QPushButton("Mo thu muc log")
        btn_logs.clicked.connect(self._open_log_dir)

        top.addWidget(title)
        top.addSpacing(16)
        top.addWidget(self.summary)
        top.addStretch(1)
        top.addWidget(btn_start_all)
        top.addWidget(btn_stop_all)
        top.addWidget(btn_logs)

        # Cot trai: danh sach module
        cards_host = QWidget()
        self.cards_layout = QVBoxLayout(cards_host)
        self.cards_layout.setContentsMargins(4, 4, 4, 4)
        self.cards_layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_host)
        scroll.setMinimumWidth(430)
        scroll.setObjectName("cardScroll")

        # Cot phai: log
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        for cfg in service_cfgs:
            self._add_service(cfg)
        self.cards_layout.addStretch(1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([440, 980])

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.addLayout(top)
        root.addSpacing(8)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.setStyleSheet(STYLESHEET)

    def _add_service(self, cfg: Dict[str, Any]) -> None:
        svc = ServiceProcess(cfg, self.python_exe, self.log_dir, self.max_lines, parent=self)
        self.services[svc.name] = svc

        card = ServiceCard(svc)
        card.start_requested.connect(self.start_service)
        card.stop_requested.connect(self.stop_service)
        card.focus_requested.connect(self._focus_tab)
        self.cards[svc.name] = card
        self.cards_layout.addWidget(card)

        pane = LogPane(svc, self.max_lines)
        self.panes[svc.name] = pane
        self.tabs.addTab(pane, svc.label)

        svc.log_line.connect(self._on_log_line)
        svc.state_changed.connect(self._on_state_changed)

    # --- su kien --------------------------------------------------------
    def _on_log_line(self, name: str, line: str) -> None:
        pane = self.panes.get(name)
        if pane is not None:
            pane.add_line(line)

    def _on_state_changed(self, name: str) -> None:
        svc = self.services[name]
        card = self.cards.get(name)
        if card is not None:
            card.refresh()
        self._update_summary()

        if svc.state == CRASHED and svc.auto_restart and not svc.is_tool:
            svc.restart_count += 1
            if svc.restart_count <= 5:
                QTimer.singleShot(3000, lambda: self.start_service(name))
            else:
                svc._emit("### Da bat lai qua 5 lan - dung tu dong bat lai.")

    def _focus_tab(self, name: str) -> None:
        pane = self.panes.get(name)
        if pane is not None:
            self.tabs.setCurrentWidget(pane)

    # --- dieu khien -----------------------------------------------------
    def start_service(self, name: str) -> None:
        svc = self.services[name]
        card = self.cards[name]
        args = card.current_args()
        if args is not None:
            svc.args = args
        svc.start(args)
        self._focus_tab(name)
        card.refresh()

    def stop_service(self, name: str) -> None:
        self.services[name].stop()
        self.cards[name].refresh()

    def start_all(self) -> None:
        """Bat lan luot theo thu tu trong services.yaml, cach nhau vai giay.

        Cac tool (chay 1 lan) khong nam trong Start All - phai bam Chay thu cong.
        """
        self._start_queue = [n for n, s in self.services.items()
                             if not s.is_tool and not s.is_alive]
        self._start_next()

    def _start_next(self) -> None:
        if not self._start_queue:
            return
        name = self._start_queue.pop(0)
        self.start_service(name)
        if self._start_queue:
            QTimer.singleShot(int(self.start_gap * 1000), self._start_next)

    def stop_all(self) -> None:
        self._start_queue.clear()
        # Dung theo thu tu nguoc: app phu thuoc dung truoc, ha tang dung sau
        for name in reversed(list(self.services.keys())):
            self.services[name].stop()

    def _open_log_dir(self) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.log_dir))  # noqa: S606

    # --- cap nhat dinh ky ------------------------------------------------
    def _refresh_all(self) -> None:
        for card in self.cards.values():
            card.refresh()
        self._update_summary()

    def _update_summary(self) -> None:
        services = [s for s in self.services.values() if not s.is_tool]
        running = sum(1 for s in services if s.is_alive)
        crashed = sum(1 for s in self.services.values() if s.state == CRASHED)
        text = f"{running}/{len(services)} module dang chay"
        if crashed:
            text += f"   ·   {crashed} module loi"
        self.summary.setText(text)

    def _run_health_checks(self) -> None:
        targets = [s for s in self.services.values() if s.health_url and s.is_alive]
        if not targets:
            return

        def worker() -> None:
            for svc in targets:
                svc.check_health()

        threading.Thread(target=worker, daemon=True, name="health").start()

    # --- thoat ------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        alive = [s.label for s in self.services.values() if s.is_alive]
        if alive:
            answer = QMessageBox.question(
                self,
                "Thoat Service Manager",
                "Cac module dang chay:\n  - " + "\n  - ".join(alive)
                + "\n\nDung tat ca va thoat?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Yes:
                self.stop_all()
                deadline = time.time() + 8
                while time.time() < deadline and any(s.is_alive for s in self.services.values()):
                    QApplication.processEvents()
                    time.sleep(0.1)
        for svc in self.services.values():
            svc.close_log()
        event.accept()
