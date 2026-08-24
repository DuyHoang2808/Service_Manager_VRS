"""Config dùng chung cho các CLI tool trong thư mục này (run_auto_board_offset.py,
calibrate_camera_axis.py) - tránh lặp lại default IP/port PLC ở nhiều nơi.

Đọc/ghi file YAML "cli_config.yaml" cạnh file này (tự tạo với giá trị mặc định nếu
chưa có). Các giá trị này chỉ là DEFAULT cho cờ dòng lệnh - vẫn có thể override qua
--base-url/--plc-pc-ip/--plc-ip/--plc-port khi chạy.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import yaml
from pydantic import BaseModel

# Dong goi PyInstaller (onedir): __file__ tro vao ben trong bundle, khong phai
# thu muc chua file .exe - phai dung sys.executable de tim dung cli_config.yaml
# nam canh file .exe khi da build.
RUNTIME_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent)
CONFIG_FILE = RUNTIME_DIR / "cli_config.yaml"


class CliConfigModel(BaseModel):
    """Default dùng chung cho các CLI tool bù lệch board / hiệu chỉnh trục camera."""

    base_url: str = "http://localhost:8093"
    plc_pc_ip: str = "192.168.3.101"
    plc_ip: str = "192.168.3.1"
    plc_port: int = 9600


DEFAULT_CONFIG: Dict[str, Any] = CliConfigModel().model_dump()


def save_config(config: Dict[str, Any]) -> None:
    """Persist CLI config to YAML file."""
    CONFIG_FILE.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_config() -> Dict[str, Any]:
    """Load CLI config from YAML, creating the file with defaults if missing."""
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    try:
        loaded = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Config root must be a YAML mapping")

        merged = DEFAULT_CONFIG.copy()
        merged.update({k: v for k, v in loaded.items() if k in merged})

        if merged != loaded:
            save_config(merged)
        return merged
    except Exception as exc:
        print(f"⚠️  Không đọc được {CONFIG_FILE}, dùng giá trị mặc định: {exc}")
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


CONFIG: Dict[str, Any] = load_config()
