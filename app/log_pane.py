"""Tab log (stdout/stderr) cua 1 module: loc, tam dung cuon, mo file/thu muc."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from PySide6.QtGui import QFont, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .service_process import ServiceProcess


class LogPane(QWidget):
    def __init__(self, svc: ServiceProcess, max_lines: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.svc = svc
        self._filter = ""

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 6)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Loc log (vd: ERROR, offset, 8083)...")
        self.filter_edit.textChanged.connect(self._on_filter)

        self.autoscroll = QCheckBox("Tu cuon")
        self.autoscroll.setChecked(True)

        self.wrap_cb = QCheckBox("Xuong dong")
        self.wrap_cb.setChecked(False)
        self.wrap_cb.toggled.connect(self._on_wrap)

        clear_btn = QPushButton("Xoa man hinh")
        clear_btn.clicked.connect(self._clear)

        open_btn = QPushButton("Mo file log hom nay")
        open_btn.clicked.connect(self._open_file)

        folder_btn = QPushButton("Mo thu muc")
        folder_btn.clicked.connect(self._open_folder)

        bar.addWidget(self.filter_edit, 1)
        bar.addWidget(self.autoscroll)
        bar.addWidget(self.wrap_cb)
        bar.addWidget(clear_btn)
        bar.addWidget(open_btn)
        bar.addWidget(folder_btn)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(max_lines)
        self.view.setWordWrapMode(QTextOption.NoWrap)
        self.view.setObjectName("logView")
        self.view.setFont(QFont("Consolas", 10))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

    # --- hien thi ------------------------------------------------------
    @staticmethod
    def _color_for(line: str) -> Optional[str]:
        upper = line.upper()
        if line.lstrip("[0123456789:] ").startswith("###"):
            return "#93c5fd"
        if any(k in upper for k in ("ERROR", "TRACEBACK", "EXCEPTION", "FAILED", "THAT BAI", "❌")):
            return "#f87171"
        if any(k in upper for k in ("WARNING", "WARN", "CANH BAO")):
            return "#fbbf24"
        if any(k in upper for k in ("INFO", "STARTED", "✅", "OK")):
            return "#86efac"
        return None

    def _append(self, line: str) -> None:
        if self._filter and self._filter not in line.lower():
            return
        color = self._color_for(line)
        if color:
            escaped = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            self.view.appendHtml(f'<span style="color:{color};">{escaped}</span>')
        else:
            self.view.appendPlainText(line)
        if self.autoscroll.isChecked():
            self.view.moveCursor(QTextCursor.End)

    def add_line(self, line: str) -> None:
        self._append(line)

    def _rebuild(self) -> None:
        self.view.clear()
        for line in self.svc.buffered_lines():
            self._append(line)

    def _on_filter(self, text: str) -> None:
        self._filter = text.strip().lower()
        self._rebuild()

    def _on_wrap(self, on: bool) -> None:
        self.view.setWordWrapMode(QTextOption.WrapAnywhere if on else QTextOption.NoWrap)

    def _clear(self) -> None:
        self.view.clear()

    def _open_file(self) -> None:
        path = self.svc.log_path_for(datetime.now())
        if not path.exists():
            QMessageBox.information(
                self, "Log",
                f"Hom nay module nay chua ghi log nao.\n\nFile se nam o:\n{path}")
            return
        os.startfile(str(path))  # noqa: S606 - mo bang trinh xem mac dinh cua Windows

    def _open_folder(self) -> None:
        """Mo thu muc rieng cua module (chua log cua tat ca cac ngay)."""
        folder = self.svc.log_root / self.svc.name
        folder.mkdir(parents=True, exist_ok=True)
        os.startfile(str(folder))  # noqa: S606
