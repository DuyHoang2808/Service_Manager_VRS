"""
Do_khoang_cach_kem_camera_v5.py
--------------------------------------------------------------------
Gop app "Do khoang cach + Camera" (v4, PySide6/Qt) VOI app "PLC Gateway
Tester + Bu lech Board" (truoc day la 1 file Tkinter rieng, chay 1
tien trinh khac) thanh MOT app DUY NHAT, chung 1 cua so.

VI SAO GOP: 2 chuc nang nay truoc day noi voi nhau qua HTTP (app PLC
POST toa do sang API cua app do khoang cach o port 8090). Neu 2 app
chay tren 2 may khac nhau hoac mang tam thoi gian doan, buoc forward
nay co the loi (xem log "[Forward] ... connection refused" tung gap).
Khi gop chung 1 tien trinh, buoc cap nhat toa do PLC X/Y sang tab
"Do khoang cach" duoc thay bang GOI Qt Signal NOI BO truc tiep (cung
co che voi PlcPositionBridge da co san trong v4) -> KHONG con goi
HTTP nao giua 2 chuc nang nay nua, nen KHONG the loi do mat ket noi
mang.

PLC Gateway (server that dieu khien phan cung, port 8093, chay bang
run_plc_gateway.py) VAN LA 1 tien trinh rieng - khong gop vao day,
vi do la server dieu khien phan cung that, khong phai giao dien.

Giao dien (QTabWidget):
    Tab 1 "Do khoang cach + Camera" : nguyen ven MainWindow/MainWindowV4
                                       (v3 + v4), KHONG doi logic/UI.
    Tab 2 "PLC Gateway Tester"      : chuyen the tu test_plc_gateway_gui_v2.py
                                       sang Qt, tai dung nguyen ham logic.
    Tab 3 "Bu lech Board (Calib)"   : chuyen the tu bu_lech_board.py sang
                                       Qt, tai dung nguyen ham logic.

Cac file goc KHONG bi sua doi, chi duoc import lai:
    - Do_khoang_cach_kem_camera_v3.py / v4.py (cung thu muc)
    - Funs/test_plc_gateway_gui_v2_with_board_offset.py (chi lay ham logic thuan)
    - ../gateway/bu_lech_board.py (chi lay ham logic thuan)

Chay (chi 1 lenh duy nhat, thay vi 2 lenh nhu truoc):
    python Do_khoang_cach_kem_camera_v5.py

(PLC Gateway server van phai chay rieng: python run_gateway.py, xem ../gateway/)

--- BAN TACH RIENG (AutoBoardOffset_YOLO_2Mat) ---
Day la BAN COPY doc lap cua file goc cung ten trong
Calib_Phan_Cung_VRS/Do_Khoang_Cach/ (ban goc KHONG bi dung/sua, van chay
duoc binh thuong nhu truoc). Ban nay tu tro vao gateway/ va calib/offset
RIENG trong folder AutoBoardOffset_YOLO_2Mat (xem block duoi day), KHONG
doc/ghi vao Calib_Phan_Cung_VRS/Calib_4diem/ cua ban goc - de tranh dung
vao du lieu that dang chay va de phat trien tinh nang board 2 mat (A/B)
ma khong anh huong workflow hien tai.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from urllib import error

import numpy as np

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# --- Thiet lap duong dan NOI BO trong AutoBoardOffset_YOLO_2Mat (KHONG dung
# DEV_DIR/Calib_Phan_Cung_VRS/Calib_4diem cua ban goc nua - ban nay doc lap
# hoan toan, tu tro vao ../gateway/ cua chinh no) ---
THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # .../AutoBoardOffset_YOLO_2Mat/gui
PROJECT_DIR = os.path.dirname(THIS_DIR)                          # .../AutoBoardOffset_YOLO_2Mat
GATEWAY_DIR = os.path.join(PROJECT_DIR, "gateway")               # bu_lech_board.py + calib/offset o day

for _p in (THIS_DIR, GATEWAY_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from Do_khoang_cach_kem_camera_v4 import MainWindowV4  # noqa: E402

# Luu y: cac ham logic thuan (build_payload, send_json...) hien nam trong
# test_plc_gateway_gui_v2_with_board_offset.py - ban Tkinter gop gateway +
# bu lech hien tai cua ban (test_plc_gateway_gui_v2.py gio chi la launcher
# import lai tu file nay). Import dung tu day de luon khop voi ban dang chay.
# gui/Funs/test_plc_gateway_gui_v2_with_board_offset.py (copy rieng, doc lap)
from Funs.test_plc_gateway_gui_v2_with_board_offset import (  # noqa: E402
    DEFAULT_BASE_URL,
    INSPECT_ENDPOINT,
    MOVE_ENDPOINT,
    build_payload,
    extract_sent_xy,
    parse_coordinates,
    pretty_json,
    response_reports_success,
    send_json,
)
# ../gateway/bu_lech_board.py (CUNG 1 file ma gateway PA2 dang dung - dam bao
# GUI va gateway luon tinh toan giong het nhau, khong bi lech logic)
from bu_lech_board import (  # noqa: E402
    ANCHOR_POINTS_POOL,
    ANCHOR_SETS,
    apply_rigid_offset,
    board_to_plc,
    kabsch_2d,
    load_calibration_matrix,
)

CALIB_JSON_PATH = os.path.join(GATEWAY_DIR, "vrs_calib_4diem.json")
OFFSET_JSON_PATH = os.path.join(GATEWAY_DIR, "offset_runtime.json")


def rotation_matrix(theta_rad: float) -> np.ndarray:
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    return np.array([[c, -s], [s, c]], dtype=float)


class RequestSignal(QObject):
    """Cau noi thread nen (gui HTTP) -> GUI thread, dung chung cho ca 2 tab moi."""

    finished = Signal(str, str)


# ======================================================================
# TAB MOI 1: PLC GATEWAY TESTER (chuyen the Qt tu test_plc_gateway_gui_v2.py)
# ======================================================================
class GatewayTabQt(QWidget):
    def __init__(self, main_window) -> None:
        super().__init__()
        self.main_window = main_window

        self._signal = RequestSignal()
        self._signal.finished.connect(self._on_request_finished)

        self.base_url_edit = QLineEdit(DEFAULT_BASE_URL)
        self.radio_inspect = QRadioButton(INSPECT_ENDPOINT)
        self.radio_move = QRadioButton(MOVE_ENDPOINT)
        self.radio_inspect.setChecked(True)
        self.endpoint_group = QButtonGroup(self)
        self.endpoint_group.addButton(self.radio_inspect)
        self.endpoint_group.addButton(self.radio_move)
        self.radio_inspect.toggled.connect(self._on_endpoint_changed)

        self.x_edit = QLineEdit("100")
        self.y_edit = QLineEdit("200")
        self.board_id_edit = QLineEdit()
        self.defect_id_edit = QLineEdit()
        self.move_timeout_edit = QLineEdit("2000")
        self.ai_threshold_edit = QLineEdit("0.25")
        self.ai_api_url_edit = QLineEdit("http://localhost:8082/api/ai-detection")

        self.status_label = QLabel("San sang")
        self.output = QTextEdit()
        self.output.setReadOnly(True)

        self.health_btn = QPushButton("GET /")
        self.send_btn = QPushButton("Gui POST")
        self.clear_btn = QPushButton("Xoa ket qua")

        self.health_btn.clicked.connect(self.send_health_check)
        self.send_btn.clicked.connect(self.send_post)
        self.clear_btn.clicked.connect(self.output.clear)

        self._build_ui()
        self._on_endpoint_changed()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        conn_group = QGroupBox("Ket noi")
        conn_layout = QHBoxLayout(conn_group)
        conn_layout.addWidget(QLabel("Gateway URL:"))
        conn_layout.addWidget(self.base_url_edit)
        conn_layout.addWidget(self.health_btn)
        layout.addWidget(conn_group)

        ep_group = QGroupBox("Endpoint POST")
        ep_layout = QHBoxLayout(ep_group)
        ep_layout.addWidget(self.radio_inspect)
        ep_layout.addWidget(self.radio_move)
        ep_layout.addStretch()
        layout.addWidget(ep_group)

        data_group = QGroupBox("Du lieu gui")
        data_form = QFormLayout(data_group)
        data_form.addRow("X:", self.x_edit)
        data_form.addRow("Y:", self.y_edit)
        data_form.addRow("Board ID:", self.board_id_edit)
        data_form.addRow("Defect ID:", self.defect_id_edit)
        layout.addWidget(data_group)

        self.inspect_group = QGroupBox("Tuy chon inspect-defect")
        inspect_form = QFormLayout(self.inspect_group)
        inspect_form.addRow("PLC timeout (ms):", self.move_timeout_edit)
        inspect_form.addRow("AI threshold:", self.ai_threshold_edit)
        inspect_form.addRow("AI API URL:", self.ai_api_url_edit)
        layout.addWidget(self.inspect_group)

        note_label = QLabel(
            "Ghi chu: sau khi gui THANH CONG, toa do se duoc CAP NHAT TRUC TIEP\n"
            "(khong qua mang, khong the loi mat ket noi) vao o PLC X/Y cua tab\n"
            "'Do khoang cach + Camera'."
        )
        layout.addWidget(note_label)

        actions = QHBoxLayout()
        actions.addWidget(self.send_btn)
        actions.addWidget(self.clear_btn)
        actions.addStretch()
        actions.addWidget(self.status_label)
        layout.addLayout(actions)

        layout.addWidget(self.output, 1)

    def _on_endpoint_changed(self, *_args) -> None:
        self.inspect_group.setVisible(self.radio_inspect.isChecked())

    def _current_endpoint(self) -> str:
        return INSPECT_ENDPOINT if self.radio_inspect.isChecked() else MOVE_ENDPOINT

    def _gateway_url(self, endpoint: str) -> str:
        return f"{self.base_url_edit.text().rstrip('/')}{endpoint}"

    def _append_output(self, text: str) -> None:
        self.output.moveCursor(QTextCursor.End)
        self.output.insertPlainText(text)
        self.output.moveCursor(QTextCursor.End)
        self.output.ensureCursorVisible()

    def send_health_check(self) -> None:
        self._start_request("GET", "/", None, None)

    def send_post(self) -> None:
        endpoint = self._current_endpoint()
        try:
            x, y = parse_coordinates(self.x_edit.text(), self.y_edit.text())
            if x > 250 or y > 250:
                reply = QMessageBox.question(
                    self,
                    "Canh bao toa do lon",
                    f"Gia tri X/Y hien tai la X={x:g}, Y={y:g}.\n"
                    "Mot trong hai gia tri vuot qua nguong 250.\n"
                    "Ban co muon tiep tuc gui request khong?",
                )
                if reply != QMessageBox.Yes:
                    self.status_label.setText("Da huy gui request")
                    return

            payload = build_payload(
                endpoint=endpoint,
                x_text=self.x_edit.text(),
                y_text=self.y_edit.text(),
                board_id_text=self.board_id_edit.text(),
                defect_id_text=self.defect_id_edit.text(),
                move_timeout_text=self.move_timeout_edit.text(),
                ai_threshold_text=self.ai_threshold_edit.text(),
                ai_api_url=self.ai_api_url_edit.text(),
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Du lieu khong hop le", str(exc))
            return
        self._start_request("POST", endpoint, payload, endpoint)

    def _start_request(
        self, method: str, endpoint: str, payload: dict | None, endpoint_for_extract: str | None
    ) -> None:
        url = self._gateway_url(endpoint)
        self.send_btn.setEnabled(False)
        self.health_btn.setEnabled(False)
        self.status_label.setText("Dang gui request...")
        self._append_output(f"\n{'=' * 72}\n{method} {url}\n")
        if payload is not None:
            self._append_output("Request:\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        threading.Thread(
            target=self._request_worker,
            args=(method, url, payload, endpoint_for_extract),
            daemon=True,
        ).start()

    def _request_worker(
        self, method: str, url: str, payload: dict | None, endpoint_for_extract: str | None
    ) -> None:
        started = time.perf_counter()
        try:
            status_code, response_body = send_json(method, url, payload)
            elapsed = time.perf_counter() - started
            message = f"\nHTTP {status_code} ({elapsed:.2f}s)\nResponse:\n{pretty_json(response_body)}\n"

            # QUAN TRONG: toi day PLC/inspect DA thanh cong. Cap nhat sau day
            # la NOI BO (cung tien trinh, qua Qt Signal) - neu co loi thi CHI
            # bo qua, KHONG anh huong lenh vua gui.
            if (
                payload is not None
                and endpoint_for_extract is not None
                and status_code == 200
                and response_reports_success(response_body)
            ):
                x, y = extract_sent_xy(endpoint_for_extract, payload)
                if x is not None and y is not None:
                    try:
                        self.main_window.request_plc_position_update(float(x), float(y))
                        message += (
                            f"\n[Cap nhat noi bo] Da cap nhat PLC X/Y = ({x}, {y}) "
                            "vao tab 'Do khoang cach + Camera'.\n"
                        )
                    except Exception as exc:
                        message += f"\n[Cap nhat noi bo] BO QUA (khong anh huong lenh PLC): {exc}\n"

            self._signal.finished.emit(message, f"Hoan tat: HTTP {status_code}")
        except error.URLError as exc:
            elapsed = time.perf_counter() - started
            message = f"\nLoi ket noi ({elapsed:.2f}s): {exc.reason}\n"
            self._signal.finished.emit(message, "Khong the ket noi gateway")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            message = f"\nLoi ({elapsed:.2f}s): {exc}\n"
            self._signal.finished.emit(message, "Request that bai")

    def _on_request_finished(self, message: str, status_text: str) -> None:
        self._append_output(message)
        self.status_label.setText(status_text)
        self.send_btn.setEnabled(True)
        self.health_btn.setEnabled(True)


# ======================================================================
# TAB MOI 2: BU LECH BOARD (Kabsch 2/3 diem) - chuyen the Qt tu bu_lech_board.py
# ======================================================================
class BuLechTabQt(QWidget):
    def __init__(self, main_window) -> None:
        super().__init__()
        self.main_window = main_window

        self._signal = RequestSignal()
        self._signal.finished.connect(self._on_move_finished)

        self.coeffs: dict | None = None
        self.R: np.ndarray | None = None
        self.t: np.ndarray | None = None
        self.measured_edits: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        self.expected_labels: dict[str, QLabel] = {}

        self.radio_2 = QRadioButton("2 diem (A, C) - nhanh, khop tuyet doi, khong co residual")
        self.radio_3 = QRadioButton("3 diem (A, C, D) - co residual de tu kiem tra loi do/board meo")
        self.radio_3.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.radio_2)
        self.mode_group.addButton(self.radio_3)
        self.radio_3.toggled.connect(self._rebuild_anchor_rows)

        self.calib_status_label = QLabel("Chua nap ma tran calib.")
        self.calib_path_edit = QLineEdit(CALIB_JSON_PATH)
        self.browse_calib_btn = QPushButton("Duyet...")
        self.browse_calib_btn.clicked.connect(self._browse_calib_path)
        self.reload_calib_btn = QPushButton("Nap lai")
        self.reload_calib_btn.clicked.connect(self.load_calibration)

        self.anchor_group = QGroupBox("Nhap toa do PLC do duoc tai tung diem moc")
        self.anchor_layout = QFormLayout(self.anchor_group)

        self.compute_btn = QPushButton("Tinh bu lech (Kabsch)")
        self.compute_btn.clicked.connect(self.compute_offset)
        self.load_offset_btn = QPushButton("Nap offset da luu")
        self.load_offset_btn.clicked.connect(lambda: self.load_saved_offset(startup=False))

        self.offset_path_edit = QLineEdit(OFFSET_JSON_PATH)
        self.browse_offset_btn = QPushButton("Duyet...")
        self.browse_offset_btn.clicked.connect(self._browse_offset_path)

        self.offset_info_label = QLabel("Chua co du lieu bu lech. Hay tinh hoac nap offset.")

        self.target_x_edit = QLineEdit("0")
        self.target_y_edit = QLineEdit("0")
        self.target_board_id_edit = QLineEdit()
        self.target_defect_id_edit = QLineEdit()
        self.preview_label = QLabel("-")
        self.preview_btn = QPushButton("Xem truoc toa do PLC")
        self.preview_btn.clicked.connect(self.preview_plc)
        self.send_move_btn = QPushButton("Gui PLC (move)")
        self.send_move_btn.clicked.connect(self.send_move)
        self.send_to_gateway_btn = QPushButton("Gui toa do sang PLC Gateway Tester")
        self.send_to_gateway_btn.clicked.connect(self.send_to_gateway_tab)

        self.log = QTextEdit()
        self.log.setReadOnly(True)

        self._build_ui()
        self.load_calibration()
        self.load_saved_offset(startup=True)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        calib_group = QGroupBox("Ma tran calib tinh (bilinear)")
        calib_layout = QHBoxLayout(calib_group)
        calib_layout.addWidget(QLabel("Duong dan:"))
        calib_layout.addWidget(self.calib_path_edit)
        calib_layout.addWidget(self.browse_calib_btn)
        calib_layout.addWidget(self.reload_calib_btn)
        layout.addWidget(calib_group)
        layout.addWidget(self.calib_status_label)

        mode_group = QGroupBox("Chon so diem moc bu lech")
        mode_layout = QVBoxLayout(mode_group)
        mode_layout.addWidget(self.radio_2)
        mode_layout.addWidget(self.radio_3)
        layout.addWidget(mode_group)

        layout.addWidget(self.anchor_group)

        offset_path_group = QGroupBox("File offset da luu (doc/ghi)")
        offset_path_layout = QHBoxLayout(offset_path_group)
        offset_path_layout.addWidget(QLabel("Duong dan:"))
        offset_path_layout.addWidget(self.offset_path_edit)
        offset_path_layout.addWidget(self.browse_offset_btn)
        layout.addWidget(offset_path_group)

        calc_row = QHBoxLayout()
        calc_row.addWidget(self.compute_btn)
        calc_row.addWidget(self.load_offset_btn)
        calc_row.addStretch()
        layout.addLayout(calc_row)

        result_group = QGroupBox("Ket qua bu lech hien tai")
        result_layout = QVBoxLayout(result_group)
        result_layout.addWidget(self.offset_info_label)
        layout.addWidget(result_group)

        move_group = QGroupBox("Di chuyen den vi tri tren Board (tu dong ap dung bu lech)")
        move_form = QFormLayout(move_group)
        move_form.addRow("Board X (mm):", self.target_x_edit)
        move_form.addRow("Board Y (mm):", self.target_y_edit)
        move_form.addRow("Board ID (tuy chon):", self.target_board_id_edit)
        move_form.addRow("Defect ID (tuy chon):", self.target_defect_id_edit)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.preview_btn)
        btn_row.addWidget(self.send_move_btn)
        btn_row.addWidget(self.send_to_gateway_btn)
        btn_row.addStretch()
        move_form.addRow(btn_row)
        move_form.addRow(self.preview_label)
        layout.addWidget(move_group)

        note_label = QLabel(
            "Ghi chu: sau khi gui PLC THANH CONG, toa do se duoc CAP NHAT TRUC TIEP\n"
            "(khong qua mang, khong the loi mat ket noi) vao tab 'Do khoang cach + Camera'."
        )
        layout.addWidget(note_label)

        layout.addWidget(QLabel("Nhat ky:"))
        layout.addWidget(self.log, 1)

        self._rebuild_anchor_rows()

    def _log(self, text: str) -> None:
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)
        self.log.ensureCursorVisible()

    def _current_mode(self) -> int:
        return 3 if self.radio_3.isChecked() else 2

    def _current_anchor_names(self) -> list[str]:
        return ANCHOR_SETS[self._current_mode()]

    def _offset_path(self) -> str:
        """Duong dan file offset dang dung: lay tu o nhap, rong thi dung mac dinh."""
        return self.offset_path_edit.text().strip() or OFFSET_JSON_PATH

    def _browse_offset_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Chon file offset (doc hoac se ghi vao day)",
            self._offset_path(),
            "JSON (*.json);;Tat ca (*.*)",
        )
        if path:
            self.offset_path_edit.setText(path)

    def _calib_path(self) -> str:
        """Duong dan file ma tran calib (vrs_calib_4diem.json) dang dung:
        lay tu o nhap, rong thi dung mac dinh."""
        return self.calib_path_edit.text().strip() or CALIB_JSON_PATH

    def _browse_calib_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Chon file ma tran calib (vrs_calib_4diem.json)",
            self._calib_path(),
            "JSON (*.json);;Tat ca (*.*)",
        )
        if path:
            self.calib_path_edit.setText(path)
            self.load_calibration()

    # ---------------------------------------------------------- Calib IO
    def load_calibration(self) -> None:
        calib_path = self._calib_path()
        try:
            self.coeffs = load_calibration_matrix(calib_path)
            n = self.coeffs.get("_diagnostics", {}).get("n_points", "?")
            self.calib_status_label.setText(
                f"Da nap ma tran calib thanh cong ({n} diem goc) tu: {calib_path}"
            )
        except Exception as exc:
            self.coeffs = None
            self.calib_status_label.setText(f"LOI nap ma tran calib: {exc}")
        if hasattr(self, "anchor_layout"):
            self._rebuild_anchor_rows()

    def _rebuild_anchor_rows(self, *_args) -> None:
        while self.anchor_layout.rowCount() > 0:
            self.anchor_layout.removeRow(0)
        self.measured_edits = {}
        self.expected_labels = {}

        for name in self._current_anchor_names():
            bx, by = ANCHOR_POINTS_POOL[name]
            if self.coeffs is not None:
                ex, ey = board_to_plc(bx, by, self.coeffs)
                expected_text = f"PLC ky vong: ({ex:.3f}, {ey:.3f})"
            else:
                expected_text = "PLC ky vong: (chua co calib)"

            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            exp_label = QLabel(expected_text)
            row_layout.addWidget(exp_label)
            x_edit = QLineEdit()
            x_edit.setPlaceholderText("PLC X do duoc")
            y_edit = QLineEdit()
            y_edit.setPlaceholderText("PLC Y do duoc")
            row_layout.addWidget(x_edit)
            row_layout.addWidget(y_edit)

            self.anchor_layout.addRow(f"Diem {name}  Board ({bx:.3f}, {by:.3f}):", row_widget)
            self.expected_labels[name] = exp_label
            self.measured_edits[name] = (x_edit, y_edit)

    # -------------------------------------------------------- Kabsch
    def compute_offset(self) -> None:
        if self.coeffs is None:
            QMessageBox.critical(self, "Chua co calib", "Vui long nap ma tran calib (vrs_calib_4diem.json) truoc.")
            return

        names = self._current_anchor_names()
        expected_points, measured_points = [], []
        try:
            for name in names:
                bx, by = ANCHOR_POINTS_POOL[name]
                expected_points.append(board_to_plc(bx, by, self.coeffs))
                x_edit, y_edit = self.measured_edits[name]
                mx = float(x_edit.text().strip())
                my = float(y_edit.text().strip())
                measured_points.append((mx, my))
        except ValueError:
            QMessageBox.critical(
                self,
                "Du lieu khong hop le",
                "Vui long nhap day du toa do PLC (so thuc) cho tat ca diem moc.",
            )
            return

        try:
            R, t, theta, rms_error, max_error, residuals = kabsch_2d(expected_points, measured_points)
        except Exception as exc:
            QMessageBox.critical(self, "Loi tinh toan", str(exc))
            return

        self.R, self.t = R, t

        lines = [
            f"Goc lech board:      {math.degrees(theta):+.4f} do",
            f"Dich chuyen bu (X):  {t[0]:+.4f} mm",
            f"Dich chuyen bu (Y):  {t[1]:+.4f} mm",
            f"Sai so du RMS:       {rms_error:.4f} mm (max: {max_error:.4f} mm)",
        ]
        for name, res in zip(names, residuals):
            flag = "  <-- kiem tra lai phep do!" if res > 2 * rms_error and res > 0.02 else ""
            lines.append(f"  Sai so tai {name}: {res:.4f} mm{flag}")
        if len(names) >= 3 and rms_error >= 0.03:
            lines.append("=> Sai so du kha lon, nen do lai hoac kiem tra board bi meo.")
        self.offset_info_label.setText("\n".join(lines))

        offset_data = {
            "anchor_mode": len(names),
            "theta_deg": math.degrees(theta),
            "tx": float(t[0]),
            "ty": float(t[1]),
            "rms_error_mm": rms_error,
            "max_error_mm": max_error,
            "anchor_points_board": [(name, ANCHOR_POINTS_POOL[name]) for name in names],
            "anchor_points_expected_plc": expected_points,
            "anchor_points_measured_plc": measured_points,
            "residuals_mm": residuals.tolist(),
        }
        offset_path = self._offset_path()
        try:
            with open(offset_path, "w", encoding="utf-8") as f:
                json.dump(offset_data, f, indent=4, ensure_ascii=False)
            self._log(f"\nDa tinh va luu offset vao: {offset_path}\n")
        except Exception as exc:
            self._log(f"\nLOI luu offset: {exc}\n")

    def load_saved_offset(self, startup: bool) -> None:
        offset_path = self._offset_path()
        if not os.path.exists(offset_path):
            if not startup:
                QMessageBox.information(self, "Khong tim thay", f"Chua co file {offset_path}.")
            return
        try:
            with open(offset_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            theta = math.radians(data["theta_deg"])
            self.R = rotation_matrix(theta)
            self.t = np.array([data["tx"], data["ty"]], dtype=float)
            lines = [
                f"[Da nap tu file] Goc lech: {data['theta_deg']:+.4f} do",
                f"Dich chuyen bu (X): {data['tx']:+.4f} mm",
                f"Dich chuyen bu (Y): {data['ty']:+.4f} mm",
                f"Sai so du RMS: {data['rms_error_mm']:.4f} mm (max: {data['max_error_mm']:.4f} mm)",
            ]
            self.offset_info_label.setText("\n".join(lines))
            if not startup:
                self._log(f"\nDa nap offset tu: {offset_path}\n")
        except Exception as exc:
            if not startup:
                QMessageBox.critical(self, "Loi doc file", str(exc))

    # ------------------------------------------------------ Di chuyen
    def _compute_final_plc(self):
        if self.coeffs is None:
            raise RuntimeError("Chua nap ma tran calib.")
        if self.R is None or self.t is None:
            raise RuntimeError("Chua co du lieu bu lech. Hay 'Tinh bu lech' hoac 'Nap offset da luu' truoc.")
        bx, by = parse_coordinates(self.target_x_edit.text(), self.target_y_edit.text())
        nominal = board_to_plc(bx, by, self.coeffs)
        final = apply_rigid_offset(*nominal, self.R, self.t)
        return bx, by, nominal, final

    def preview_plc(self) -> None:
        try:
            bx, by, nominal, final = self._compute_final_plc()
        except Exception as exc:
            QMessageBox.critical(self, "Loi", str(exc))
            return
        self.preview_label.setText(
            f"Board ({bx:g}, {by:g}) -> PLC chua bu: ({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"PLC DA BU LECH        : X = {final[0]:.3f}   Y = {final[1]:.3f}"
        )

    def send_move(self) -> None:
        try:
            bx, by, nominal, final = self._compute_final_plc()
        except Exception as exc:
            QMessageBox.critical(self, "Loi", str(exc))
            return

        self.preview_label.setText(
            f"Board ({bx:g}, {by:g}) -> PLC chua bu: ({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"PLC DA BU LECH        : X = {final[0]:.3f}   Y = {final[1]:.3f}"
        )

        if abs(final[0]) > 250 or abs(final[1]) > 250:
            reply = QMessageBox.question(
                self,
                "Canh bao toa do lon",
                f"Toa do PLC da bu la X={final[0]:.3f}, Y={final[1]:.3f}, vuot nguong 250.\n"
                "Ban co muon tiep tuc gui request khong?",
            )
            if reply != QMessageBox.Yes:
                return

        try:
            payload = build_payload(
                endpoint=MOVE_ENDPOINT,
                x_text=str(final[0]),
                y_text=str(final[1]),
                board_id_text=self.target_board_id_edit.text(),
                defect_id_text=self.target_defect_id_edit.text(),
            )
        except ValueError as exc:
            QMessageBox.critical(self, "Du lieu khong hop le", str(exc))
            return

        base_url = self.main_window.gateway_base_url()
        url = f"{base_url}{MOVE_ENDPOINT}"

        self._log(f"\n{'=' * 72}\nPOST {url}\nRequest:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
        threading.Thread(
            target=self._move_worker,
            args=(url, payload),
            daemon=True,
        ).start()

    def _move_worker(self, url: str, payload: dict) -> None:
        started = time.perf_counter()
        try:
            status_code, response_body = send_json("POST", url, payload)
            elapsed = time.perf_counter() - started
            message = f"\nHTTP {status_code} ({elapsed:.2f}s)\nResponse:\n{pretty_json(response_body)}\n"

            # QUAN TRONG: toi day PLC DA di chuyen THANH CONG. Cap nhat ben
            # duoi la NOI BO (Qt Signal, cung tien trinh) - neu loi thi CHI
            # bo qua, KHONG anh huong lenh PLC vua gui.
            if status_code == 200 and response_reports_success(response_body):
                x, y = extract_sent_xy(MOVE_ENDPOINT, payload)
                if x is not None and y is not None:
                    try:
                        self.main_window.request_plc_position_update(float(x), float(y))
                        message += (
                            f"\n[Cap nhat noi bo] Da cap nhat PLC X/Y = ({x}, {y}) "
                            "vao tab 'Do khoang cach + Camera'.\n"
                        )
                    except Exception as exc:
                        message += f"\n[Cap nhat noi bo] BO QUA (khong anh huong lenh PLC): {exc}\n"

            self._signal.finished.emit(message, "")
        except error.URLError as exc:
            elapsed = time.perf_counter() - started
            self._signal.finished.emit(f"\nLoi ket noi ({elapsed:.2f}s): {exc.reason}\n", "")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self._signal.finished.emit(f"\nLoi ({elapsed:.2f}s): {exc}\n", "")

    def _on_move_finished(self, message: str, _status: str) -> None:
        self._log(message)

    def send_to_gateway_tab(self) -> None:
        """Dien toa do PLC da bu lech (tinh tu Board X/Y hien tai) sang tab
        'PLC Gateway Tester', roi chuyen sang tab do de nguoi dung tu kiem
        tra/bam Gui POST. Khong tu dong gui PLC o day."""
        try:
            bx, by, nominal, final = self._compute_final_plc()
        except Exception as exc:
            QMessageBox.critical(self, "Loi", str(exc))
            return

        self.preview_label.setText(
            f"Board ({bx:g}, {by:g}) -> PLC chua bu: ({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"PLC DA BU LECH        : X = {final[0]:.3f}   Y = {final[1]:.3f}"
        )

        gw = self.main_window.gateway_tab
        gw.radio_move.setChecked(True)
        gw.x_edit.setText(f"{final[0]:.6f}")
        gw.y_edit.setText(f"{final[1]:.6f}")
        gw.board_id_edit.setText(self.target_board_id_edit.text())
        gw.defect_id_edit.setText(self.target_defect_id_edit.text())

        self._log(
            f"\nDa gui toa do sang tab 'PLC Gateway Tester': X={final[0]:.3f}, Y={final[1]:.3f}\n"
        )
        try:
            self.main_window.show_gateway_tab()
        except Exception:
            pass


# ======================================================================
# APP CHINH: MainWindowV4 (camera + do khoang cach, KHONG doi) + 2 tab moi
# ======================================================================
class MergedMainWindow(MainWindowV4):
    """MainWindowV4 nguyen ven (camera, do khoang cach, zoom Sony, API port
    8090) + them 2 tab PLC Gateway Tester / Bu lech Board, tat ca chung 1
    cua so, 1 tien trinh -> khong con forward HTTP giua 2 chuc nang.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "VRS Control Center - Do khoang cach + PLC Gateway + Bu lech Board"
        )
        self.resize(1500, 860)
        self._wrap_central_in_tabs()
        self._add_two_point_send_button()

    def _wrap_central_in_tabs(self) -> None:
        # MainWindowV4.__init__() (qua MainWindow.build_ui()) da goi
        # self.setCentralWidget(central) san. Lay lai widget do de dat vao
        # tab dau tien, roi them 2 tab moi.
        measure_widget = self.centralWidget()
        measure_widget.setParent(None)

        tabs = QTabWidget()
        tabs.addTab(measure_widget, "Do khoang cach + Camera")

        self.gateway_tab = GatewayTabQt(self)
        tabs.addTab(self.gateway_tab, "PLC Gateway Tester")

        self.bulech_tab = BuLechTabQt(self)
        tabs.addTab(self.bulech_tab, "Bu lech Board (Calib)")

        self.setCentralWidget(tabs)
        self.tabs_widget = tabs

    def gateway_base_url(self) -> str:
        return self.gateway_tab.base_url_edit.text().rstrip("/")

    def show_gateway_tab(self) -> None:
        self.tabs_widget.setCurrentWidget(self.gateway_tab)

    def _add_two_point_send_button(self) -> None:
        """Them 1 nut vao ngay trong nhom 'Ket qua - Do 2 diem (P1/P2)' cua
        tab Do khoang cach (khong sua Do_khoang_cach_kem_camera_v3.py/v4.py):
        'result_two_point_group'/'result_two_point_layout' la bien cuc bo
        trong MainWindow.build_ui() nen khong the goi truc tiep self.xxx -
        tim lai layout do qua widget da luu san (self.plc_move_label), roi
        gan them nut vao cuoi layout.
        """
        try:
            container_widget = self.plc_move_label.parentWidget()
            container_layout = container_widget.layout()
        except Exception:
            return

        self.send_two_point_btn = QPushButton(
            "Gui X, Y (PLC dich P1->P2) sang PLC Gateway Tester"
        )
        self.send_two_point_btn.clicked.connect(self.send_two_point_result_to_gateway)
        container_layout.addWidget(self.send_two_point_btn)

    def send_two_point_result_to_gateway(self) -> None:
        """Lay toa do PLC dich (tinh tu P1/P2 da danh dau tren tab Do khoang
        cach) va dien sang tab 'PLC Gateway Tester', roi chuyen sang tab do.
        Khong tu dong gui PLC - nguoi dung tu kiem tra va bam Gui POST."""
        if getattr(self, "point1", None) is None or getattr(self, "point2", None) is None:
            QMessageBox.warning(
                self,
                "Chua du diem",
                "Vui long danh dau ca P1 va P2 (nut 'Click Chon P1'/'Click Chon P2') truoc.",
            )
            return

        try:
            result = self.calculate_p2_real_result()
        except Exception as exc:
            QMessageBox.critical(self, "Loi tinh toan", str(exc))
            return

        if not result or result.get("plc_target_x") is None or result.get("plc_target_y") is None:
            QMessageBox.warning(
                self,
                "Chua tinh duoc toa do",
                "Khong tinh duoc toa do PLC dich. Kiem tra da bat camera, "
                "nhap dung Zoom/FOV va toa do thuc P1 chua.",
            )
            return

        x = float(result["plc_target_x"])
        y = float(result["plc_target_y"])

        gw = self.gateway_tab
        gw.radio_move.setChecked(True)
        gw.x_edit.setText(f"{x:.6f}")
        gw.y_edit.setText(f"{y:.6f}")
        gw._append_output(
            f"\n[Do khoang cach] Da nhan toa do PLC dich tu P1->P2: X={x:.3f}, Y={y:.3f}\n"
        )

        self.show_gateway_tab()

    def request_plc_position_update(self, x: float, y: float) -> None:
        """Cap nhat PLC X/Y hien thi tren tab 'Do khoang cach' TRUC TIEP,
        khong qua HTTP. An toan khi goi tu bat ky thread nao vi day la Qt
        Signal (Qt tu dong dua ve GUI thread qua queued connection) - dung
        chung co che voi PlcPositionBridge da co san trong MainWindowV4.
        """
        self.bridge.position_received.emit(x, y)


def main() -> None:
    app = QApplication(sys.argv)
    window = MergedMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
