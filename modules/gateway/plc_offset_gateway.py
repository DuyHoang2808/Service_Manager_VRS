"""
PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat) - REST API for PLC + Camera + Auto Board-Offset (YOLO)

Đây là bản CLONE ĐỘC LẬP của `plc_gateway_api_optimized_v2.py` (dự án gốc), đặt trong
folder `AutoBoardOffset_YOLO_2Mat/gateway/` để không đụng vào code production đang chạy.
KHÔNG import gì từ bên ngoài folder này (trừ các thư viện pip chuẩn).

Workflow gốc (giữ nguyên, không đổi):
1. Nhận toạ độ, gửi PLC Omron (X, Y, Trigger)
2. Đợi PLC di chuyển xong (motion-status / done-busy / timeout)
3. Chụp ảnh từ Sony streamer snapshot endpoint
4. (tuỳ endpoint) gửi ảnh sang AI Detection API để tìm lỗi board

Phần MỚI thêm trong bản clone này (theo kế hoạch `docs/Ke_hoach_tu_dong_bu_lech_board.md`):
- Import lại các hàm THUẦN (không đổi) từ `bu_lech_board.py` cùng thư mục:
  ANCHOR_POINTS_POOL, ANCHOR_SETS, board_to_plc, kabsch_2d, apply_rigid_offset,
  load_calibration_matrix.
- `FiducialClient`: gọi sang Fiducial Detector Service (YOLO riêng biệt, service khác,
  xem `fiducial_detector/fiducial_service.py`) để tìm tâm vòng tròn mốc trong ảnh.
- `pixel_offset_to_plc_mm()`: quy đổi độ lệch pixel (so với tâm ảnh) sang độ lệch mm
  THEO ĐÚNG TRỤC PLC, dùng ma trận hiệu chỉnh `camera_axis_matrix` (T).
- Endpoint MỚI `/api/calib/auto-board-offset`: tự động hoá toàn bộ quy trình
  bu_lech_board.py (thay vì operator jog bằng mắt + gõ tay PLC X/Y).
- Endpoint MỚI `/api/calib/camera-axis`: hiệu chỉnh ma trận T (làm 1 lần khi setup máy
  / khi tháo lắp lại camera - KHÔNG PHẢI mỗi khi đổi board).
- Endpoint MỚI `/api/calib/board-to-plc`: xem trước toạ độ PLC đã bù lệch cho 1 điểm
  Board X/Y bất kỳ, dùng offset đã lưu gần nhất.
- Endpoint MỚI `/api/calib/offset-status`: đọc nội dung offset_runtime.json hiện tại.

Port mặc định: 8093 (khác 8083 của gateway gốc, để chạy song song không đụng độ khi
đang thử nghiệm/song song với hệ thống production).
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import logging
import math
import os
import struct
import sys
import time
import traceback
from contextlib import asynccontextmanager
from dataclasses import dataclass, field as dc_field
from math import hypot
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import requests
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ===============================
# Import lại logic THUẦN từ bu_lech_board.py (CÙNG THƯ MỤC, không sửa file đó)
# ===============================
# Dong goi PyInstaller (onedir): __file__ tro vao ben trong bundle - dung
# get_runtime_dir() (dinh nghia ben duoi, da frozen-aware) thay vi __file__ tho,
# de tim dung ClassLibrary.dll va bu_lech_board.py canh file .exe khi da build.
if getattr(sys, "frozen", False):
    THIS_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from bu_lech_board import (  # noqa: E402
    ANCHOR_POINTS_POOL,
    ANCHOR_SETS,
    apply_rigid_offset,
    board_to_plc,
    kabsch_2d,
    load_calibration_matrix,
)

# ===============================
# Logging
# ===============================
# Khi dong goi bang PyInstaller, bootloader khong ton trong PYTHONIOENCODING nhu
# python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8 tai code, neu
# khong logger in emoji/tieng Viet co dau se nem UnicodeEncodeError va crash exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("PLCOffsetGateway")

# ===============================
# Constants (giống hệt gateway gốc)
# ===============================
PLC_MOTION_TIMEOUT_SAFETY_FACTOR = 1.5
PLC_MOTION_TIMEOUT_OVERHEAD_MS = 1000
PLC_MOTION_MIN_COMPLETION_RATIO = 0.8
PLC_MOTION_MIN_COMPLETION_FLOOR_MS = 300
CAMERA_SNAPSHOT_FRESH_FETCHES = 1
CAMERA_SNAPSHOT_FRESH_DELAY_MS = 120
PLC_ALREADY_IN_POSITION_TOLERANCE_MM = 0.5

MOTION_ACTIVE_D466 = frozenset({2, 100, 600, 65136, 65386, 65448, 65511})
MOTION_ACTIVE_D469 = frozenset({16472, 16473, 49240, 49241})

ALLOWED_SNAPSHOT_CONTENT_TYPES = ("image/jpeg", "image/jpg", "image/png")

DEFAULT_PLC_CONN = {"pc_ip": "192.168.3.101", "plc_ip": "192.168.3.1", "port": 9600}


def get_runtime_dir() -> Path:
    """Return the directory where config/data should live (cùng thư mục file này)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


RUNTIME_DIR = get_runtime_dir()
CONFIG_FILE = RUNTIME_DIR / "plc_offset_gateway_config.json"
# Mai nhớ sauwr chỗ này


# ===============================
# Persistent gateway config
# ===============================
class GatewayConfigModel(BaseModel):
    """Persistent gateway config. Field defaults are the single source of truth."""

    # --- Camera / AI (giống gateway gốc) ---
    camera_snapshot_url: str = "http://127.0.0.1:8001/snapshot"
    camera_snapshot_timeout_ms: int = 3000
    ai_api_url: str = "http://192.168.0.3:8082/api/ai-detection"

    # --- PLC motion feedback (giống gateway gốc) ---
    plc_axis_speed_mm_per_s: float = 60.0
    use_plc_position_feedback: bool = False
    plc_done_mem_area: str = "D"
    plc_done_addr: Optional[int] = None
    plc_done_value: int = 1
    plc_busy_mem_area: str = "D"
    plc_busy_addr: Optional[int] = None
    plc_busy_idle_value: int = 0
    plc_poll_interval_ms: int = 50
    use_plc_motion_status: bool = True
    plc_motion_status_mem_area: str = "D"
    plc_motion_status_addr: int = 466
    plc_motion_status_count: int = 4
    plc_motion_start_timeout_ms: int = 1500
    plc_motion_idle_confirm_count: int = 10
    plc_motion_settle_ms: int = 500
    plc_motion_hard_timeout_ms: int = 20000

    # --- MỚI: Fiducial Detector Service (YOLO riêng) ---
    fiducial_api_url: str = "http://127.0.0.1:8193/api/detect-marker"
    fiducial_confidence_threshold: float = 0.5

    # --- MỚI: FOV / camera pixel geometry (theo mức zoom quang học đang dùng) ---
    camera_fov_width_mm: float = 10.872
    camera_fov_height_mm: float = 5.884
    camera_image_width_px: int = 1920
    camera_image_height_px: int = 1080

    # --- MỚI: Ma trận trục camera<->máy T (pixel_shift = T . plc_mm_shift) ---
    # Giá trị khởi tạo = ước lượng thô từ FOV (giả định trục thẳng hàng, không xoay/lật).
    # PHẢI hiệu chỉnh lại bằng /api/calib/camera-axis trước khi tin dùng cho production.
    camera_axis_matrix: List[List[float]] = Field(
        default_factory=lambda: [
            [1920.0 / 10.872, 0.0],
            [0.0, 1080.0 / 5.884],
        ]
    )
    camera_axis_calibrated: bool = False  # False = vẫn đang dùng giá trị ước lượng từ FOV

    # --- MỚI: đảo dấu trục dễ dàng (khi camera lắp lật trục so với PLC) ---
    # Áp SAU khi quy đổi pixel->mm: nếu true thì đảo dấu độ lệch mm của trục đó.
    #   - Dùng khi bạn KHÔNG hiệu chỉnh T đầy đủ (đang dùng ma trận mặc định đường chéo dương
    #     từ FOV) nhưng camera lắp lật trục -> chỉ cần bật cờ thay vì phải sửa tay ma trận.
    #   - Nếu ĐÃ hiệu chỉnh T bằng /api/calib/camera-axis thì T đã chứa sẵn dấu đúng ->
    #     ĐỂ CẢ HAI = false (bật thêm sẽ bị đảo dấu 2 lần -> sai hướng).
    camera_axis_invert_x: bool = False
    camera_axis_invert_y: bool = False

    # --- MỚI: an toàn / ngưỡng cảnh báo ---
    max_allowed_pixel_offset_px: float = 150.0
    max_allowed_rms_error_mm: float = 1.0

    # Đường dẫn file calib/offset tĩnh theo mặt board (A/B) KHÔNG còn khai báo ở đây nữa -
    # xem gateway/products_registry.yaml (calib_dir theo mã hàng đang active). Đã xoá field
    # vrs_calib_json_path/offset_runtime_json_path/calib_paths/offset_paths (PA2 cũ).


def _model_dump(model: BaseModel) -> Dict[str, Any]:
    """Support both pydantic v1 and v2."""
    return model.model_dump() if hasattr(model, "model_dump") else model.dict()


DEFAULT_GATEWAY_CONFIG: Dict[str, Any] = _model_dump(GatewayConfigModel())


def save_gateway_config(config: Dict[str, Any]) -> None:
    """Persist gateway config to JSON file."""
    CONFIG_FILE.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_gateway_config() -> Dict[str, Any]:
    """Load gateway config, creating the JSON file if it does not exist."""
    if not CONFIG_FILE.exists():
        save_gateway_config(DEFAULT_GATEWAY_CONFIG)
        return DEFAULT_GATEWAY_CONFIG.copy()

    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Config root must be a JSON object")

        merged = DEFAULT_GATEWAY_CONFIG.copy()
        merged.update({k: v for k, v in loaded.items() if k in merged})

        if merged != loaded:
            save_gateway_config(merged)
        return merged
    except Exception as e:
        logger.warning(f"⚠️  Failed to load config JSON, using defaults: {e}")
        save_gateway_config(DEFAULT_GATEWAY_CONFIG)
        return DEFAULT_GATEWAY_CONFIG.copy()


GATEWAY_CONFIG: Dict[str, Any] = load_gateway_config()


# ===============================
# MỚI: Danh mục mã hàng (products_registry.yaml) - hỗ trợ NHIỀU MÃ HÀNG, chọn tự động
# ===============================
# Mỗi mã hàng board (vd "23691025-250616-0004-nvq-aoi") cần 1 bộ weights YOLO riêng (marker
# khác nhau) + 1 bộ file mapping toạ độ riêng (anchor points/offset, CÙNG cấu trúc calib
# tĩnh/offset runtime đã có, chỉ khác chỗ lưu). Kỹ thuật viên tự train weights + tự chạy
# hiệu chỉnh (calib) cho từng mã hàng, chỉ cần thêm 1 mục vào file này (calib_dir trỏ tới
# thư mục riêng của mã hàng đó) - KHÔNG cần sửa code.
PRODUCTS_REGISTRY_FILE = RUNTIME_DIR / "products_registry.yaml"
ACTIVE_PRODUCT_STATE_FILE = RUNTIME_DIR / "active_product_state.json"

# Mã hàng mặc định = mã đang chạy production hiện tại. calib_dir = chính thư mục gateway/
# (RUNTIME_DIR) để KHÔNG phải di chuyển vrs_calib_side_a/b.json + offset_runtime_side_a/b.json
# đang dùng thật - tương thích ngược 100%, không cần migrate gì khi mới bật tính năng này.
DEFAULT_PRODUCTS_REGISTRY: Dict[str, Any] = {
    "default_product": "23691025-250616-0004-nvq-aoi",
    "products": {
        "23691025-250616-0004-nvq-aoi": {
            "weights_path": r"D:\Camera\Weight\VRS_yolo11n_640x640\weights\best.pt",
            "class_name_filter": 0,
            "imgsz": 640,
            "calib_dir": str(RUNTIME_DIR),
        },
    },
}


