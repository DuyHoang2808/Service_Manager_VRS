"""Duong dan file cau hinh va ham doc services.yaml."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import yaml

# Khi service_manager.py tu no cung duoc dong goi thanh .exe (PyInstaller),
# __file__ tro vao ben trong bundle - phai dung sys.executable de tim dung
# services.yaml nam canh file .exe. Khi chay tu source, file nay nam trong
# app/ nen phai lui len 1 cap de ve thu muc goc Service_Manager/.
THIS_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parent.parent)
CONFIG_FILE = THIS_DIR / "services.yaml"


def load_config() -> Dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise SystemExit(f"Khong tim thay file cau hinh: {CONFIG_FILE}")
    data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not data.get("services"):
        raise SystemExit(f"File cau hinh khong hop le (thieu muc 'services'): {CONFIG_FILE}")
    return data
