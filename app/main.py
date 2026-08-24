"""Diem khoi dong ung dung: tao QApplication, doc config, hien cua so chinh."""

from __future__ import annotations

import os
import sys

from PySide6.QtWidgets import QApplication

from .config import load_config
from .main_window import ManagerWindow


def main() -> None:
    if os.name != "nt":
        print("Canh bao: chuong trinh nay duoc viet cho Windows (taskkill / CTRL_BREAK).")
    app = QApplication(sys.argv)
    app.setApplicationName("Service Manager")
    window = ManagerWindow(load_config())
    window.show()
    sys.exit(app.exec())
