"""The dieu khien (bat/tat, trang thai, tham so) cho 1 module."""

from __future__ import annotations

import time
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from .constants import STARTING, STATE_COLOR, STATE_TEXT, STOPPING, fmt_uptime
from .service_process import ServiceProcess


class ServiceCard(QFrame):
    start_requested = Signal(str)
    stop_requested = Signal(str)
    focus_requested = Signal(str)

    def __init__(self, svc: ServiceProcess, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.svc = svc
        self.setObjectName("card")
        self.setFrameShape(QFrame.StyledPanel)

        grid = QGridLayout(self)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        self.dot = QLabel("●")
        self.dot.setObjectName("dot")
        font = self.dot.font()
        font.setPointSize(15)
        self.dot.setFont(font)

        self.title = QLabel(svc.label)
        self.title.setObjectName("cardTitle")
        title_font = self.title.font()
        title_font.setBold(True)
        title_font.setPointSize(title_font.pointSize() + 1)
        self.title.setFont(title_font)

        kind_text = {"service": "SERVICE", "gui": "GUI", "tool": "CLI"}.get(svc.kind, "SERVICE")
        self.badge = QLabel(kind_text)
        self.badge.setObjectName("badge")

        # Khong dat objectName o day: selector ID (#primaryBtn) uu tien cao hon
        # selector thuoc tinh ([variant="stop"]) nen nut se ket mau xanh, khong doi
        # sang do duoc. Mau do refresh() dieu khien qua property "variant".
        self.btn = QPushButton("Bat")
        self.btn.setMinimumWidth(88)
        self.btn.clicked.connect(self._on_button)

        self.status = QLabel("Da dung")
        self.status.setObjectName("statusText")

        self.meta = QLabel(svc.note)
        self.meta.setObjectName("metaText")
        self.meta.setWordWrap(True)

        grid.addWidget(self.dot, 0, 0)
        grid.addWidget(self.title, 0, 1)
        grid.addWidget(self.badge, 0, 2)
        grid.addWidget(self.btn, 0, 3)
        grid.addWidget(self.status, 1, 1, 1, 3)
        grid.addWidget(self.meta, 2, 1, 1, 3)
        grid.setColumnStretch(1, 1)

        row = 3
        if svc.is_tool:
            self.args_edit = QLineEdit(svc.args)
            self.args_edit.setPlaceholderText("Tham so dong lenh, vd: --board-id BOARD-001 --board-side B")
            self.args_edit.setObjectName("argsEdit")
            args_row = QHBoxLayout()
            args_row.setSpacing(8)
            args_row.addWidget(QLabel("Tham so:"))
            args_row.addWidget(self.args_edit, 1)
            grid.addLayout(args_row, row, 1, 1, 3)
            row += 1
        else:
            self.args_edit = None
            self.auto_cb = QCheckBox("Tu bat lai khi module chet")
            self.auto_cb.toggled.connect(lambda on: setattr(svc, "auto_restart", on))
            grid.addWidget(self.auto_cb, row, 1, 1, 3)
            row += 1

        self.refresh()

    def _on_button(self) -> None:
        if self.svc.is_alive and not self.svc.is_tool:
            self.stop_requested.emit(self.svc.name)
        elif self.svc.is_alive and self.svc.is_tool:
            self.stop_requested.emit(self.svc.name)
        else:
            self.start_requested.emit(self.svc.name)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.focus_requested.emit(self.svc.name)
        super().mouseDoubleClickEvent(event)

    def current_args(self) -> Optional[str]:
        return self.args_edit.text() if self.args_edit is not None else None

    def refresh(self) -> None:
        svc = self.svc
        self.dot.setStyleSheet(f"color: {STATE_COLOR[svc.state]};")

        parts = [STATE_TEXT[svc.state]]
        if svc.is_alive and svc.proc is not None:
            parts.append(f"PID {svc.proc.pid}")
            parts.append(f"chay {fmt_uptime(time.time() - svc.started_at)}")
        if svc.is_alive and svc.healthy is True:
            parts.append("health OK")
        elif svc.is_alive and svc.healthy is False:
            parts.append("chua phan hoi")
        if svc.restart_count:
            parts.append(f"da bat lai {svc.restart_count} lan")
        self.status.setText("  ·  ".join(parts))

        if svc.is_alive:
            self.btn.setText("Dung" if not svc.is_tool else "Huy")
            self.btn.setProperty("variant", "stop")
        else:
            self.btn.setText("Chay" if svc.is_tool else "Bat")
            self.btn.setProperty("variant", "start")
        self.btn.setEnabled(svc.state not in (STARTING, STOPPING) or svc.is_alive)
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)