def save_products_registry(registry: Dict[str, Any]) -> None:
    PRODUCTS_REGISTRY_FILE.write_text(
        yaml.safe_dump(registry, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_products_registry() -> Dict[str, Any]:
    """Đọc danh mục mã hàng, tự tạo file với mã hàng production hiện tại nếu chưa có."""
    if not PRODUCTS_REGISTRY_FILE.exists():
        save_products_registry(DEFAULT_PRODUCTS_REGISTRY)
        return DEFAULT_PRODUCTS_REGISTRY.copy()
    try:
        loaded = yaml.safe_load(PRODUCTS_REGISTRY_FILE.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict) or "products" not in loaded:
            raise ValueError("products_registry.yaml phải có key 'products'")
        return loaded
    except Exception as e:
        logger.warning(f"⚠️  Không đọc được {PRODUCTS_REGISTRY_FILE}, dùng mặc định: {e}")
        save_products_registry(DEFAULT_PRODUCTS_REGISTRY)
        return DEFAULT_PRODUCTS_REGISTRY.copy()


PRODUCTS_REGISTRY: Dict[str, Any] = load_products_registry()
ACTIVE_PRODUCT_CODE: Optional[str] = None


def get_active_product() -> Optional[Dict[str, Any]]:
    """Config (dict) của mã hàng đang active, hoặc None nếu products_registry.yaml rỗng/lỗi
    (chưa từng khởi tạo được mã hàng nào - resolve_calib_path/resolve_offset_path sẽ raise)."""
    if not ACTIVE_PRODUCT_CODE:
        return None
    return PRODUCTS_REGISTRY.get("products", {}).get(ACTIVE_PRODUCT_CODE)


def resolve_fiducial_service_base_url() -> str:
    """Suy ra base URL của Fiducial Detector Service từ fiducial_api_url đã cấu hình (vd
    http://127.0.0.1:8191/api/detect-marker -> http://127.0.0.1:8191) thay vì thêm 1 field
    config mới dễ bị lệch giá trị với fiducial_api_url."""
    url = GATEWAY_CONFIG["fiducial_api_url"]
    suffix = "/api/detect-marker"
    if url.endswith(suffix):
        return url[: -len(suffix)]
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _save_active_product_state() -> None:
    ACTIVE_PRODUCT_STATE_FILE.write_text(
        json.dumps({"product_code": ACTIVE_PRODUCT_CODE}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_startup_active_product() -> None:
    """Lúc khởi động: khôi phục mã hàng đã chọn gần nhất (qua lần restart gateway), nếu
    chưa từng chọn thì dùng default_product trong registry."""
    global ACTIVE_PRODUCT_CODE
    code = None
    if ACTIVE_PRODUCT_STATE_FILE.exists():
        try:
            code = json.loads(ACTIVE_PRODUCT_STATE_FILE.read_text(encoding="utf-8")).get("product_code")
        except Exception as e:
            logger.warning(f"⚠️  Không đọc được {ACTIVE_PRODUCT_STATE_FILE}: {e}")
    if not code or code not in PRODUCTS_REGISTRY.get("products", {}):
        code = PRODUCTS_REGISTRY.get("default_product")
    if code and code in PRODUCTS_REGISTRY.get("products", {}):
        ACTIVE_PRODUCT_CODE = code


_load_startup_active_product()


# ===============================
# PLC DLL loading (pythonnet) - ClassLibrary.dll đã copy vào cùng thư mục
# ===============================
PLC_AVAILABLE = False
OmronConnection = None

try:
    import clr

    DLL_PATH = os.path.join(THIS_DIR, "ClassLibrary.dll")

    if not os.path.isfile(DLL_PATH):
        logger.warning(f"⚠️  ClassLibrary.dll not found at: {DLL_PATH}")
        logger.warning("   Chạy mock mode (không có PLC thật) cho tới khi có DLL.")
    else:
        clr.AddReference(DLL_PATH)
        from ClassLibrary.PLC.Omron import OmronConnection
        PLC_AVAILABLE = True
        logger.info(f"✅ PLC DLL loaded successfully from: {DLL_PATH}")
except ImportError:
    logger.warning("⚠️  pythonnet not installed. Install with: pip install pythonnet")
except Exception as e:
    logger.warning(f"⚠️  PLC DLL not available: {e}")


# ===============================
# Request/Response models (giống gateway gốc)
# ===============================
class PLCFeedbackOverrides(BaseModel):
    """Optional per-request overrides for PLC feedback / motion-status config."""
    use_plc_position_feedback: Optional[bool] = None
    plc_done_mem_area: Optional[str] = None
    plc_done_addr: Optional[int] = None
    plc_done_value: Optional[int] = None
    plc_busy_mem_area: Optional[str] = None
    plc_busy_addr: Optional[int] = None
    plc_busy_idle_value: Optional[int] = None
    plc_poll_interval_ms: Optional[int] = None
    use_plc_motion_status: Optional[bool] = None
    plc_motion_status_mem_area: Optional[str] = None
    plc_motion_status_addr: Optional[int] = None
    plc_motion_status_count: Optional[int] = None
    plc_motion_start_timeout_ms: Optional[int] = None
    plc_motion_idle_confirm_count: Optional[int] = None
    plc_motion_settle_ms: Optional[int] = None
    plc_motion_hard_timeout_ms: Optional[int] = None
    plc_axis_speed_mm_per_s: Optional[float] = None


class MoveRequest(PLCFeedbackOverrides):
    """Simple request to move camera to coordinates."""
    x: float
    y: float
    board_id: Optional[int] = None
    defect_id: Optional[int] = None


class MoveResponse(BaseModel):
    success: bool
    message: str
    plc_x: Optional[float] = None
    plc_y: Optional[float] = None
    elapsed_seconds: Optional[float] = None


class MoveBuLechRequest(PLCFeedbackOverrides):
    """Di chuyển camera tới toạ độ Board (Gerber/design) - tự động quy đổi sang
    PLC qua ma trận calib tĩnh (board_to_plc) + bù lệch board (rigid offset),
    giống hệt bước đầu của /api/inspect-defect. Dùng cho VRS thủ công để camera
    không còn di chuyển theo toạ độ thô chưa mapping/bù lệch như /api/plc/move."""
    board_x: float
    board_y: float
    board_id: Optional[str] = None
    defect_id: Optional[int] = None
    board_side: str = "A"  # "A" hoặc "B" - chọn calib tĩnh + offset runtime theo mặt
    apply_board_offset: bool = True


class MoveBuLechResponse(BaseModel):
    success: bool
    message: str
    board_coords: Optional[Dict[str, float]] = None    # toạ độ Board đầu vào
    nominal_coords: Optional[Dict[str, float]] = None  # PLC nominal (đã board_to_plc, CHƯA bù lệch)
    plc_x: Optional[float] = None                       # PLC compensated đã gửi thực tế
    plc_y: Optional[float] = None
    board_side: Optional[str] = None
    offset_applied: bool = False                        # đã bù lệch chưa
    offset_info: Optional[Dict[str, Any]] = None
    elapsed_seconds: Optional[float] = None


class InspectDefectRequest(PLCFeedbackOverrides):
    """Request để soi lỗi: gửi PLC + chụp ảnh + gọi AI Detection (giống hệt gateway gốc)."""
    defect_x: float
    defect_y: float
    board_id: Optional[str] = None
    defect_id: Optional[int] = None
    board_side: str = "A"  # "A" (mặc định) hoặc "B" — chọn calib tĩnh + offset runtime theo mặt

    plc_pc_ip: str = "192.168.3.101"
    plc_ip: str = "192.168.3.1"
    plc_port: int = 9600
    plc_mem_area: str = "D"
    plc_x_addr: int = 2810
    plc_y_addr: int = 2910
    plc_trigger_addr: int = 3000
    plc_move_timeout_ms: int = 2000

    ai_confidence_threshold: float = 0.25

    camera_snapshot_url: Optional[str] = None
    camera_snapshot_timeout_ms: Optional[int] = None

    # Bù lệch board tự động (mặc định True → luôn apply nếu có offset file cho board_side này)
    apply_board_offset: bool = True


class InspectDefectResponse(BaseModel):
    success: bool
    message: str
    step: str  # "plc_sent", "camera_captured", "ai_detected", "completed", "error"
    plc_coords: Optional[Dict[str, float]] = None
    board_coords: Optional[Dict[str, float]] = None       # tọa độ Board gốc (defect_x/defect_y đầu vào)
    nominal_coords: Optional[Dict[str, float]] = None   # PLC nominal (đã board_to_plc, CHƯA bù lệch board)
    board_side: Optional[str] = None
    offset_applied: bool = False                         # đã apply offset?
    offset_info: Optional[Dict[str, Any]] = None         # theta/tx/ty/source/board_id/board_side
    image_captured: bool = False
    image_base64: Optional[str] = None
    ai_detections: Optional[list] = None
    ai_verdict: Optional[str] = None
    ai_statistics: Optional[Dict[str, Any]] = None
    timing: Optional[Dict[str, float]] = None
    error_details: Optional[str] = None


# --- MỚI: models cho tự động bù lệch board ---
class AnchorPointResult(BaseModel):
    name: str
    board_xy: List[float]
    expected_plc_xy: List[float]
    detected_pixel_xy: Optional[List[float]] = None
    detection_confidence: Optional[float] = None
    detection_refined: Optional[bool] = None
    measured_plc_xy: Optional[List[float]] = None
    status: str  # "ok" | "not_found" | "outlier_pixel_offset" | "capture_failed" | "fiducial_service_error"
    detail: Optional[str] = None


class AutoBoardOffsetRequest(PLCFeedbackOverrides):
    """Tự động đo bù lệch board: PLC move -> chụp -> YOLO -> Kabsch -> lưu offset_runtime.json."""
    anchor_mode: int = 2  # 2 (A,C - mặc định) hoặc 3 (A,C,D)
    board_id: Optional[str] = None
    board_side: str = "A"  # "A" (mặc định) hoặc "B" — PA2: dùng calib tĩnh riêng cho từng mặt

    plc_pc_ip: str = "192.168.3.101"
    plc_ip: str = "192.168.3.1"
    plc_port: int = 9600
    plc_mem_area: str = "D"
    plc_x_addr: int = 2810
    plc_y_addr: int = 2910
    plc_trigger_addr: int = 3000
    plc_move_timeout_ms: int = 2000

    fiducial_api_url: Optional[str] = None
    fiducial_confidence_threshold: Optional[float] = None

    camera_snapshot_url: Optional[str] = None
    camera_snapshot_timeout_ms: Optional[int] = None

    max_allowed_pixel_offset_px: Optional[float] = None
    max_allowed_rms_error_mm: Optional[float] = None


class AutoBoardOffsetResponse(BaseModel):
    success: bool
    message: str
    anchor_mode: int
    anchor_results: List[AnchorPointResult] = []
    theta_deg: Optional[float] = None
    tx: Optional[float] = None
    ty: Optional[float] = None
    rms_error_mm: Optional[float] = None
    max_error_mm: Optional[float] = None
    offset_saved_path: Optional[str] = None
    warning: Optional[str] = None
    timing: Optional[Dict[str, float]] = None


class CameraAxisCalibRequest(PLCFeedbackOverrides):
    """Hiệu chỉnh ma trận trục camera<->máy T. Chạy 1 LẦN lúc setup, không phải mỗi board."""
    anchor_name: str = "A"
    delta_mm: float = 5.0
    board_side: str = "A"  # mặt board đang gá khi hiệu chỉnh (thường "A"); PA2: chọn calib tĩnh theo mặt

    plc_pc_ip: str = "192.168.3.101"
    plc_ip: str = "192.168.3.1"
    plc_port: int = 9600
    plc_mem_area: str = "D"
    plc_x_addr: int = 2810
    plc_y_addr: int = 2910
    plc_trigger_addr: int = 3000
    plc_move_timeout_ms: int = 2000

    fiducial_api_url: Optional[str] = None
    fiducial_confidence_threshold: Optional[float] = None
    camera_snapshot_url: Optional[str] = None
    camera_snapshot_timeout_ms: Optional[int] = None


class CameraAxisCalibResponse(BaseModel):
    success: bool
    message: str
    anchor_board_xy: Optional[List[float]] = None
    anchor_plc_expected_xy: Optional[List[float]] = None
    camera_axis_matrix: Optional[List[List[float]]] = None
    samples_pixel_xy: Optional[Dict[str, List[float]]] = None
    sanity_check: Optional[str] = None


class BoardToPlcPreviewRequest(BaseModel):
    board_x: float
    board_y: float
    board_side: str = "A"


class BoardToPlcPreviewResponse(BaseModel):
    board_xy: List[float]
    board_side: str = "A"
    plc_nominal_xy: List[float]
    plc_compensated_xy: List[float]
    offset_loaded: bool
    offset_source: Optional[str] = None


# --- MỚI: models cho chọn mã hàng (nhiều model YOLO / nhiều bộ calib) ---
class ProductSelectRequest(BaseModel):
    product_code: str


class ProductSelectResponse(BaseModel):
    success: bool
    product_code: str
    message: str
    weights_path: Optional[str] = None
    calib_dir: Optional[str] = None
    fiducial_class_names: Optional[Dict[str, Any]] = None


# ===============================
# PLC Service (giống hệt gateway gốc)
# ===============================
class PLCService:
    """Service for PLC communication (Omron via ClassLibrary.dll)."""

    def __init__(self):
        self.connection = None

    def connect(self, pc_ip: str, plc_ip: str, port: int) -> bool:
        if not PLC_AVAILABLE:
            logger.warning("⚠️  PLC DLL not available, using mock mode")
            return True

        try:
            self.connection = OmronConnection(pc_ip, plc_ip, port)
            logger.info(f"✅ Connected to PLC: {plc_ip}:{port}")
            return True
        except Exception as e:
            logger.error(f"❌ PLC connection failed: {e}")
            return False

    def send_coordinates(
        self,
        mem_area: str,
        x_addr: int,
        y_addr: int,
        trigger_addr: int,
        x_val: float,
        y_val: float,
    ) -> bool:
        if not PLC_AVAILABLE or self.connection is None:
            logger.info(f"🔧 [MOCK] Would send: X={x_val}, Y={y_val}")
            return True

        try:
            ok_x = self.connection.WriteFloat(mem_area, x_addr, str(x_val))
            ok_y = self.connection.WriteFloat(mem_area, y_addr, str(y_val))
            ok_trigger = self.connection.WriteInt(mem_area, trigger_addr, "1")
            time.sleep(0.05)
            self.connection.WriteInt(mem_area, trigger_addr, "0")

            logger.info(f"✅ PLC write OK: X={ok_x}, Y={ok_y}, Trigger={ok_trigger}")
            return ok_x and ok_y and ok_trigger
        except Exception as e:
            logger.error(f"❌ PLC write failed: {e}")
            return False

    def read_int(self, mem_area: str, addr: int, qnt: int = 1) -> Optional[List[int]]:
        if not PLC_AVAILABLE or self.connection is None:
            logger.info(f"🔧 [MOCK] Would read: {mem_area}{addr} x {qnt}")
            return [0] * qnt

        try:
            raw_value = self.connection.ReadInt(mem_area, addr, qnt)
            values = [int(part) for part in str(raw_value).split() if str(part).strip()]
            if len(values) != qnt:
                raise ValueError(f"Expected {qnt} values, got '{raw_value}'")
            # logger.info(f"📥 PLC read OK: {mem_area}{addr} -> {values}")
            return values
        except Exception as e:
            logger.error(f"❌ PLC read failed at {mem_area}{addr}: {e}")
            return None

    def read_float_words(self, mem_area: str, addr: int) -> Optional[float]:
        words = self.read_int(mem_area, addr, 2)
        if words is None or len(words) != 2:
            return None

        try:
            packed = struct.pack("<HH", words[0] & 0xFFFF, words[1] & 0xFFFF)
            value = struct.unpack("<f", packed)[0]
            logger.info(f"PLC float read OK: {mem_area}{addr}-{addr + 1} -> {value}")
            return value
        except Exception as e:
            logger.error(f"PLC float read failed at {mem_area}{addr}: {e}")
            return None

    def close(self) -> None:
        if self.connection:
            try:
                self.connection.Close()
                logger.info("🔌 PLC connection closed")
            except Exception:
                pass


# ===============================
# Camera Service (giống hệt gateway gốc)
# ===============================
class CameraService:
    """Service for camera snapshot capture from Sony streamer."""

    def __init__(self, snapshot_url: str):
        self.snapshot_url = snapshot_url
        self.last_error: Optional[str] = None
        self.last_snapshot_image_bytes: Optional[bytes] = None
        self.last_snapshot_image_md5: Optional[str] = None
        self.last_snapshot_content_type: Optional[str] = None

    def capture_frame(self, timeout_ms: int = 3000, snapshot_url: Optional[str] = None) -> Optional[np.ndarray]:
        url = snapshot_url or self.snapshot_url
        self.last_error = None
        self.last_snapshot_image_bytes = None
        self.last_snapshot_image_md5 = None
        self.last_snapshot_content_type = None

        try:
            frame = None
            image_bytes_raw = None
            content_type = None
            fresh_fetches = max(CAMERA_SNAPSHOT_FRESH_FETCHES, 1)

            for fetch_index in range(fresh_fetches):
                logger.info(f"📸 Fetching snapshot {fetch_index + 1}/{fresh_fetches}: {url}")
                response = requests.get(
                    url,
                    timeout=timeout_ms / 1000.0,
                    headers={
                        "Cache-Control": "no-cache, no-store, must-revalidate",
                        "Pragma": "no-cache",
                        "Expires": "0",
                    },
                )

                if response.status_code != 200:
                    body_preview = response.text[:300].replace("\n", " ")
                    raise RuntimeError(f"Snapshot endpoint returned {response.status_code}: {body_preview}")

                content_type = response.headers.get("Content-Type", "")
                if not any(t in content_type for t in ALLOWED_SNAPSHOT_CONTENT_TYPES):
                    raise RuntimeError(f"Unexpected content type: {content_type}")

                image_bytes_raw = response.content
                frame = cv2.imdecode(np.frombuffer(image_bytes_raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise RuntimeError("Failed to decode snapshot image")

                if fetch_index < fresh_fetches - 1:
                    time.sleep(CAMERA_SNAPSHOT_FRESH_DELAY_MS / 1000.0)

            self.last_snapshot_image_bytes = image_bytes_raw
            self.last_snapshot_image_md5 = hashlib.md5(image_bytes_raw).hexdigest() if image_bytes_raw else None
            self.last_snapshot_content_type = content_type
            logger.info(
                f"Snapshot image captured: {len(image_bytes_raw)} bytes, "
                f"type={content_type}, md5={self.last_snapshot_image_md5}, "
                f"shape={frame.shape[1]}x{frame.shape[0]}"
            )
            return frame

        except Exception as e:
            self.last_error = f"{url} -> {e}"
            logger.error(f"❌ Snapshot capture failed: {self.last_error}")
            return None


def encode_image_base64(frame: Optional[np.ndarray], raw_bytes: Optional[bytes]) -> Optional[str]:
    if raw_bytes:
        return base64.b64encode(raw_bytes).decode("utf-8")
    if frame is None:
        return None
    success, jpeg = cv2.imencode(".jpg", frame)
    return base64.b64encode(jpeg.tobytes()).decode("utf-8") if success else None


def resolve_camera_snapshot_url(request_url: Optional[str] = None) -> str:
    return request_url or GATEWAY_CONFIG["camera_snapshot_url"]


def resolve_camera_snapshot_timeout_ms(request_timeout_ms: Optional[int] = None) -> int:
    return int(request_timeout_ms or GATEWAY_CONFIG["camera_snapshot_timeout_ms"])


def resolve_fiducial_api_url(request_url: Optional[str] = None) -> str:
    return request_url or GATEWAY_CONFIG["fiducial_api_url"]


def resolve_fiducial_confidence_threshold(request_threshold: Optional[float] = None) -> float:
    return float(request_threshold if request_threshold is not None else GATEWAY_CONFIG["fiducial_confidence_threshold"])


def resolve_ai_api_url() -> str:
    return GATEWAY_CONFIG["ai_api_url"]


# ===============================
# PLC feedback / motion-wait config (giống hệt gateway gốc)
# ===============================
@dataclass
class FeedbackConfig:
    axis_speed_mm_per_s: float
    use_feedback: bool
    done_mem_area: str
    done_addr: Optional[int]
    done_value: int
    busy_mem_area: str
    busy_addr: Optional[int]
    busy_idle_value: int
    poll_interval_ms: int
    use_motion_status: bool
    motion_status_mem_area: str
    motion_status_addr: int
    motion_status_count: int
    motion_start_timeout_ms: int
    motion_idle_confirm_count: int
    motion_settle_ms: int
    motion_hard_timeout_ms: int

    estimated_distance_mm: Optional[float] = None
    expected_travel_ms: int = 0
    min_completion_wait_ms: int = 0

    _FIELD_MAP = {
        "axis_speed_mm_per_s": ("plc_axis_speed_mm_per_s", float),
        "use_feedback": ("use_plc_position_feedback", bool),
        "done_mem_area": ("plc_done_mem_area", None),
        "done_addr": ("plc_done_addr", None),
        "done_value": ("plc_done_value", int),
        "busy_mem_area": ("plc_busy_mem_area", None),
        "busy_addr": ("plc_busy_addr", None),
        "busy_idle_value": ("plc_busy_idle_value", int),
        "poll_interval_ms": ("plc_poll_interval_ms", int),
        "use_motion_status": ("use_plc_motion_status", bool),
        "motion_status_mem_area": ("plc_motion_status_mem_area", None),
        "motion_status_addr": ("plc_motion_status_addr", int),
        "motion_status_count": ("plc_motion_status_count", int),
        "motion_start_timeout_ms": ("plc_motion_start_timeout_ms", int),
        "motion_idle_confirm_count": ("plc_motion_idle_confirm_count", int),
        "motion_settle_ms": ("plc_motion_settle_ms", int),
        "motion_hard_timeout_ms": ("plc_motion_hard_timeout_ms", int),
    }

    @classmethod
    def from_request(cls, request: Any) -> "FeedbackConfig":
        kwargs: Dict[str, Any] = {}
        for field_name, (key, cast) in cls._FIELD_MAP.items():
            value = getattr(request, key, None)
            if value is None:
                value = GATEWAY_CONFIG.get(key, DEFAULT_GATEWAY_CONFIG.get(key))
            if cast is not None:
                if value is None:
                    value = DEFAULT_GATEWAY_CONFIG[key]
                value = cast(value)
            kwargs[field_name] = value
        return cls(**kwargs)

    def apply_dynamic_motion_timeout(
        self,
        current_x: Optional[float],
        current_y: Optional[float],
        target_x: float,
        target_y: float,
    ) -> Optional[int]:
        speed = max(self.axis_speed_mm_per_s, 0.0)
        if current_x is None or current_y is None or speed <= 0:
            return None

        distance_mm = hypot(target_x - current_x, target_y - current_y)
        travel_ms = max(int((distance_mm / speed) * 1000.0), 0)
        estimated_hard_ms = max(
            int(travel_ms * PLC_MOTION_TIMEOUT_SAFETY_FACTOR + PLC_MOTION_TIMEOUT_OVERHEAD_MS),
            PLC_MOTION_TIMEOUT_OVERHEAD_MS,
        )

        self.estimated_distance_mm = distance_mm
        self.expected_travel_ms = travel_ms
        self.min_completion_wait_ms = max(
            int(travel_ms * PLC_MOTION_MIN_COMPLETION_RATIO),
            PLC_MOTION_MIN_COMPLETION_FLOOR_MS,
        )
        self.motion_hard_timeout_ms = max(self.motion_hard_timeout_ms, estimated_hard_ms)
        return estimated_hard_ms


def is_motion_status_active(status_values: List[int]) -> bool:
    if len(status_values) < 4:
        return False
    d466, d467, d468, d469 = status_values[:4]
    return (
        d467 == 65535
        or d468 == 65535
        or d466 in MOTION_ACTIVE_D466
        or d469 in MOTION_ACTIVE_D469
    )


def is_motion_status_idle(status_values: List[int]) -> bool:
    return len(status_values) >= 4 and all(v == 0 for v in status_values[:4])


def _elapsed_ms(start_time: float) -> float:
    return (time.time() - start_time) * 1000.0


class _WarnOnce:
    def __init__(self):
        self._seen: set = set()

    def __call__(self, key: str, message: str) -> None:
        if key not in self._seen:
            self._seen.add(key)
            logger.warning(message)


async def wait_for_plc_position(plc: PLCService, timeout_ms: int, fb: FeedbackConfig) -> float:
    start_time = time.time()

    if not fb.use_feedback and not fb.use_motion_status:
        logger.info(f"⏲️ PLC feedback disabled, waiting fixed timeout {timeout_ms}ms")
        await asyncio.sleep(timeout_ms / 1000.0)
        return time.time() - start_time

    poll_interval = max(fb.poll_interval_ms, 10) / 1000.0
    soft_deadline = start_time + (timeout_ms / 1000.0)

    if fb.use_motion_status:
        return await _wait_motion_status(plc, timeout_ms, fb, start_time, poll_interval, soft_deadline)
    return await _wait_done_busy(plc, timeout_ms, fb, start_time, poll_interval, soft_deadline)


async def _wait_motion_status(
    plc: PLCService,
    timeout_ms: int,
    fb: FeedbackConfig,
    start_time: float,
    poll_interval: float,
    soft_deadline: float,
) -> float:
    motion_addr = fb.motion_status_addr
    motion_count = max(fb.motion_status_count, 4)
    motion_start_deadline = start_time + (fb.motion_start_timeout_ms / 1000.0)
    hard_deadline = start_time + (max(fb.motion_hard_timeout_ms, timeout_ms) / 1000.0)
    idle_confirm_count = max(fb.motion_idle_confirm_count, 1)
    settle_seconds = max(fb.motion_settle_ms, 0) / 1000.0
    min_completion_wait_ms = max(fb.min_completion_wait_ms, 0)
    expected_travel_ms = max(fb.expected_travel_ms, 0)
    silent_fallback_wait_ms = max(
        int(expected_travel_ms * PLC_MOTION_TIMEOUT_SAFETY_FACTOR),
        timeout_ms,
        min_completion_wait_ms,
        PLC_MOTION_MIN_COMPLETION_FLOOR_MS,
    )

    warn_once = _WarnOnce()
    saw_motion = False
    idle_hits = 0

    async def finish(reason: str, status_values: List[int]) -> float:
        logger.info(f"✅ {reason}; idle status {status_values[:4]} confirmed for {idle_hits} polls")
        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        return time.time() - start_time

    logger.info(
        f"⏳ Waiting PLC motion status: {fb.motion_status_mem_area}{motion_addr}-"
        f"{motion_addr + motion_count - 1}, poll={fb.poll_interval_ms}ms"
    )

    while time.time() < hard_deadline:
        status_values = await asyncio.to_thread(
            plc.read_int, fb.motion_status_mem_area, motion_addr, motion_count
        )
        if not status_values or len(status_values) < 4:
            await asyncio.sleep(poll_interval)
            continue

        now = time.time()
        idle = is_motion_status_idle(status_values)

        if not saw_motion:
            if is_motion_status_active(status_values):
                saw_motion = True
                idle_hits = 0
                logger.info(f"🚀 PLC motion started: {status_values[:4]}")
            else:
                if fb.estimated_distance_mm is not None and idle:
                    if fb.estimated_distance_mm <= PLC_ALREADY_IN_POSITION_TOLERANCE_MM:
                        idle_hits += 1
                        if idle_hits >= idle_confirm_count:
                            return await finish("PLC already at requested position", status_values)
                    elif _elapsed_ms(start_time) >= silent_fallback_wait_ms:
                        idle_hits += 1
                        warn_once(
                            "silent_fallback",
                            f"PLC motion status stayed idle for a moving command; "
                            f"falling back to travel-time estimate after {silent_fallback_wait_ms}ms",
                        )
                        if idle_hits >= idle_confirm_count:
                            return await finish(
                                "PLC position wait completed using silent-motion fallback", status_values
                            )
                if now >= motion_start_deadline:
                    warn_once(
                        "motion_not_started",
                        f"⚠️ PLC has not reported motion yet after {fb.motion_start_timeout_ms}ms; "
                        f"continuing to wait",
                    )
                if now >= soft_deadline:
                    warn_once(
                        "soft_timeout",
                        f"⚠️ PLC still has not reported motion by expected timeout {timeout_ms}ms; "
                        f"continuing to wait until hard timeout {fb.motion_hard_timeout_ms}ms",
                    )
        else:
            if now >= soft_deadline:
                warn_once(
                    "soft_timeout",
                    f"⚠️ PLC motion exceeded expected timeout {timeout_ms}ms; "
                    f"continuing to wait until idle or hard timeout {fb.motion_hard_timeout_ms}ms",
                )
            if idle:
                elapsed_ms = _elapsed_ms(start_time)
                if elapsed_ms < min_completion_wait_ms:
                    warn_once(
                        "early_idle",
                        f"⚠️ PLC returned idle too early after {elapsed_ms:.0f}ms; "
                        f"waiting until at least {min_completion_wait_ms}ms before capture",
                    )
                    idle_hits = 0
                else:
                    idle_hits += 1
                    if idle_hits >= idle_confirm_count:
                        return await finish("PLC motion completed with stable idle status", status_values)
            else:
                idle_hits = 0

        await asyncio.sleep(poll_interval)

    if not saw_motion:
        raise TimeoutError("PLC did not report motion before hard timeout")
    raise TimeoutError("PLC did not complete motion before hard timeout")


async def _wait_done_busy(
    plc: PLCService,
    timeout_ms: int,
    fb: FeedbackConfig,
    start_time: float,
    poll_interval: float,
    deadline: float,
) -> float:
    if fb.done_addr is None and fb.busy_addr is None:
        logger.warning("⚠️  PLC feedback enabled but no done/busy address configured, falling back to timeout")
        await asyncio.sleep(timeout_ms / 1000.0)
        return time.time() - start_time

    logger.info(
        f"⏳ Waiting PLC feedback: done={fb.done_mem_area}{fb.done_addr}, "
        f"busy={fb.busy_mem_area}{fb.busy_addr}, poll={fb.poll_interval_ms}ms"
    )

    while time.time() < deadline:
        if fb.done_addr is not None:
            values = await asyncio.to_thread(plc.read_int, fb.done_mem_area, fb.done_addr, 1)
            if values and values[0] == fb.done_value:
                logger.info(f"✅ PLC done feedback reached value {values[0]}")
                return time.time() - start_time

        if fb.busy_addr is not None:
            values = await asyncio.to_thread(plc.read_int, fb.busy_mem_area, fb.busy_addr, 1)
            if values and values[0] == fb.busy_idle_value:
                logger.info(f"✅ PLC busy feedback reached idle value {values[0]}")
                return time.time() - start_time

        await asyncio.sleep(poll_interval)

    raise TimeoutError("PLC did not report in-position/done before timeout")


# ===============================
# AI Detection Client (giống hệt gateway gốc - dùng cho /api/inspect-defect)
# ===============================
class AIServiceClient:
    """Client for AI Detection API."""

    @staticmethod
    def detect(
        image_bgr: np.ndarray,
        confidence_threshold: float,
        api_url: str,
        original_image_bytes: Optional[bytes] = None,
    ) -> Optional[Dict[str, Any]]:
        """Send image to AI API and return detections."""
        try:
            if original_image_bytes:
                image_bytes = original_image_bytes
            else:
                success, jpeg = cv2.imencode(".jpg", image_bgr)
                if not success:
                    raise RuntimeError("Failed to encode image")
                image_bytes = jpeg.tobytes()

            logger.info(f"🤖 Sending to AI API: {api_url}")
            response = requests.post(
                api_url,
                json={
                    "image_base64": base64.b64encode(image_bytes).decode("utf-8"),
                    "confidence_threshold": confidence_threshold,
                },
                timeout=30,
            )
            if response.status_code != 200:
                raise RuntimeError(f"AI API error: {response.status_code}")

            result = response.json()
            logger.info(f"✅ AI detected {len(result.get('detections', []))} defects")
            return result
        except Exception as e:
            logger.error(f"❌ AI detection failed: {e}")
            return None


# ===============================
# MỚI: Fiducial Detector Client (mirror AIServiceClient của gateway gốc)
# ===============================
class FiducialClient:
    """Client gọi sang Fiducial Detector Service (YOLO riêng, xem fiducial_detector/)."""

    @staticmethod
    def detect(
        image_bgr: np.ndarray,
        api_url: str,
        confidence_threshold: float,
        original_image_bytes: Optional[bytes] = None,
        timeout_s: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        """Trả về dict {"found", "cx", "cy", "confidence", "bbox", "refined", ...} hoặc None nếu lỗi mạng/service."""
        try:
            if original_image_bytes:
                image_bytes = original_image_bytes
            else:
                success, jpeg = cv2.imencode(".jpg", image_bgr)
                if not success:
                    raise RuntimeError("Failed to encode image")
                image_bytes = jpeg.tobytes()

            logger.info(f"🎯 Gửi ảnh sang Fiducial Detector: {api_url}")
            response = requests.post(
                api_url,
                json={
                    "image_base64": base64.b64encode(image_bytes).decode("utf-8"),
                    "confidence_threshold": confidence_threshold,
                },
                timeout=timeout_s,
            )
            if response.status_code != 200:
                raise RuntimeError(f"Fiducial Detector API error: {response.status_code} - {response.text[:300]}")

            result = response.json()
            logger.info(
                f"🎯 Fiducial Detector kết quả: found={result.get('found')}, "
                f"cx={result.get('cx')}, cy={result.get('cy')}, conf={result.get('confidence')}"
            )
            return result
        except Exception as e:
            logger.error(f"❌ Fiducial Detector call failed: {e}")
            return None

    @staticmethod
    def select_model(
        base_url: str,
        weights_path: str,
        class_name_filter: Optional[Any],
        imgsz: int,
        timeout_s: float = 30.0,
    ) -> Optional[Dict[str, Any]]:
        """Báo Fiducial Detector Service đổi sang dùng weights của mã hàng vừa chọn
        (POST {base_url}/api/select-model). Trả None nếu lỗi mạng/service không phản hồi."""
        try:
            response = requests.post(
                f"{base_url}/api/select-model",
                json={"weights_path": weights_path, "class_name_filter": class_name_filter, "imgsz": imgsz},
                timeout=timeout_s,
            )
            result = response.json()
            if response.status_code != 200:
                logger.error(f"❌ Fiducial select-model trả lỗi HTTP {response.status_code}: {result}")
            return result
        except Exception as e:
            logger.error(f"❌ Gọi Fiducial select-model thất bại: {e}")
            return None


# ===============================
# MỚI: quy đổi lệch pixel -> lệch mm THEO TRỤC PLC
# ===============================
def pixel_offset_to_plc_mm(dx_px: float, dy_px: float, axis_matrix: List[List[float]]) -> Tuple[float, float]:
    """
    axis_matrix = T sao cho: [dpx, dpy]^T = T . [dPLCx, dPLCy]^T
    (T được hiệu chỉnh 1 lần bằng /api/calib/camera-axis, xem docs/Ke_hoach...).
    Hàm này đảo ngược T để suy ra độ lệch PLC (mm) từ độ lệch pixel đo được.
    """
    T = np.array(axis_matrix, dtype=float)
    if abs(np.linalg.det(T)) < 1e-9:
        raise ValueError("camera_axis_matrix suy biến (det ~ 0), cần hiệu chỉnh lại bằng /api/calib/camera-axis")
    T_inv = np.linalg.inv(T)
    plc_shift = T_inv @ np.array([dx_px, dy_px], dtype=float)
    return float(plc_shift[0]), float(plc_shift[1])


def measured_plc_from_marker(
    expected_x: float,
    expected_y: float,
    marker_px: float,
    marker_py: float,
    image_w: int,
    image_h: int,
    axis_matrix: List[List[float]],
    invert_x: bool = False,
    invert_y: bool = False,
) -> Tuple[float, float]:
    """Suy ra toạ độ PLC THỰC TẾ của marker khi camera đang ở vị trí `expected`.

    Định nghĩa T (từ /api/calib/camera-axis): pixel_shift_cua_marker = T · plc_shift_cua_camera
    (khi CAMERA/PLC dịch chuyển Δ thì pixel của 1 marker CỐ ĐỊNH dịch T·Δ).

    Marker đang lệch khỏi tâm ảnh 1 lượng off = (marker_px - w/2, marker_py - h/2). Muốn đưa
    marker về tâm (tức camera chỉ đúng vào marker) thì phải dịch PLC một lượng Δ sao cho
    off + T·Δ = 0  =>  Δ = -T⁻¹·off. Do đó toạ độ PLC nơi marker thực sự nằm là:
        measured = expected + Δ = expected - T⁻¹·off        (DẤU TRỪ - đây là chỗ bug cũ)

    Bản cũ cộng (+T⁻¹·off) nên toạ độ đo bị lệch ngược dấu, kéo theo t của Kabsch ngược dấu
    và bù lệch đi sai hướng.

    invert_x/invert_y: đảo dấu độ lệch mm của trục tương ứng (khi camera lắp lật trục mà
    dùng ma trận mặc định đường chéo dương). Nếu T đã hiệu chỉnh đầy đủ thì để cả hai = False.
    """
    dx_px = marker_px - image_w / 2.0
    dy_px = marker_py - image_h / 2.0
    dx_mm, dy_mm = pixel_offset_to_plc_mm(dx_px, dy_px, axis_matrix)
    if invert_x:
        dx_mm = -dx_mm
    if invert_y:
        dy_mm = -dy_mm
    return expected_x - dx_mm, expected_y - dy_mm


def rotation_matrix(theta_rad: float) -> np.ndarray:
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    return np.array([[c, -s], [s, c]], dtype=float)


def load_saved_rigid_offset(offset_path: str) -> Optional[Tuple[np.ndarray, np.ndarray, Dict[str, Any]]]:
    """Đọc offset_runtime.json đã lưu (từ auto-board-offset hoặc bu_lech_board.py cũ), trả (R, t, raw_data)."""
    if not os.path.exists(offset_path):
        return None
    with open(offset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    theta = math.radians(data["theta_deg"])
    R = rotation_matrix(theta)
    t = np.array([data["tx"], data["ty"]], dtype=float)
    return R, t, data


# ===============================
# FastAPI app
# ===============================
camera_service = CameraService(GATEWAY_CONFIG["camera_snapshot_url"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("=" * 60)
    logger.info("🚀 PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat) Starting...")
    logger.info("=" * 60)
    logger.info(f"💾 Config file: {CONFIG_FILE}")
    logger.info(f"📷 Snapshot URL default: {GATEWAY_CONFIG['camera_snapshot_url']}")
    logger.info(f"🎯 Fiducial Detector URL: {GATEWAY_CONFIG['fiducial_api_url']}")
    logger.info(f"📦 Mã hàng đang active: {ACTIVE_PRODUCT_CODE} (xem {PRODUCTS_REGISTRY_FILE})")
    logger.info(
        f"📐 camera_axis_matrix calibrated: {GATEWAY_CONFIG['camera_axis_calibrated']} "
        f"(False = đang dùng ước lượng thô từ FOV, hãy chạy /api/calib/camera-axis)"
    )
    logger.info("✅ Services ready")
    yield
    logger.info("🛑 Shutting down...")


app = FastAPI(title="PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat)", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===============================
# Helpers dùng chung cho các endpoint /api/calib/*
# ===============================
def _get_anchor_names(anchor_mode: int) -> List[str]:
    if anchor_mode not in ANCHOR_SETS:
        raise HTTPException(status_code=400, detail=f"anchor_mode phải là 2 hoặc 3, nhận được {anchor_mode}")
    return ANCHOR_SETS[anchor_mode]


def _require_active_product() -> Dict[str, Any]:
    """Trả config (dict) của mã hàng đang active, hoặc raise HTTPException 400 rõ ràng nếu
    chưa có mã hàng nào được chọn (products_registry.yaml rỗng hoặc chưa khởi tạo)."""
    product = get_active_product()
    if product is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Chưa có mã hàng nào đang active. Gọi POST /api/products/select "
                f"{{'product_code': '<mã hàng>'}} trước (xem {PRODUCTS_REGISTRY_FILE})."
            ),
        )
    return product


def resolve_calib_path(board_side: str) -> str:
    """Trả đường dẫn file calib tĩnh cho mặt board đã cho, theo calib_dir của mã hàng
    đang active (products_registry.yaml) - mỗi mã hàng 1 bộ file riêng."""
    side = (board_side or "A").upper()
    product = _require_active_product()
    return str(Path(product["calib_dir"]) / f"vrs_calib_side_{side.lower()}.json")


def resolve_offset_path(board_side: str) -> str:
    """Trả đường dẫn file offset runtime cho mặt board đã cho, theo calib_dir của mã hàng
    đang active (xem resolve_calib_path)."""
    side = (board_side or "A").upper()
    product = _require_active_product()
    return str(Path(product["calib_dir"]) / f"offset_runtime_side_{side.lower()}.json")


def validate_board_side(board_side: str) -> None:
    """Kiểm tra board_side hợp lệ + tồn tại file calib cho side đó. Raise HTTPException nếu sai."""
    side = (board_side or "A").upper()
    if side not in ("A", "B"):
        raise HTTPException(status_code=400, detail=f"board_side phải là 'A' hoặc 'B', nhận được '{board_side}'")
    # Kiểm tra calib path tồn tại (resolve_calib_path sẽ raise nếu thiếu mặt B)
    calib_path = resolve_calib_path(side)
    if not os.path.exists(calib_path):
        raise HTTPException(
            status_code=400,
            detail=f"File calib cho mặt {side} không tồn tại: {calib_path}",
        )


async def _move_and_wait(
    plc: PLCService,
    request: Any,
    current_x: Optional[float],
    current_y: Optional[float],
    target_x: float,
    target_y: float,
) -> float:
    """Gửi toạ độ + chờ hoàn tất, dùng chung logic FeedbackConfig/wait_for_plc_position."""
    if not await asyncio.to_thread(
        plc.send_coordinates,
        request.plc_mem_area,
        request.plc_x_addr,
        request.plc_y_addr,
        request.plc_trigger_addr,
        target_x,
        target_y,
    ):
        raise RuntimeError("Failed to send coordinates to PLC")

    fb = FeedbackConfig.from_request(request)
    fb.apply_dynamic_motion_timeout(current_x, current_y, target_x, target_y)
    return await wait_for_plc_position(plc, request.plc_move_timeout_ms, fb)


async def _capture_and_detect_marker(
    request: Any,
) -> Tuple[Optional[np.ndarray], Optional[Dict[str, Any]], Optional[str]]:
    """Chụp ảnh + gọi Fiducial Detector. Trả (frame, detection_dict_or_None, error_message_or_None)."""
    snapshot_url = resolve_camera_snapshot_url(getattr(request, "camera_snapshot_url", None))
    snapshot_timeout_ms = resolve_camera_snapshot_timeout_ms(getattr(request, "camera_snapshot_timeout_ms", None))

    frame = await asyncio.to_thread(camera_service.capture_frame, snapshot_timeout_ms, snapshot_url)
    if frame is None:
        return None, None, f"Không chụp được ảnh từ {snapshot_url}: {camera_service.last_error}"

    fiducial_url = resolve_fiducial_api_url(getattr(request, "fiducial_api_url", None))
    fiducial_threshold = resolve_fiducial_confidence_threshold(getattr(request, "fiducial_confidence_threshold", None))

    det = await asyncio.to_thread(
        FiducialClient.detect,
        frame,
        fiducial_url,
        fiducial_threshold,
        camera_service.last_snapshot_image_bytes,
    )
    if det is None:
        return frame, None, f"Không gọi được Fiducial Detector Service tại {fiducial_url}"
    if not det.get("found"):
        return frame, det, det.get("message", "Không phát hiện được điểm mốc trong ảnh")

    return frame, det, None


# ===============================
# Khoá độc quyền PLC
# ===============================
# Mỗi endpoint tạo `PLCService()` riêng, nên KHÔNG có xung đột state Python -
# nhưng tất cả đều ghi vào CÙNG các thanh ghi vật lý (D2810/D2910 + trigger
# D3000) của MỘT con PLC. Nếu 2 request chạy song song (ví dụ Auto VRS đang
# /api/inspect-defect trong khi VRS thủ công bấm "Di chuyển Camera", hoặc 1
# chu kỳ calib 90s đang di chuyển qua các điểm mốc) thì chúng ghi toạ độ đan
# xen nhau rồi mỗi bên lại `wait_for_plc_position` trên vị trí do bên kia vừa
# đặt -> máy đi sai vị trí, ảnh chụp sai chỗ.
#
# Chốt ở tầng gateway (không phải chỉ ở app Flutter) vì gateway còn có client
# khác: các script/GUI trong Auto_calib gọi trực tiếp cùng cổng.
#
# Cố tình TRẢ 409 thay vì xếp hàng: một lệnh calib 90s âm thầm chờ sau lệnh
# khác rồi mới chạy sẽ làm operator bất ngờ hơn là báo "đang bận" ngay.
_plc_lock = asyncio.Lock()
_plc_lock_holder: Optional[str] = None


def plc_exclusive(operation: str):
    """Chỉ cho phép 1 lệnh điều khiển PLC chạy tại một thời điểm.

    Đặt DƯỚI decorator `@app.post/get(...)` để FastAPI vẫn đọc được signature
    gốc (functools.wraps giữ `__wrapped__` nên inspect.signature xuyên qua được).
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            global _plc_lock_holder
            # asyncio đơn luồng: giữa lúc kiểm tra locked() và lúc acquire()
            # thành công trên khoá đang rảnh không có điểm await nào, nên
            # không có kẽ hở cho request khác chen vào.
            if _plc_lock.locked():
                busy_with = _plc_lock_holder or "lệnh khác"
                logger.warning(
                    f"⛔ Từ chối '{operation}': PLC đang bận với '{busy_with}'"
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"PLC đang bận ({busy_with}). Đợi lệnh hiện tại xong "
                        f"rồi thử lại."
                    ),
                )
            async with _plc_lock:
                _plc_lock_holder = operation
                try:
                    return await func(*args, **kwargs)
                finally:
                    _plc_lock_holder = None

        return wrapper

    return decorator


# ===============================
# Endpoints (giữ nguyên endpoint cơ bản từ gateway gốc)
# ===============================
@app.get("/")
async def root():
    """Health check."""
    return {
        "service": "PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat)",
        "status": "running",
        "plc_available": PLC_AVAILABLE,
        "camera_snapshot_url": GATEWAY_CONFIG["camera_snapshot_url"],
        "fiducial_api_url": GATEWAY_CONFIG["fiducial_api_url"],
        "camera_axis_calibrated": GATEWAY_CONFIG["camera_axis_calibrated"],
        "config_file": str(CONFIG_FILE),
        "active_product": ACTIVE_PRODUCT_CODE,
        "products_registry_file": str(PRODUCTS_REGISTRY_FILE),
    }


@app.get("/api/products")
async def list_products():
    """Danh sách mã hàng có trong products_registry.yaml - app Flutter dùng để hiện dropdown."""
    products = PRODUCTS_REGISTRY.get("products", {})
    return {
        "products": list(products.keys()),
        "default_product": PRODUCTS_REGISTRY.get("default_product"),
        "active_product": ACTIVE_PRODUCT_CODE,
        "registry_file": str(PRODUCTS_REGISTRY_FILE),
    }


@app.get("/api/products/active")
async def get_active_product_info():
    """Mã hàng đang active hiện tại + đường dẫn calib đã resolve - dùng để app Flutter xác
    nhận lại sau khi chọn, hoặc hiện thị lúc khởi động app."""
    product = get_active_product()
    return {
        "active_product": ACTIVE_PRODUCT_CODE,
        "config": product,
        "calib_path_side_a": resolve_calib_path("A") if product else None,
        "calib_path_side_b": resolve_calib_path("B") if product else None,
    }


@app.post("/api/products/select", response_model=ProductSelectResponse)
async def select_product(request: ProductSelectRequest):
    """Đổi mã hàng đang active - gọi từ app Flutter khi operator chọn mã hàng.

    Sẽ: (1) tra cứu weights/calib_dir của mã hàng trong products_registry.yaml, (2) báo
    Fiducial Detector Service đổi sang weights của mã hàng đó, (3) từ đó resolve_calib_path/
    resolve_offset_path tự động trả về đúng file mapping toạ độ của mã hàng này cho các
    endpoint bù lệch (/api/calib/auto-board-offset, /api/plc/move_bulech, ...).

    AN TOÀN: nếu Fiducial Detector từ chối (thiếu file weights, service down...), mã hàng
    đang active HIỆN TẠI vẫn giữ nguyên - không đổi dây chuyền đang chạy tốt sang trạng thái
    lỗi chỉ vì chọn nhầm 1 mã hàng có cấu hình sai.
    """
    global ACTIVE_PRODUCT_CODE

    previous_product_code = ACTIVE_PRODUCT_CODE

    product = PRODUCTS_REGISTRY.get("products", {}).get(request.product_code)
    if product is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Mã hàng '{request.product_code}' chưa có trong {PRODUCTS_REGISTRY_FILE}. "
                f"Các mã hàng hiện có: {list(PRODUCTS_REGISTRY.get('products', {}).keys())}"
            ),
        )

    if request.product_code == previous_product_code:
        logger.info(f"ℹ️ Mã hàng '{request.product_code}' đã đang active sẵn - không cần đổi.")

    fiducial_base = resolve_fiducial_service_base_url()
    fiducial_result = await asyncio.to_thread(
        FiducialClient.select_model,
        fiducial_base,
        product["weights_path"],
        product.get("class_name_filter"),
        product.get("imgsz", 640),
    )
    if fiducial_result is None or not fiducial_result.get("success"):
        message = (fiducial_result or {}).get("message") or f"Không gọi được Fiducial Detector Service tại {fiducial_base}"
        logger.error(
            f"❌ Đổi mã hàng '{previous_product_code}' → '{request.product_code}' thất bại "
            f"(giữ nguyên mã hàng cũ '{previous_product_code}'): {message}"
        )
        return ProductSelectResponse(success=False, product_code=request.product_code, message=message)

    ACTIVE_PRODUCT_CODE = request.product_code
    _save_active_product_state()
    calib_dir = Path(product["calib_dir"])
    calib_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"🔄 Đã đổi mã hàng: '{previous_product_code}' → '{request.product_code}' "
        f"(weights={product['weights_path']}, calib_dir={calib_dir})"
    )
    return ProductSelectResponse(
        success=True, product_code=request.product_code, message="OK",
        weights_path=product["weights_path"], calib_dir=str(calib_dir),
        fiducial_class_names=fiducial_result.get("class_names"),
    )


@app.post("/api/plc/move", response_model=MoveResponse)
@plc_exclusive("di chuyển PLC (/api/plc/move)")
async def move_camera_simple(request: MoveRequest):
    """Di chuyển PLC tới toạ độ X,Y (đơn vị mm, đã tính sẵn) - dùng để test tay hoặc tích hợp ngoài."""
    plc_config = {**DEFAULT_PLC_CONN, "mem_area": "D", "x_addr": 2810, "y_addr": 2910, "trigger_addr": 3000}
    plc = PLCService()
    try:
        if not await asyncio.to_thread(plc.connect, plc_config["pc_ip"], plc_config["plc_ip"], plc_config["port"]):
            raise HTTPException(status_code=500, detail="Failed to connect to PLC")

        current_x = await asyncio.to_thread(plc.read_float_words, plc_config["mem_area"], plc_config["x_addr"])
        current_y = await asyncio.to_thread(plc.read_float_words, plc_config["mem_area"], plc_config["y_addr"])

        if not await asyncio.to_thread(
            plc.send_coordinates, plc_config["mem_area"], plc_config["x_addr"], plc_config["y_addr"],
            plc_config["trigger_addr"], request.x, request.y,
        ):
            raise HTTPException(status_code=500, detail="Failed to write coordinates to PLC")

        fb = FeedbackConfig.from_request(request)
        fb.apply_dynamic_motion_timeout(current_x, current_y, request.x, request.y)
        elapsed = await wait_for_plc_position(plc, 10000, fb)

        return MoveResponse(success=True, message=f"Moved to ({request.x:.3f}, {request.y:.3f})",
                             plc_x=request.x, plc_y=request.y, elapsed_seconds=elapsed)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Move camera failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        plc.close()


@app.post("/api/plc/move_bulech", response_model=MoveBuLechResponse)
@plc_exclusive("di chuyển PLC có bù lệch (/api/plc/move_bulech)")
async def move_camera_bulech(request: MoveBuLechRequest):
    """Di chuyển camera tới toạ độ Board sau khi quy đổi sang PLC (board_to_plc)
    và bù lệch board (rigid offset) - giống hệt bước đầu của /api/inspect-defect
    nhưng KHÔNG chụp ảnh/AI. Dùng cho nút "Di chuyển Camera" ở VRS thủ công thay
    cho /api/plc/move (vốn gửi x/y thô, không mapping/không bù lệch)."""
    board_side = (request.board_side or "A").upper()
    validate_board_side(board_side)

    coeffs = load_calibration_matrix(resolve_calib_path(board_side))
    nominal_x, nominal_y = board_to_plc(request.board_x, request.board_y, coeffs)
    logger.info(
        f"📍 [move_bulech] Board→PLC (mặt {board_side}): Board=({request.board_x:.3f},{request.board_y:.3f})"
        f" → PLC nominal=({nominal_x:.3f},{nominal_y:.3f})"
    )

    final_x, final_y = nominal_x, nominal_y
    offset_applied = False
    offset_info = None

    if request.apply_board_offset:
        offset_path = resolve_offset_path(board_side)
        loaded = load_saved_rigid_offset(offset_path)
        if loaded is not None:
            R, t, raw = loaded
            final_x, final_y = apply_rigid_offset(nominal_x, nominal_y, R, t)
            offset_applied = True
            offset_info = {
                "theta_deg": raw.get("theta_deg"),
                "tx": raw.get("tx"), "ty": raw.get("ty"),
                "source": raw.get("source"), "board_id": raw.get("board_id"),
                "board_side": raw.get("board_side", board_side),
            }
            logger.info(
                f"📐 [move_bulech] Offset applied: Nominal=({nominal_x:.3f},{nominal_y:.3f})"
                f" → Compensated=({final_x:.3f},{final_y:.3f})"
                f" [θ={raw['theta_deg']:+.4f}° tx={raw['tx']:+.4f} ty={raw['ty']:+.4f}]"
            )
        else:
            logger.warning(
                f"⚠️ [move_bulech] apply_board_offset=True nhưng CHƯA có offset runtime"
                f" cho mặt {board_side} ({offset_path}) → dùng tọa độ nominal (CHƯA bù lệch)"
            )

    plc_config = {**DEFAULT_PLC_CONN, "mem_area": "D", "x_addr": 2810, "y_addr": 2910, "trigger_addr": 3000}
    plc = PLCService()
    try:
        if not await asyncio.to_thread(plc.connect, plc_config["pc_ip"], plc_config["plc_ip"], plc_config["port"]):
            raise HTTPException(status_code=500, detail="Failed to connect to PLC")

        current_x = await asyncio.to_thread(plc.read_float_words, plc_config["mem_area"], plc_config["x_addr"])
        current_y = await asyncio.to_thread(plc.read_float_words, plc_config["mem_area"], plc_config["y_addr"])

        if not await asyncio.to_thread(
            plc.send_coordinates, plc_config["mem_area"], plc_config["x_addr"], plc_config["y_addr"],
            plc_config["trigger_addr"], final_x, final_y,
        ):
            raise HTTPException(status_code=500, detail="Failed to write coordinates to PLC")

        fb = FeedbackConfig.from_request(request)
        fb.apply_dynamic_motion_timeout(current_x, current_y, final_x, final_y)
        elapsed = await wait_for_plc_position(plc, 10000, fb)

        message = f"Moved to Board=({request.board_x:.3f},{request.board_y:.3f}) -> PLC=({final_x:.3f},{final_y:.3f})"
        if not offset_applied:
            message += " [CẢNH BÁO: chưa bù lệch board cho mặt " + board_side + "]"

        return MoveBuLechResponse(
            success=True,
            message=message,
            board_coords={"x": request.board_x, "y": request.board_y},
            nominal_coords={"x": nominal_x, "y": nominal_y},
            plc_x=final_x,
            plc_y=final_y,
            board_side=board_side,
            offset_applied=offset_applied,
            offset_info=offset_info,
            elapsed_seconds=elapsed,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ [move_bulech] Move camera failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        plc.close()


@app.post("/api/inspect-defect", response_model=InspectDefectResponse)
@plc_exclusive("soi lỗi (/api/inspect-defect)")
async def inspect_defect(request: InspectDefectRequest):
    """Soi lỗi: gửi toạ độ tới PLC, chờ di chuyển xong, chụp ảnh, gọi AI Detection (giống hệt gateway gốc)."""
    timing: Dict[str, float] = {}
    start_time = time.time()
    plc = PLCService()  # instance riêng mỗi request -> an toàn khi có request đồng thời

    try:
        logger.info("=" * 60)

        # ---- Board -> PLC: quy đổi tọa độ Board (defect_x, defect_y) sang PLC nominal
        # bằng ma trận bilinear tĩnh của đúng mặt board (board_side), giống hệt
        # /api/calib/board-to-plc ----
        board_x, board_y = request.defect_x, request.defect_y
        board_side = (request.board_side or "A").upper()
        validate_board_side(board_side)

        coeffs = load_calibration_matrix(resolve_calib_path(board_side))
        nominal_x, nominal_y = board_to_plc(board_x, board_y, coeffs)
        logger.info(
            f"📍 Board→PLC (mặt {board_side}): Board=({board_x:.3f},{board_y:.3f})"
            f" → PLC nominal=({nominal_x:.3f},{nominal_y:.3f})"
        )

        # ---- Bù lệch board: load offset runtime của đúng mặt + apply_rigid_offset ----
        final_x, final_y = nominal_x, nominal_y
        offset_applied = False
        offset_info = None

        if request.apply_board_offset:
            offset_path = resolve_offset_path(board_side)
            loaded = load_saved_rigid_offset(offset_path)
            if loaded is not None:
                R, t, raw = loaded
                final_x, final_y = apply_rigid_offset(nominal_x, nominal_y, R, t)
                offset_applied = True
                offset_info = {
                    "theta_deg": raw.get("theta_deg"),
                    "tx": raw.get("tx"), "ty": raw.get("ty"),
                    "source": raw.get("source"), "board_id": raw.get("board_id"),
                    "board_side": raw.get("board_side", board_side),
                }
                logger.info(
                    f"📐 Offset applied: Nominal=({nominal_x:.3f},{nominal_y:.3f})"
                    f" → Compensated=({final_x:.3f},{final_y:.3f})"
                    f" [θ={raw['theta_deg']:+.4f}° tx={raw['tx']:+.4f} ty={raw['ty']:+.4f}]"
                )
            else:
                logger.warning(
                    f"⚠️ apply_board_offset=True nhưng không có offset runtime cho mặt {board_side}"
                    f" ({offset_path}) → dùng tọa độ nominal"
                )

        logger.info(f"🔍 Inspecting defect: PLC target=({final_x:.3f}, {final_y:.3f})")

        step_start = time.time()
        if not await asyncio.to_thread(plc.connect, request.plc_pc_ip, request.plc_ip, request.plc_port):
            raise RuntimeError("Failed to connect to PLC")
        timing["plc_connect"] = time.time() - step_start

        current_x = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_x_addr)
        current_y = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_y_addr)

        step_start = time.time()
        if not await asyncio.to_thread(
            plc.send_coordinates,
            request.plc_mem_area,
            request.plc_x_addr,
            request.plc_y_addr,
            request.plc_trigger_addr,
            final_x,
            final_y,
        ):
            raise RuntimeError("Failed to send coordinates to PLC")
        timing["plc_send"] = time.time() - step_start

        fb = FeedbackConfig.from_request(request)
        fb.apply_dynamic_motion_timeout(current_x, current_y, final_x, final_y)
        timing["plc_wait"] = await wait_for_plc_position(plc, request.plc_move_timeout_ms, fb)

        step_start = time.time()
        snapshot_url = resolve_camera_snapshot_url(request.camera_snapshot_url)
        snapshot_timeout_ms = resolve_camera_snapshot_timeout_ms(request.camera_snapshot_timeout_ms)
        frame = await asyncio.to_thread(camera_service.capture_frame, snapshot_timeout_ms, snapshot_url)
        if frame is None:
            raise RuntimeError(
                f"Failed to capture image from {snapshot_url}. Last error: {camera_service.last_error}"
            )
        timing["camera_capture"] = time.time() - step_start

        step_start = time.time()
        ai_api_url = resolve_ai_api_url()
        ai_result = await asyncio.to_thread(
            AIServiceClient.detect,
            frame,
            request.ai_confidence_threshold,
            ai_api_url,
            camera_service.last_snapshot_image_bytes,
        )
        if ai_result is None:
            raise RuntimeError("AI detection failed")
        timing["ai_detection"] = time.time() - step_start
        timing["total"] = time.time() - start_time

        logger.info(f"✅ Inspection completed in {timing['total']:.2f}s")

        return InspectDefectResponse(
            success=True,
            message="Inspection completed successfully",
            step="completed",
            plc_coords={"x": final_x, "y": final_y},
            board_coords={"x": board_x, "y": board_y},
            nominal_coords={"x": nominal_x, "y": nominal_y},
            board_side=board_side,
            offset_applied=offset_applied,
            offset_info=offset_info,
            image_captured=True,
            image_base64=ai_result.get("processed_image_base64")
            or encode_image_base64(frame, camera_service.last_snapshot_image_bytes),
            ai_detections=ai_result.get("detections", []),
            ai_verdict=ai_result.get("statistics", {}).get("system_verdict", "UNKNOWN"),
            ai_statistics=ai_result.get("statistics", {}),
            timing=timing,
        )

    except Exception as e:
        logger.error(f"❌ Inspection failed: {e}\n{traceback.format_exc()}")
        return InspectDefectResponse(
            success=False,
            message=f"Inspection failed: {e}",
            step="error",
            error_details=traceback.format_exc(),
            timing=timing,
        )
    finally:
        plc.close()


@app.get("/api/test-plc")
@plc_exclusive("test kết nối PLC (/api/test-plc)")
async def test_plc():
    plc = PLCService()
    try:
        if await asyncio.to_thread(plc.connect, **DEFAULT_PLC_CONN):
            return {"success": True, "message": "PLC connection OK", "plc_available": PLC_AVAILABLE}
        return {"success": False, "message": "PLC connection failed"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        plc.close()


@app.get("/api/test-plc-feedback")
@plc_exclusive("đọc thanh ghi feedback PLC (/api/test-plc-feedback)")
async def test_plc_feedback():
    """Đọc thử các thanh ghi PLC feedback (done/busy) đang cấu hình - dùng để debug."""
    plc = PLCService()
    try:
        if not await asyncio.to_thread(plc.connect, **DEFAULT_PLC_CONN):
            return {"success": False, "message": "Failed to connect to PLC"}

        done_value = busy_value = None
        if GATEWAY_CONFIG["plc_done_addr"] is not None:
            values = await asyncio.to_thread(
                plc.read_int, GATEWAY_CONFIG["plc_done_mem_area"], GATEWAY_CONFIG["plc_done_addr"], 1
            )
            done_value = values[0] if values else None
        if GATEWAY_CONFIG["plc_busy_addr"] is not None:
            values = await asyncio.to_thread(
                plc.read_int, GATEWAY_CONFIG["plc_busy_mem_area"], GATEWAY_CONFIG["plc_busy_addr"], 1
            )
            busy_value = values[0] if values else None

        return {
            "success": True,
            "use_plc_position_feedback": GATEWAY_CONFIG["use_plc_position_feedback"],
            "plc_done_mem_area": GATEWAY_CONFIG["plc_done_mem_area"],
            "plc_done_addr": GATEWAY_CONFIG["plc_done_addr"],
            "plc_done_value": done_value,
            "plc_busy_mem_area": GATEWAY_CONFIG["plc_busy_mem_area"],
            "plc_busy_addr": GATEWAY_CONFIG["plc_busy_addr"],
            "plc_busy_value": busy_value,
        }
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        plc.close()


@app.get("/api/test-camera")
async def test_camera(snapshot_url: Optional[str] = None, snapshot_timeout_ms: Optional[int] = None):
    resolved_url = resolve_camera_snapshot_url(snapshot_url)
    resolved_timeout_ms = resolve_camera_snapshot_timeout_ms(snapshot_timeout_ms)
    try:
        frame = await asyncio.to_thread(camera_service.capture_frame, resolved_timeout_ms, resolved_url)
        if frame is not None:
            return {
                "success": True, "message": "Snapshot capture OK", "snapshot_url": resolved_url,
                "shape": frame.shape, "image_base64": encode_image_base64(frame, camera_service.last_snapshot_image_bytes),
            }
        return {"success": False, "message": "Snapshot capture failed", "snapshot_url": resolved_url}
    except Exception as e:
        return {"success": False, "message": str(e), "snapshot_url": resolved_url}


@app.get("/api/test-fiducial")
async def test_fiducial(snapshot_url: Optional[str] = None):
    """Chụp 1 ảnh + gọi Fiducial Detector, KHÔNG di chuyển PLC - dùng để test riêng model YOLO."""
    resolved_url = resolve_camera_snapshot_url(snapshot_url)
    frame = await asyncio.to_thread(camera_service.capture_frame, resolve_camera_snapshot_timeout_ms(None), resolved_url)
    if frame is None:
        return {"success": False, "message": f"Không chụp được ảnh: {camera_service.last_error}"}
    det = await asyncio.to_thread(
        FiducialClient.detect, frame, GATEWAY_CONFIG["fiducial_api_url"],
        GATEWAY_CONFIG["fiducial_confidence_threshold"], camera_service.last_snapshot_image_bytes,
    )
    return {"success": det is not None, "detection": det, "image_shape": list(frame.shape)}


@app.get("/api/camera-config", response_model=GatewayConfigModel)
async def get_camera_config():
    return GatewayConfigModel(**GATEWAY_CONFIG)


@app.put("/api/camera-config", response_model=GatewayConfigModel)
async def update_camera_config(config: GatewayConfigModel):
    global GATEWAY_CONFIG
    GATEWAY_CONFIG = _model_dump(config)
    save_gateway_config(GATEWAY_CONFIG)
    camera_service.snapshot_url = GATEWAY_CONFIG["camera_snapshot_url"]
    logger.info(f"💾 Config saved to {CONFIG_FILE}")
    return GatewayConfigModel(**GATEWAY_CONFIG)


# ===============================
# MỚI: /api/calib/auto-board-offset
# ===============================
@app.post("/api/calib/auto-board-offset", response_model=AutoBoardOffsetResponse)
@plc_exclusive("calib bù lệch board (/api/calib/auto-board-offset)")
async def auto_board_offset(request: AutoBoardOffsetRequest):
    """
    Thay thế bu_lech_board.py THỦ CÔNG:
    Với mỗi điểm mốc (mặc định A, C):
      1. Tính PLC kỳ vọng = board_to_plc(board_xy, ma_trận_calib_gốc)
      2. Di chuyển PLC tới đó, chờ ổn định
      3. Chụp ảnh, gọi Fiducial Detector tìm tâm vòng tròn mốc (px, py)
      4. Lệch pixel so với tâm ảnh -> lệch mm theo trục PLC (qua camera_axis_matrix)
      5. PLC "đo được" = PLC kỳ vọng + lệch mm
    Sau khi có đủ >=2 cặp (kỳ vọng, đo được) -> kabsch_2d() -> lưu offset_runtime.json
    (schema giống hệt bu_lech_board.py, có thêm board_id + source="auto_yolo").
    """
    timing: Dict[str, float] = {}
    t_start = time.time()

    max_pixel_offset = request.max_allowed_pixel_offset_px or GATEWAY_CONFIG["max_allowed_pixel_offset_px"]
    max_rms = request.max_allowed_rms_error_mm or GATEWAY_CONFIG["max_allowed_rms_error_mm"]

    board_side = (request.board_side or "A").upper()
    validate_board_side(board_side)  # chặn sớm nếu thiếu calib cho mặt này

    calib_path = resolve_calib_path(board_side)
    try:
        coeffs = load_calibration_matrix(calib_path)
    except Exception as e:
        return AutoBoardOffsetResponse(success=False, message=f"Lỗi nạp ma trận calib mặt {board_side}: {e}",
                                        anchor_mode=request.anchor_mode)

    anchor_names = _get_anchor_names(request.anchor_mode)

    plc = PLCService()
    results: List[AnchorPointResult] = []
    expected_pts: List[Tuple[float, float]] = []
    measured_pts: List[Tuple[float, float]] = []

    try:
        step_start = time.time()
        if not await asyncio.to_thread(plc.connect, request.plc_pc_ip, request.plc_ip, request.plc_port):
            return AutoBoardOffsetResponse(success=False, message="Không kết nối được PLC",
                                            anchor_mode=request.anchor_mode)
        timing["plc_connect"] = time.time() - step_start

        for name in anchor_names:
            bx, by = ANCHOR_POINTS_POOL[name]
            ex, ey = board_to_plc(bx, by, coeffs)
            logger.info(
                f"🔎 Điểm mốc {name} (mặt {board_side}): "
                f"Board=({bx:.3f},{by:.3f}) -> PLC kỳ vọng=({ex:.3f},{ey:.3f})"
            )

            current_x = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_x_addr)
            current_y = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_y_addr)

            try:
                await _move_and_wait(plc, request, current_x, current_y, ex, ey)
            except Exception as e:
                results.append(AnchorPointResult(name=name, board_xy=[bx, by], expected_plc_xy=[ex, ey],
                                                  status="capture_failed", detail=f"Lỗi di chuyển PLC: {e}"))
                continue

            frame, det, err = await _capture_and_detect_marker(request)
            if err is not None or det is None or not det.get("found"):
                status = "fiducial_service_error" if det is None else "not_found"
                results.append(AnchorPointResult(name=name, board_xy=[bx, by], expected_plc_xy=[ex, ey],
                                                  status=status, detail=err))
                continue

            px, py = float(det["cx"]), float(det["cy"])
            dx_px = px - GATEWAY_CONFIG["camera_image_width_px"] / 2.0
            dy_px = py - GATEWAY_CONFIG["camera_image_height_px"] / 2.0

            if hypot(dx_px, dy_px) > max_pixel_offset:
                results.append(AnchorPointResult(
                    name=name, board_xy=[bx, by], expected_plc_xy=[ex, ey],
                    detected_pixel_xy=[px, py], detection_confidence=det.get("confidence"),
                    detection_refined=det.get("refined"), status="outlier_pixel_offset",
                    detail=f"Lệch pixel {hypot(dx_px, dy_px):.1f}px vượt ngưỡng {max_pixel_offset}px",
                ))
                continue

            # Toạ độ PLC thực tế của marker = expected - T⁻¹·offset (xem measured_plc_from_marker).
            # (Bug cũ: cộng offset -> đo ngược dấu -> t ngược dấu -> bù sai hướng.)
            mx, my = measured_plc_from_marker(
                ex, ey, px, py,
                GATEWAY_CONFIG["camera_image_width_px"],
                GATEWAY_CONFIG["camera_image_height_px"],
                GATEWAY_CONFIG["camera_axis_matrix"],
                invert_x=GATEWAY_CONFIG.get("camera_axis_invert_x", False),
                invert_y=GATEWAY_CONFIG.get("camera_axis_invert_y", False),
            )

            expected_pts.append((ex, ey))
            measured_pts.append((mx, my))
            results.append(AnchorPointResult(
                name=name, board_xy=[bx, by], expected_plc_xy=[ex, ey],
                detected_pixel_xy=[px, py], detection_confidence=det.get("confidence"),
                detection_refined=det.get("refined"), measured_plc_xy=[mx, my], status="ok",
            ))

        timing["measure_all_anchors"] = time.time() - t_start

        if len(expected_pts) < 2:
            return AutoBoardOffsetResponse(
                success=False,
                message=f"Chỉ đo được {len(expected_pts)}/{len(anchor_names)} điểm mốc hợp lệ (cần >= 2). "
                        "Kiểm tra ánh sáng/marker/Fiducial Detector Service.",
                anchor_mode=request.anchor_mode, anchor_results=results, timing=timing,
            )

        R, t, theta, rms_error, max_error, residuals = kabsch_2d(expected_pts, measured_pts)

        warning = None
        if rms_error > max_rms:
            warning = (
                f"⚠️ Sai số dư RMS ({rms_error:.4f}mm) VƯỢT ngưỡng an toàn ({max_rms}mm). "
                "Offset vẫn được lưu nhưng NÊN kiểm tra lại (đo lại, kiểm tra marker/ánh sáng, "
                "hoặc board bị méo) trước khi dùng cho sản xuất."
            )
            logger.warning(warning)

        offset_data = {
            "anchor_mode": len(expected_pts),
            "theta_deg": math.degrees(theta),
            "tx": float(t[0]),
            "ty": float(t[1]),
            "rms_error_mm": rms_error,
            "max_error_mm": max_error,
            "anchor_points_board": [
                (name, list(ANCHOR_POINTS_POOL[name]))
                for name in anchor_names
            ],
            "anchor_points_expected_plc": expected_pts,
            "anchor_points_measured_plc": measured_pts,
            "residuals_mm": residuals.tolist(),
            "board_id": request.board_id,
            "board_side": board_side,
            "source": "auto_yolo",
            "timestamp": time.time(),
        }
        offset_path = resolve_offset_path(board_side)
        with open(offset_path, "w", encoding="utf-8") as f:
            json.dump(offset_data, f, indent=4, ensure_ascii=False)

        timing["total"] = time.time() - t_start

        return AutoBoardOffsetResponse(
            success=True,
            message="Tính bù lệch board tự động THÀNH CÔNG" + (" (có cảnh báo)" if warning else ""),
            anchor_mode=len(expected_pts), anchor_results=results,
            theta_deg=math.degrees(theta), tx=float(t[0]), ty=float(t[1]),
            rms_error_mm=rms_error, max_error_mm=max_error,
            offset_saved_path=offset_path, warning=warning, timing=timing,
        )

    except Exception as e:
        logger.error(f"❌ auto_board_offset thất bại: {e}\n{traceback.format_exc()}")
        return AutoBoardOffsetResponse(success=False, message=f"Lỗi: {e}", anchor_mode=request.anchor_mode,
                                        anchor_results=results, timing=timing)
     
    finally:
        try:
            # quay lại vị trí gốc (0,0) để tiến hành bước tiếp theo quy trình sản xuất, tránh để PLC dừng lệch vị trí ban đầu
            current_x = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_x_addr)
            current_y = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_y_addr)
            logger.info(f"🔙 Quay lại vị trí gốc (0,0) từ ({current_x:.3f},{current_y:.3f})")
            await _move_and_wait(plc, request, current_x, current_y, 0.0, 0.0)
        except Exception:
            logger.warning("⚠️ Không thể quay lại vị trí gốc (0,0) sau khi đo xong, nhưng không chặn kết quả hiệu chỉnh")  
        plc.close()


# ===============================
# MỚI: /api/calib/anchor-info (tra toạ độ điểm mốc - KHÔNG di chuyển máy)
# ===============================
@app.get("/api/calib/anchor-info")
async def anchor_info(anchor_name: str = "A", board_side: str = "A"):
    """Trả toạ độ Board (mm) và toạ độ PLC kỳ vọng của 1 điểm mốc, dùng để xem trước
    trước khi chạy /api/calib/camera-axis (không di chuyển máy, không chụp ảnh)."""
    side = (board_side or "A").upper()
    if anchor_name not in ANCHOR_POINTS_POOL:
        raise HTTPException(status_code=400, detail=f"anchor_name '{anchor_name}' không có trong ANCHOR_POINTS_POOL")
    validate_board_side(side)

    bx, by = ANCHOR_POINTS_POOL[anchor_name]

    result = {
        "anchor_name": anchor_name,
        "board_side": side,
        "board_xy": [bx, by],
        "plc_expected_xy": None,
    }
    try:
        coeffs = load_calibration_matrix(resolve_calib_path(side))
        result["plc_expected_xy"] = list(board_to_plc(bx, by, coeffs))
    except Exception as e:
        result["plc_expected_xy_error"] = str(e)
    return result


# ===============================
# MỚI: /api/calib/camera-axis (hiệu chỉnh ma trận T - làm 1 lần lúc setup)
# ===============================
@app.post("/api/calib/camera-axis", response_model=CameraAxisCalibResponse)
@plc_exclusive("calib trục camera (/api/calib/camera-axis)")
async def calibrate_camera_axis(request: CameraAxisCalibRequest):
    """
    Hiệu chỉnh ma trận trục camera<->máy T:
      P0 = PLC kỳ vọng tại anchor_name (mặc định A)
      P1 = P0 + (delta_mm, 0)
      P2 = P0 + (0, delta_mm)
    Di chuyển PLC lần lượt tới P0, P1, P2; mỗi lần chụp ảnh + detect tâm marker (p0, p1, p2).
    T = [[(p1x-p0x)/delta, (p2x-p0x)/delta], [(p1y-p0y)/delta, (p2y-p0y)/delta]]
    Lưu vào config (camera_axis_matrix, camera_axis_calibrated=True). Cuối cùng trả PLC về P0.

    LƯU Ý: chỉ cần chạy 1 LẦN khi setup máy hoặc sau khi tháo/lắp lại camera - KHÔNG chạy
    lại mỗi khi đổi board (đó là việc của /api/calib/auto-board-offset).
    """
    global GATEWAY_CONFIG

    board_side = (request.board_side or "A").upper()
    if request.anchor_name not in ANCHOR_POINTS_POOL:
        raise HTTPException(status_code=400, detail=f"anchor_name '{request.anchor_name}' không có trong ANCHOR_POINTS_POOL")
    if request.delta_mm <= 0:
        raise HTTPException(status_code=400, detail="delta_mm phải > 0")
    validate_board_side(board_side)

    calib_path = resolve_calib_path(board_side)
    try:
        coeffs = load_calibration_matrix(calib_path)
    except Exception as e:
        return CameraAxisCalibResponse(success=False, message=f"Lỗi nạp ma trận calib mặt {board_side}: {e}")

    bx, by = ANCHOR_POINTS_POOL[request.anchor_name]
    p0_plc = board_to_plc(bx, by, coeffs)
    delta = request.delta_mm
    targets = {
        "P0": p0_plc,
        "P1_dx": (p0_plc[0] + delta, p0_plc[1]),
        "P2_dy": (p0_plc[0], p0_plc[1] + delta),
    }

    logger.info(
        f"🔎 Điểm mốc dùng để hiệu chỉnh: anchor='{request.anchor_name}' (mặt {board_side}) "
        f"Board=({bx:.3f},{by:.3f}) -> PLC kỳ vọng P0=({p0_plc[0]:.3f},{p0_plc[1]:.3f})"
    )

    plc = PLCService()
    pixel_samples: Dict[str, List[float]] = {}
    try:
        if not await asyncio.to_thread(plc.connect, request.plc_pc_ip, request.plc_ip, request.plc_port):
            return CameraAxisCalibResponse(
                success=False, message="Không kết nối được PLC",
                anchor_board_xy=[bx, by], anchor_plc_expected_xy=list(p0_plc),
            )

        for label, (tx_, ty_) in targets.items():
            logger.info(f"➡️  Di chuyển tới {label}: PLC target=({tx_:.3f},{ty_:.3f})")
            current_x = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_x_addr)
            current_y = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_y_addr)
            try:
                await _move_and_wait(plc, request, current_x, current_y, tx_, ty_)
            except Exception as e:
                return CameraAxisCalibResponse(
                    success=False, message=f"Lỗi di chuyển PLC tới {label}: {e}",
                    anchor_board_xy=[bx, by], anchor_plc_expected_xy=list(p0_plc),
                )

            frame, det, err = await _capture_and_detect_marker(request)
            if err is not None or det is None or not det.get("found"):
                return CameraAxisCalibResponse(
                    success=False,
                    message=f"Không phát hiện được marker tại {label}: {err or 'không rõ lý do'}",
                    anchor_board_xy=[bx, by], anchor_plc_expected_xy=list(p0_plc),
                )
            pixel_samples[label] = [float(det["cx"]), float(det["cy"])]

        # quay lại P0 cho an toàn (không để máy dừng lệch khỏi vị trí ban đầu)
        current_x = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_x_addr)
        current_y = await asyncio.to_thread(plc.read_float_words, request.plc_mem_area, request.plc_y_addr)
        try:
            await _move_and_wait(plc, request, current_x, current_y, *p0_plc)
        except Exception:
            pass  # không chặn kết quả hiệu chỉnh nếu chỉ bước dọn dẹp này lỗi

        p0 = np.array(pixel_samples["P0"])
        p1 = np.array(pixel_samples["P1_dx"])
        p2 = np.array(pixel_samples["P2_dy"])

        col1 = (p1 - p0) / delta   # pixel dịch chuyển / mm PLC theo trục X
        col2 = (p2 - p0) / delta   # pixel dịch chuyển / mm PLC theo trục Y
        T = [[float(col1[0]), float(col2[0])], [float(col1[1]), float(col2[1])]]

        # sanity-check so với ước lượng thô từ FOV
        fov_estimate = [
            GATEWAY_CONFIG["camera_image_width_px"] / GATEWAY_CONFIG["camera_fov_width_mm"],
            GATEWAY_CONFIG["camera_image_height_px"] / GATEWAY_CONFIG["camera_fov_height_mm"],
        ]
        measured_scale = [hypot(*col1), hypot(*col2)]
        sanity_msgs = []
        for axis_name, est, meas in zip(("X", "Y"), fov_estimate, measured_scale):
            ratio = meas / est if est else float("inf")
            if ratio < 0.5 or ratio > 2.0:
                sanity_msgs.append(
                    f"⚠️ Tỉ lệ trục {axis_name} đo được ({meas:.3f}px/mm) lệch nhiều so với ước lượng FOV "
                    f"({est:.3f}px/mm) - kiểm tra lại FOV/camera_image_*_px hoặc delta_mm."
                )
        sanity_check = " ".join(sanity_msgs) if sanity_msgs else "OK - khớp hợp lý với ước lượng từ FOV."

        GATEWAY_CONFIG["camera_axis_matrix"] = T
        GATEWAY_CONFIG["camera_axis_calibrated"] = True
        save_gateway_config(GATEWAY_CONFIG)

        return CameraAxisCalibResponse(
            success=True,
            message=f"Hiệu chỉnh ma trận trục T thành công (delta={delta}mm, anchor={request.anchor_name})",
            anchor_board_xy=[bx, by], anchor_plc_expected_xy=list(p0_plc),
            camera_axis_matrix=T, samples_pixel_xy=pixel_samples, sanity_check=sanity_check,
        )
    except Exception as e:
        logger.error(f"❌ calibrate_camera_axis thất bại: {e}\n{traceback.format_exc()}")
        return CameraAxisCalibResponse(
            success=False, message=f"Lỗi: {e}",
            anchor_board_xy=[bx, by], anchor_plc_expected_xy=list(p0_plc),
        )
    finally:
        plc.close()


# ===============================
# MỚI: tiện ích xem trước / đọc trạng thái offset
# ===============================
@app.post("/api/calib/board-to-plc", response_model=BoardToPlcPreviewResponse)
async def preview_board_to_plc(request: BoardToPlcPreviewRequest):
    """Xem trước toạ độ PLC (chưa bù & đã bù) cho 1 điểm Board X/Y, dùng offset đã lưu gần nhất."""
    side = (request.board_side or "A").upper()
    validate_board_side(side)

    coeffs = load_calibration_matrix(resolve_calib_path(side))
    nominal = board_to_plc(request.board_x, request.board_y, coeffs)

    offset_path = resolve_offset_path(side)
    loaded = load_saved_rigid_offset(offset_path)
    if loaded is None:
        return BoardToPlcPreviewResponse(
            board_xy=[request.board_x, request.board_y], board_side=side,
            plc_nominal_xy=[nominal[0], nominal[1]],
            plc_compensated_xy=[nominal[0], nominal[1]], offset_loaded=False,
        )

    R, t, raw = loaded
    final = apply_rigid_offset(*nominal, R, t)
    return BoardToPlcPreviewResponse(
        board_xy=[request.board_x, request.board_y], board_side=side,
        plc_nominal_xy=[nominal[0], nominal[1]],
        plc_compensated_xy=[final[0], final[1]], offset_loaded=True, offset_source=raw.get("source", "unknown"),
    )


@app.get("/api/calib/offset-status")
async def offset_status(board_side: str = "A"):
    """Đọc nội dung offset runtime cho mặt board đã cho (mặc định A)."""
    side = (board_side or "A").upper()
    if side not in ("A", "B"):
        raise HTTPException(status_code=400, detail=f"board_side phải là 'A' hoặc 'B', nhận được '{board_side}'")
    path = resolve_offset_path(side)
    if not os.path.exists(path):
        return {"exists": False, "path": path, "board_side": side}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {"exists": True, "path": path, "board_side": side, "data": data}


# ===============================
# Main
# ===============================
if __name__ == "__main__":
    import uvicorn

    print("=" * 60)
    print("🚀 Starting PLC Offset Gateway (AutoBoardOffset_YOLO_2Mat)")
    print("=" * 60)
    print("📡 URL: http://localhost:8093")
    print("📚 Docs: http://localhost:8093/docs")
    print("=" * 60)

    uvicorn.run(app, host="0.0.0.0", port=8093, log_level="info")
