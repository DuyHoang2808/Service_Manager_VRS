"""
Calib_Wizard_Ma_Hang_Moi.py - Cong cu GUI cho 2 buoc dau cua quy trinh them ma hang moi
(xem docs/Quy_trinh_them_ma_hang_moi.md, buoc 6-7):

  1. Chon file Excel toa do (calib_2mat.xlsx) -> sinh calib tinh
     (vrs_calib_side_a.json + vrs_calib_side_b.json).
  2. Chon mat board (A/B) -> chay bu lech THAT (di chuyen PLC + chup anh + YOLO qua
     gateway) -> sinh offset_runtime_side_a/b.json.

Luon hien thi ro MA HANG DANG ACTIVE truoc khi cho phep bam nut - tranh lap lai loi
thuc te da gap (ghi/doc nham calib cua mot ma hang khac vi khong biet dang active
ma hang nao). Ma hang active/calib_dir lay tu gateway (GET /api/products/active) neu
gateway dang chay, hoac doc truc tiep products_registry.yaml + active_product_state.json
neu khong ket noi duoc.

Chay: python gui/Calib_Wizard_Ma_Hang_Moi.py (can PySide6, xem requirements.txt)
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import requests
import yaml

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

# Khi dong goi bang PyInstaller, bootloader khong ton trong PYTHONIOENCODING nhu
# python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8 tai code, neu
# khong print/log tieng Viet co dau se nem UnicodeEncodeError va crash exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def _repo_root() -> Path:
    """Thu muc goc chua gateway/, calib/, gui/ nhu cau truc goc (xem module
    docstring - giu nguyen cau truc thu muc khi deploy, khong doi logic doc)."""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir dat exe trong 1 thu muc con rieng cua no
        # (<dist>/<name>/<name>.exe) - so voi __file__ tho (gui/ten_file.py,
        # 2 cap) can len THEM 1 cap nua de bu vao thu muc <name>/ do PyInstaller
        # tu them, moi ra dung REPO_ROOT tuong duong.
        return Path(sys.executable).resolve().parent.parent.parent
    return Path(__file__).resolve().parent.parent


REPO_ROOT = _repo_root()
GATEWAY_DIR = REPO_ROOT / "gateway"
CALIB_DIR = REPO_ROOT / "calib"

for _p in (CALIB_DIR, GATEWAY_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from calib_2mat import fit_calib_model, read_excel_2mat  # noqa: E402
from bu_lech_board import get_anchor_points  # noqa: E402

DEFAULT_BASE_URL = "http://localhost:8083"


def _read_active_product_from_files() -> Optional[Dict[str, Any]]:
    """Doc truc tiep products_registry.yaml + active_product_state.json - dung khi
    khong goi duoc API gateway (vd dang chuan bi calib truoc khi bat gateway)."""
    registry_path = GATEWAY_DIR / "products_registry.yaml"
    state_path = GATEWAY_DIR / "active_product_state.json"
    try:
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None

    products = registry.get("products", {})
    product_code = None
    if state_path.exists():
        try:
            product_code = json.loads(state_path.read_text(encoding="utf-8")).get("product_code")
        except Exception:
            product_code = None
    if not product_code or product_code not in products:
        product_code = registry.get("default_product")

    product = products.get(product_code) if product_code else None
    if not product:
        return None
    return {"active_product": product_code, "config": product}


def _read_products_list_from_files() -> list[str]:
    """Doc danh sach ma hang truc tiep tu products_registry.yaml - dung khi khong
    goi duoc API gateway."""
    registry_path = GATEWAY_DIR / "products_registry.yaml"
    try:
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    return list(registry.get("products", {}).keys())


class CalibWizard(QMainWindow):
    active_product_loaded = Signal(object)
    select_product_done = Signal(object)
    static_calib_done = Signal(object)
    offset_done = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Công cụ hiệu chỉnh mã hàng mới (Calib Wizard)")
        self.resize(680, 620)

        self._active_calib_dir: Optional[Path] = None
        self._busy = False

        self.active_product_loaded.connect(self._on_active_product_loaded)
        self.select_product_done.connect(self._on_select_product_done)
        self.static_calib_done.connect(self._on_static_calib_done)
        self.offset_done.connect(self._on_offset_done)

        self._build_ui()
        self._update_anchor_points_label()
        self.refresh_active_product()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # --- Gateway URL + ma hang dang active ---
        top_group = QGroupBox("Kết nối gateway")
        top_form = QFormLayout(top_group)
        top_form.setContentsMargins(8, 8, 8, 8)
        top_form.setSpacing(4)

        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        top_form.addRow("Gateway URL:", self.base_url_edit)

        select_row = QHBoxLayout()
        self.product_combo = QComboBox()
        self.product_combo.setEditable(True)
        self.product_combo.setPlaceholderText("Gõ hoặc chọn mã hàng...")
        self.select_product_btn = QPushButton("Chọn mã hàng này")
        self.select_product_btn.clicked.connect(self._on_select_product_clicked)
        refresh_btn = QPushButton("Làm mới")
        refresh_btn.clicked.connect(self.refresh_active_product)
        select_row.addWidget(self.product_combo, 1)
        select_row.addWidget(self.select_product_btn)
        select_row.addWidget(refresh_btn)
        top_form.addRow("Mã hàng:", self._wrap(select_row))

        self.active_product_label = QLabel("Đang tải...")
        self.active_product_label.setWordWrap(True)
        self.active_product_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top_form.addRow("Đang active:", self.active_product_label)
        outer.addWidget(top_group)

        # --- Buoc 1: sinh calib tinh ---
        step1_group = QGroupBox("Bước 1: Sinh calib tĩnh (vrs_calib_side_a/b.json)")
        step1_form = QFormLayout(step1_group)
        step1_form.setContentsMargins(8, 8, 8, 8)
        step1_form.setSpacing(4)

        excel_row = QHBoxLayout()
        self.excel_path_edit = QLineEdit()
        self.excel_path_edit.setPlaceholderText("Chọn file calib_2mat.xlsx...")
        browse_btn = QPushButton("...")
        browse_btn.setMaximumWidth(32)
        browse_btn.clicked.connect(self._browse_excel)
        excel_row.addWidget(self.excel_path_edit, 1)
        excel_row.addWidget(browse_btn)
        step1_form.addRow("File Excel toạ độ:", self._wrap(excel_row))

        self.gen_calib_btn = QPushButton("Sinh calib tĩnh (mặt A + B)")
        self.gen_calib_btn.setMinimumHeight(30)
        self.gen_calib_btn.setStyleSheet(
            "QPushButton{background-color:#1565c0;color:white;font-weight:bold;}"
            "QPushButton:disabled{background-color:#90a4ae;color:#eceff1;}"
        )
        self.gen_calib_btn.clicked.connect(self._on_generate_calib_clicked)
        step1_form.addRow(self.gen_calib_btn)

        self.calib_result_label = QLabel("")
        self.calib_result_label.setWordWrap(True)
        self.calib_result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.calib_result_label.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        calib_scroll = QScrollArea()
        calib_scroll.setWidgetResizable(True)
        calib_scroll.setWidget(self.calib_result_label)
        calib_scroll.setMaximumHeight(220)
        calib_scroll.setFrameShape(QScrollArea.NoFrame)
        step1_form.addRow(calib_scroll)

        outer.addWidget(step1_group)

        # --- Buoc 2: chay bu lech that ---
        step2_group = QGroupBox("Bước 2: Chạy bù lệch thật (offset_runtime_side_a/b.json)")
        step2_form = QFormLayout(step2_group)
        step2_form.setContentsMargins(8, 8, 8, 8)
        step2_form.setSpacing(4)

        self.side_combo = QComboBox()
        self.side_combo.addItems(["A", "B"])
        step2_form.addRow("Mặt board:", self.side_combo)

        self.anchor_combo = QComboBox()
        self.anchor_combo.addItem("2 điểm (nhanh, khớp tuyệt đối)", 2)
        self.anchor_combo.addItem("3 điểm (chậm hơn, có kiểm tra sai số)", 3)
        self.anchor_combo.currentIndexChanged.connect(self._update_anchor_points_label)
        step2_form.addRow("Số điểm mốc:", self.anchor_combo)

        self.anchor_points_label = QLabel("")
        self.anchor_points_label.setWordWrap(True)
        self.anchor_points_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.anchor_points_label.setStyleSheet("color: #555;")
        step2_form.addRow("Toạ độ điểm mốc:", self.anchor_points_label)

        self.board_id_edit = QLineEdit()
        self.board_id_edit.setPlaceholderText("Tuỳ chọn - vd BOARD-001")
        step2_form.addRow("Board ID:", self.board_id_edit)

        self.run_offset_btn = QPushButton("Chạy bù lệch (di chuyển PLC + chụp ảnh thật)")
        self.run_offset_btn.setMinimumHeight(30)
        self.run_offset_btn.setStyleSheet(
            "QPushButton{background-color:#c62828;color:white;font-weight:bold;}"
            "QPushButton:disabled{background-color:#90a4ae;color:#eceff1;}"
        )
        self.run_offset_btn.clicked.connect(self._on_run_offset_clicked)
        step2_form.addRow(self.run_offset_btn)

        self.offset_result_label = QLabel("")
        self.offset_result_label.setWordWrap(True)
        self.offset_result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        step2_form.addRow(self.offset_result_label)

        outer.addWidget(step2_group)
        outer.addStretch(1)

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------
    # Ma hang dang active
    # ------------------------------------------------------------------
    def refresh_active_product(self) -> None:
        self.active_product_label.setText("Đang tải...")
        self.active_product_label.setStyleSheet("font-weight: bold;")
        base_url = self.base_url_edit.text().strip().rstrip("/")
        threading.Thread(
            target=self._load_active_product_worker, args=(base_url,),
            name="LoadActiveProduct", daemon=True,
        ).start()

    def _load_active_product_worker(self, base_url: str) -> None:
        data = None
        products: list[str] = []
        source = "gateway"
        try:
            resp = requests.get(f"{base_url}/api/products/active", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
            resp2 = requests.get(f"{base_url}/api/products", timeout=5)
            if resp2.status_code == 200:
                products = resp2.json().get("products") or []
        except Exception:
            data = None
        if not data or not data.get("active_product"):
            data = _read_active_product_from_files()
            products = products or _read_products_list_from_files()
            source = "file"
        self.active_product_loaded.emit((data, products, source))

    def _on_active_product_loaded(self, payload: Any) -> None:
        data, products, source = payload

        self.product_combo.blockSignals(True)
        self.product_combo.clear()
        self.product_combo.addItems(products)
        self.product_combo.blockSignals(False)

        if not data or not data.get("active_product"):
            self._active_calib_dir = None
            self.active_product_label.setText(
                "⚠ KHÔNG xác định được mã hàng đang active - gõ mã hàng ở trên rồi bấm "
                "'Chọn mã hàng này'."
            )
            self.active_product_label.setStyleSheet("font-weight: bold; color: #c62828;")
            return

        product_code = data["active_product"]
        config = data.get("config") or {}
        calib_dir = config.get("calib_dir")
        self._active_calib_dir = Path(calib_dir) if calib_dir else None
        self.product_combo.setCurrentText(product_code)

        note = "" if source == "gateway" else "  (đọc từ file cục bộ - gateway không phản hồi)"
        self.active_product_label.setText(f"{product_code}{note}")
        self.active_product_label.setStyleSheet("font-weight: bold; color: #1565c0;")

    # ------------------------------------------------------------------
    # Chon mot ma hang khac (POST /api/products/select)
    # ------------------------------------------------------------------
    def _on_select_product_clicked(self) -> None:
        if self._busy:
            return
        product_code = self.product_combo.currentText().strip()
        if not product_code:
            QMessageBox.warning(self, "Thiếu mã hàng", "Hãy gõ hoặc chọn 1 mã hàng trước.")
            return

        base_url = self.base_url_edit.text().strip().rstrip("/")
        self._set_busy(True)
        self.active_product_label.setText(f"Đang chọn mã hàng '{product_code}'...")
        self.active_product_label.setStyleSheet("font-weight: bold;")
        threading.Thread(
            target=self._select_product_worker, args=(base_url, product_code),
            name="SelectProduct", daemon=True,
        ).start()

    def _select_product_worker(self, base_url: str, product_code: str) -> None:
        try:
            resp = requests.post(
                f"{base_url}/api/products/select",
                json={"product_code": product_code}, timeout=40,
            )
            if resp.status_code == 400:
                result = {"success": False, "message": resp.json().get("detail", "HTTP 400")}
            else:
                result = resp.json()
        except Exception as e:
            result = {"success": False, "message": f"Không gọi được gateway: {e}"}
        self.select_product_done.emit(result)

    def _on_select_product_done(self, result: Dict[str, Any]) -> None:
        self._set_busy(False)
        if not result.get("success"):
            QMessageBox.critical(
                self, "Chọn mã hàng thất bại",
                f"{result.get('message')}\n\nMã hàng đang active KHÔNG bị đổi.",
            )
            self.refresh_active_product()
            return
        self.refresh_active_product()

    # ------------------------------------------------------------------
    # Buoc 1: sinh calib tinh
    # ------------------------------------------------------------------
    def _browse_excel(self) -> None:
        current = self.excel_path_edit.text().strip()
        start_dir = str(Path(current).parent) if current and Path(current).is_file() else str(CALIB_DIR)
        path, _filter = QFileDialog.getOpenFileName(
            self, "Chọn file Excel toạ độ", start_dir, "Excel (*.xlsx *.xls);;Tất cả (*.*)"
        )
        if path:
            self.excel_path_edit.setText(path)

    def _on_generate_calib_clicked(self) -> None:
        if self._busy:
            return
        excel_path = self.excel_path_edit.text().strip()
        if not excel_path or not Path(excel_path).is_file():
            QMessageBox.warning(self, "Thiếu file", "Hãy chọn 1 file Excel hợp lệ trước.")
            return
        if self._active_calib_dir is None:
            QMessageBox.critical(
                self, "Chưa có mã hàng active",
                "Không xác định được mã hàng đang active - không biết ghi calib vào đâu.\n"
                "Hãy chọn mã hàng qua POST /api/products/select trước, rồi bấm 'Làm mới'.",
            )
            return

        self._set_busy(True)
        self.calib_result_label.setStyleSheet("")
        self.calib_result_label.setText("Đang xử lý...")
        threading.Thread(
            target=self._generate_calib_worker, args=(excel_path, self._active_calib_dir),
            name="GenCalib", daemon=True,
        ).start()

    def _generate_calib_worker(self, excel_path: str, calib_dir: Path) -> None:
        try:
            rows_a, rows_b = read_excel_2mat(excel_path)
            calib_dir.mkdir(parents=True, exist_ok=True)
            generated = []
            for rows, side_label in ((rows_a, "A"), (rows_b, "B")):
                if not rows:
                    generated.append((side_label, None, "Không có dữ liệu PLC cho mặt này."))
                    continue
                if len(rows) < 4:
                    generated.append((side_label, None, f"Chỉ có {len(rows)} điểm, cần tối thiểu 4."))
                    continue
                names = [r[0] for r in rows]
                arr = np.array([(r[1], r[2], r[3], r[4]) for r in rows], dtype=float)
                fit = fit_calib_model(
                    arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3], point_names=names,
                )
                if fit is None:
                    generated.append((side_label, None, "Fit thất bại (điểm trùng/thẳng hàng?)."))
                    continue
                out_path = calib_dir / f"vrs_calib_side_{side_label.lower()}.json"
                out_path.write_text(json.dumps(fit, indent=4, ensure_ascii=False), encoding="utf-8")
                generated.append((side_label, str(out_path), fit["_diagnostics"]))
            result: Dict[str, Any] = {"success": True, "generated": generated}
        except Exception as e:
            result = {"success": False, "error": f"{e}\n{traceback.format_exc()}"}
        self.static_calib_done.emit(result)

    def _on_static_calib_done(self, result: Dict[str, Any]) -> None:
        self._set_busy(False)
        if not result.get("success"):
            self.calib_result_label.setStyleSheet("color: #c62828;")
            self.calib_result_label.setText(f"❌ Lỗi: {result.get('error')}")
            return

        lines = []
        any_error = False
        for side_label, path, info in result["generated"]:
            if path is None:
                any_error = True
                lines.append(f"Mặt {side_label}: ⚠ {info}")
                continue
            lines.append(
                f"Mặt {side_label}: ✅ đã lưu {path}\n"
                f"    RMS={info['rms_residual_mm']:.4f}mm  Max={info['max_residual_mm']:.4f}mm  "
                f"model={info['model']} ({info['n_points']} điểm)"
            )
            for pt in info.get("points", []):
                flag = "  ⚠ NGHI NGỜ OUTLIER" if pt["is_outlier"] else ""
                lines.append(
                    f"      {pt['name']}: Board({pt['board_x']:.3f}, {pt['board_y']:.3f}) "
                    f"PLC({pt['plc_x']:.3f}, {pt['plc_y']:.3f})  residual={pt['residual_mm']:.4f}mm{flag}"
                )
        self.calib_result_label.setStyleSheet("" if any_error else "color: #2e7d32;")
        self.calib_result_label.setText("\n".join(lines))

    # ------------------------------------------------------------------
    # Buoc 2: chay bu lech that
    # ------------------------------------------------------------------
    def _update_anchor_points_label(self) -> None:
        """Hien tam toa do Board (mm) cua cac diem moc se dung, theo che do 2/3 diem
        dang chon - de nguoi dung tu kiem tra truoc khi chay bu lech that."""
        anchor_mode = self.anchor_combo.currentData()
        if anchor_mode is None:
            self.anchor_points_label.setText("")
            return
        points = get_anchor_points(anchor_mode)
        text = "  |  ".join(f"{name}: Board ({x:.1f}, {y:.1f}) mm" for name, (x, y) in points)
        self.anchor_points_label.setText(text)

    def _on_run_offset_clicked(self) -> None:
        if self._busy:
            return
        if self._active_calib_dir is None:
            QMessageBox.critical(
                self, "Chưa có mã hàng active",
                "Không xác định được mã hàng đang active. Hãy chọn mã hàng trước.",
            )
            return

        side = self.side_combo.currentText()
        anchor_mode = self.anchor_combo.currentData()
        board_id = self.board_id_edit.text().strip() or None

        confirm = QMessageBox.question(
            self, "Xác nhận",
            f"Sẽ di chuyển PLC + chụp ảnh THẬT để đo bù lệch cho mặt {side}.\n"
            "Đảm bảo board mẫu đã được gá đúng vị trí trên máy.\n\nTiếp tục?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return

        base_url = self.base_url_edit.text().strip().rstrip("/")
        self._set_busy(True)
        self.offset_result_label.setStyleSheet("")
        self.offset_result_label.setText("Đang đo bù lệch (di chuyển PLC + chụp ảnh)...")
        threading.Thread(
            target=self._run_offset_worker,
            args=(base_url, side, anchor_mode, board_id),
            name="RunOffset", daemon=True,
        ).start()

    def _run_offset_worker(
        self, base_url: str, side: str, anchor_mode: int, board_id: Optional[str],
    ) -> None:
        payload = {"anchor_mode": anchor_mode, "board_side": side, "board_id": board_id}
        try:
            resp = requests.post(f"{base_url}/api/calib/auto-board-offset", json=payload, timeout=60)
            result: Dict[str, Any] = {"success": True, "side": side, "data": resp.json()}
        except Exception as e:
            result = {"success": False, "side": side, "error": str(e)}
        self.offset_done.emit(result)

    def _on_offset_done(self, result: Dict[str, Any]) -> None:
        self._set_busy(False)
        if not result.get("success"):
            self.offset_result_label.setStyleSheet("color: #c62828;")
            self.offset_result_label.setText(f"❌ Không gọi được gateway: {result.get('error')}")
            return

        data = result["data"]
        side = data.get("board_side", result["side"])
        if not data.get("success"):
            self.offset_result_label.setStyleSheet("color: #c62828;")
            self.offset_result_label.setText(f"❌ Thất bại (mặt {side}): {data.get('message')}")
            return

        theta = data.get("theta_deg") or 0.0
        tx = data.get("tx") or 0.0
        ty = data.get("ty") or 0.0
        rms = data.get("rms_error_mm") or 0.0
        max_err = data.get("max_error_mm") or 0.0
        text = (
            f"✅ Mặt {side}: {data.get('message')}\n"
            f"theta={theta:+.4f}°  tx={tx:+.4f}  ty={ty:+.4f}\n"
            f"RMS={rms:.4f}mm  Max={max_err:.4f}mm\n"
            f"Đã lưu: {data.get('offset_saved_path')}"
        )
        if data.get("warning"):
            text += f"\n⚠ {data['warning']}"
        self.offset_result_label.setStyleSheet("color: #2e7d32;")
        self.offset_result_label.setText(text)

    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.select_product_btn.setEnabled(not busy)
        self.gen_calib_btn.setEnabled(not busy)
        self.run_offset_btn.setEnabled(not busy)


def main() -> None:
    app = QApplication(sys.argv)
    window = CalibWizard()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
