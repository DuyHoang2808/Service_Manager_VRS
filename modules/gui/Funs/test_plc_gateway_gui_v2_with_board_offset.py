"""
test_plc_gateway_gui_v2_with_board_offset.py

Giu nguyen giao dien PLC Gateway Tester v2 va them 1 tab moi de:
1. Nap ma tran calib goc tu Calib_4diem/vrs_calib_4diem.json.
2. Nhap 2 hoac 3 diem moc tren board moi de tinh bu lech rigid (Kabsch).
3. Nhap toa do Board muon den, tu dong doi ra toa do PLC da bu lech.
4. Gui POST /api/plc/move den gateway va forward vi tri sang app do khoang cach
   neu tinh nang forward dang bat.

Chay:
    python test_plc_gateway_gui_v2_with_board_offset.py
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from urllib import error, request

import numpy as np


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_DIR = os.path.dirname(THIS_DIR)
CALIB_DIR = os.path.join(DEV_DIR, "Calib_Phan_Cung_VRS", "Calib_4diem")

if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)
if CALIB_DIR not in sys.path:
    sys.path.insert(0, CALIB_DIR)

from bu_lech_board import (  # noqa: E402
    ANCHOR_POINTS_POOL,
    ANCHOR_SETS,
    apply_rigid_offset,
    board_to_plc,
    kabsch_2d,
    load_calibration_matrix,
)


CALIB_JSON_PATH = os.path.join(CALIB_DIR, "vrs_calib_4diem.json")
OFFSET_JSON_PATH = os.path.join(CALIB_DIR, "offset_runtime.json")
DEFAULT_BASE_URL = "http://localhost:8083"
INSPECT_ENDPOINT = "/api/inspect-defect"
MOVE_ENDPOINT = "/api/plc/move"
DEFAULT_FORWARD_URL = "http://localhost:8090/api/plc-position"


def optional_int(value: str, field_name: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} phai la so nguyen.") from exc


def parse_coordinates(x_text: str, y_text: str) -> tuple[float, float]:
    try:
        x = float(x_text)
        y = float(y_text)
    except ValueError as exc:
        raise ValueError("Toa do X va Y phai la so.") from exc
    return x, y


def build_payload(
    endpoint: str,
    x_text: str,
    y_text: str,
    board_id_text: str = "",
    defect_id_text: str = "",
    move_timeout_text: str = "2000",
    ai_threshold_text: str = "0.25",
    ai_api_url: str = "http://localhost:8082/api/ai-detection",
) -> dict:
    x, y = parse_coordinates(x_text, y_text)
    defect_id = optional_int(defect_id_text, "Defect ID")

    if endpoint == MOVE_ENDPOINT:
        return {
            "x": x,
            "y": y,
            "board_id": optional_int(board_id_text, "Board ID"),
            "defect_id": defect_id,
        }

    if endpoint != INSPECT_ENDPOINT:
        raise ValueError(f"Endpoint khong duoc ho tro: {endpoint}")

    try:
        move_timeout_ms = int(move_timeout_text)
        ai_threshold = float(ai_threshold_text)
    except ValueError as exc:
        raise ValueError("Timeout phai la so nguyen va AI threshold phai la so.") from exc

    return {
        "defect_x": x,
        "defect_y": y,
        "board_id": board_id_text.strip() or None,
        "defect_id": defect_id,
        "plc_move_timeout_ms": move_timeout_ms,
        "ai_confidence_threshold": ai_threshold,
        "ai_api_url": ai_api_url.strip(),
    }


def extract_sent_xy(endpoint: str, payload: dict) -> tuple[float | None, float | None]:
    if endpoint == MOVE_ENDPOINT:
        return payload.get("x"), payload.get("y")
    if endpoint == INSPECT_ENDPOINT:
        return payload.get("defect_x"), payload.get("defect_y")
    return None, None


def response_reports_success(response_body: str) -> bool:
    try:
        data = json.loads(response_body)
    except json.JSONDecodeError:
        return False
    return bool(data.get("success", True)) if isinstance(data, dict) else False


def send_json(method: str, url: str, payload: dict | None = None, timeout: int = 60) -> tuple[int, str]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    http_request = request.Request(url, data=body, headers=headers, method=method)

    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def pretty_json(raw_text: str) -> str:
    try:
        return json.dumps(json.loads(raw_text), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return raw_text


def rotation_matrix(theta_rad: float) -> np.ndarray:
    c = math.cos(theta_rad)
    s = math.sin(theta_rad)
    return np.array([[c, -s], [s, c]], dtype=float)


class GatewayTab(ttk.Frame):
    def __init__(self, parent: tk.Widget) -> None:
        super().__init__(parent)

        self.base_url = tk.StringVar(value=DEFAULT_BASE_URL)
        self.endpoint = tk.StringVar(value=INSPECT_ENDPOINT)
        self.x_value = tk.StringVar(value="100")
        self.y_value = tk.StringVar(value="200")
        self.board_id = tk.StringVar()
        self.defect_id = tk.StringVar()
        self.move_timeout = tk.StringVar(value="2000")
        self.ai_threshold = tk.StringVar(value="0.25")
        self.ai_api_url = tk.StringVar(value="http://localhost:8082/api/ai-detection")
        self.forward_enabled = tk.BooleanVar(value=True)
        self.forward_url = tk.StringVar(value=DEFAULT_FORWARD_URL)
        self.status = tk.StringVar(value="San sang")

        self._build_ui()
        self._on_endpoint_changed()

    def _build_ui(self) -> None:
        container = ttk.Frame(self, padding=12)
        container.pack(fill=tk.BOTH, expand=True)
        container.columnconfigure(1, weight=1)
        container.rowconfigure(6, weight=1)

        connection = ttk.LabelFrame(container, text="Ket noi", padding=10)
        connection.grid(row=0, column=0, columnspan=2, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Gateway URL").grid(row=0, column=0, padx=(0, 8), sticky="w")
        ttk.Entry(connection, textvariable=self.base_url).grid(row=0, column=1, sticky="ew")
        self.health_button = ttk.Button(connection, text="GET /", command=self.send_health_check)
        self.health_button.grid(row=0, column=2, padx=(8, 0))

        endpoint_frame = ttk.LabelFrame(container, text="Endpoint POST", padding=10)
        endpoint_frame.grid(row=1, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        ttk.Radiobutton(
            endpoint_frame,
            text=INSPECT_ENDPOINT,
            value=INSPECT_ENDPOINT,
            variable=self.endpoint,
            command=self._on_endpoint_changed,
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Radiobutton(
            endpoint_frame,
            text=MOVE_ENDPOINT,
            value=MOVE_ENDPOINT,
            variable=self.endpoint,
            command=self._on_endpoint_changed,
        ).pack(side=tk.LEFT)

        coordinates = ttk.LabelFrame(container, text="Du lieu gui", padding=10)
        coordinates.grid(row=2, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        for column in (1, 3):
            coordinates.columnconfigure(column, weight=1)
        self._add_entry(coordinates, "X", self.x_value, 0, 0)
        self._add_entry(coordinates, "Y", self.y_value, 0, 2)
        self._add_entry(coordinates, "Board ID", self.board_id, 1, 0)
        self._add_entry(coordinates, "Defect ID", self.defect_id, 1, 2)

        self.inspect_options = ttk.LabelFrame(container, text="Tuy chon inspect-defect", padding=10)
        self.inspect_options.grid(row=3, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        self.inspect_options.columnconfigure(1, weight=1)
        self.inspect_options.columnconfigure(3, weight=1)
        self._add_entry(self.inspect_options, "PLC timeout (ms)", self.move_timeout, 0, 0)
        self._add_entry(self.inspect_options, "AI threshold", self.ai_threshold, 0, 2)
        self._add_entry(self.inspect_options, "AI API URL", self.ai_api_url, 1, 0, columnspan=3)

        forward_frame = ttk.LabelFrame(container, text="Cap nhat app do khoang cach", padding=10)
        forward_frame.grid(row=4, column=0, columnspan=2, pady=(10, 0), sticky="ew")
        forward_frame.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            forward_frame,
            text="Tu dong gui vi tri sang app do khoang cach sau khi gui PLC thanh cong",
            variable=self.forward_enabled,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(forward_frame, text="API URL").grid(row=1, column=0, padx=(0, 8), pady=(4, 0), sticky="w")
        ttk.Entry(forward_frame, textvariable=self.forward_url).grid(
            row=1,
            column=1,
            pady=(4, 0),
            sticky="ew",
        )

        actions = ttk.Frame(container)
        actions.grid(row=5, column=0, columnspan=2, pady=10, sticky="ew")
        self.send_button = ttk.Button(actions, text="Gui POST", command=self.send_post)
        self.send_button.pack(side=tk.LEFT)
        ttk.Button(actions, text="Xoa ket qua", command=self.clear_output).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(actions, textvariable=self.status).pack(side=tk.RIGHT)

        output_frame = ttk.LabelFrame(container, text="Request / Response", padding=8)
        output_frame.grid(row=6, column=0, columnspan=2, sticky="nsew")
        output_frame.rowconfigure(0, weight=1)
        output_frame.columnconfigure(0, weight=1)
        self.output = tk.Text(output_frame, wrap=tk.NONE, font=("Consolas", 10))
        self.output.grid(row=0, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(output_frame, orient=tk.VERTICAL, command=self.output.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(output_frame, orient=tk.HORIZONTAL, command=self.output.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.output.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

    @staticmethod
    def _add_entry(
        parent: tk.Widget,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, padx=(0, 8), pady=4, sticky="w")
        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=column + 1,
            columnspan=columnspan,
            padx=(0, 12),
            pady=4,
            sticky="ew",
        )

    def _on_endpoint_changed(self) -> None:
        if self.endpoint.get() == INSPECT_ENDPOINT:
            self.inspect_options.grid()
        else:
            self.inspect_options.grid_remove()

    def _gateway_url(self, endpoint: str) -> str:
        return f"{self.base_url.get().rstrip('/')}{endpoint}"

    def send_health_check(self) -> None:
        self._start_request("GET", "/", None)

    def send_post(self) -> None:
        endpoint = self.endpoint.get()
        try:
            x, y = parse_coordinates(self.x_value.get(), self.y_value.get())
            if x > 250 or y > 250:
                should_continue = messagebox.askyesno(
                    "Canh bao toa do lon",
                    (
                        f"Gia tri X/Y hien tai la X={x:g}, Y={y:g}.\n"
                        "Mot trong hai gia tri vuot qua nguong 250.\n"
                        "Ban co muon tiep tuc gui request khong?"
                    ),
                )
                if not should_continue:
                    self.status.set("Da huy gui request")
                    return

            payload = build_payload(
                endpoint=endpoint,
                x_text=self.x_value.get(),
                y_text=self.y_value.get(),
                board_id_text=self.board_id.get(),
                defect_id_text=self.defect_id.get(),
                move_timeout_text=self.move_timeout.get(),
                ai_threshold_text=self.ai_threshold.get(),
                ai_api_url=self.ai_api_url.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Du lieu khong hop le", str(exc))
            return
        self._start_request("POST", endpoint, payload)

    def _start_request(self, method: str, endpoint: str, payload: dict | None) -> None:
        url = self._gateway_url(endpoint)
        forward_url = None
        if (
            method == "POST"
            and payload is not None
            and self.forward_enabled.get()
            and endpoint in (MOVE_ENDPOINT, INSPECT_ENDPOINT)
        ):
            forward_url = self.forward_url.get().strip() or None

        self._set_busy(True)
        self._append_output(f"\n{'=' * 72}\n{method} {url}\n")
        if payload is not None:
            self._append_output("Request:\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

        threading.Thread(
            target=self._request_worker,
            args=(method, url, payload, endpoint, forward_url),
            daemon=True,
        ).start()

    def _request_worker(
        self,
        method: str,
        url: str,
        payload: dict | None,
        endpoint: str,
        forward_url: str | None,
    ) -> None:
        started = time.perf_counter()
        try:
            status_code, response_body = send_json(method, url, payload)
            elapsed = time.perf_counter() - started
            message = f"\nHTTP {status_code} ({elapsed:.2f}s)\nResponse:\n{pretty_json(response_body)}\n"

            if (
                forward_url
                and payload is not None
                and status_code == 200
                and response_reports_success(response_body)
            ):
                message += (
                    f"\n[Forward] Da bo qua khoi request chinh. Se gui nen sang {forward_url}.\n"
                )
                self._start_forward_request(endpoint, payload, forward_url)

            self.after(0, self._finish_request, message, f"Hoan tat: HTTP {status_code}")
        except error.URLError as exc:
            elapsed = time.perf_counter() - started
            message = f"\nLoi ket noi ({elapsed:.2f}s): {exc.reason}\n"
            self.after(0, self._finish_request, message, "Khong the ket noi gateway")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            message = f"\nLoi ({elapsed:.2f}s): {exc}\n"
            self.after(0, self._finish_request, message, "Request that bai")

    @staticmethod
    def _forward_position(endpoint: str, payload: dict, forward_url: str) -> str:
        x, y = extract_sent_xy(endpoint, payload)
        if x is None or y is None:
            return "\n[Forward] Bo qua: khong tim thay toa do X/Y trong payload.\n"

        try:
            status_code, body = send_json("POST", forward_url, {"x": x, "y": y}, timeout=5)
            return (
                f"\n[Forward] POST {forward_url} (X={x}, Y={y}) -> HTTP {status_code}\n"
                f"{pretty_json(body)}\n"
            )
        except error.URLError as exc:
            return (
                f"\n[Forward] KHONG gui duoc vi tri sang app do khoang cach: {exc.reason}\n"
                f"          Kiem tra app do khoang cach da chay chua ({forward_url}).\n"
            )
        except Exception as exc:
            return f"\n[Forward] Loi gui vi tri: {exc}\n"

    def _start_forward_request(self, endpoint: str, payload: dict, forward_url: str) -> None:
        threading.Thread(
            target=self._forward_worker,
            args=(endpoint, payload, forward_url),
            daemon=True,
        ).start()

    def _forward_worker(self, endpoint: str, payload: dict, forward_url: str) -> None:
        message = self._forward_position(endpoint, payload, forward_url)
        self.after(0, self._append_output, message)

    def _finish_request(self, message: str, status: str) -> None:
        self._append_output(message)
        self.status.set(status)
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        state = tk.DISABLED if busy else tk.NORMAL
        self.send_button.configure(state=state)
        self.health_button.configure(state=state)
        if busy:
            self.status.set("Dang gui request...")

    def _append_output(self, text: str) -> None:
        self.output.insert(tk.END, text)
        self.output.see(tk.END)

    def clear_output(self) -> None:
        self.output.delete("1.0", tk.END)


class BoardOffsetTab(ttk.Frame):
    def __init__(self, parent: tk.Widget, gateway_tab: GatewayTab) -> None:
        super().__init__(parent, padding=12)
        self.gateway_tab = gateway_tab

        self.coeffs: dict | None = None
        self.R: np.ndarray | None = None
        self.t: np.ndarray | None = None
        self.measured_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}

        self.anchor_mode = tk.IntVar(value=3)
        self.calib_status = tk.StringVar(value="Chua nap ma tran calib.")
        self.offset_status = tk.StringVar(value="Chua co du lieu bu lech.")
        self.board_x = tk.StringVar(value="0")
        self.board_y = tk.StringVar(value="0")
        self.board_id = tk.StringVar()
        self.defect_id = tk.StringVar()
        self.preview_text = tk.StringVar(value="-")

        self._build_ui()
        self._load_calibration()
        self._load_saved_offset(startup=True)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(6, weight=1)

        calib_frame = ttk.LabelFrame(self, text="Calib goc", padding=10)
        calib_frame.grid(row=0, column=0, sticky="ew")
        calib_frame.columnconfigure(1, weight=1)
        ttk.Label(calib_frame, text="JSON").grid(row=0, column=0, sticky="w")
        ttk.Label(calib_frame, text=CALIB_JSON_PATH).grid(row=0, column=1, sticky="w")
        ttk.Button(calib_frame, text="Nap lai", command=self._load_calibration).grid(
            row=0, column=2, padx=(8, 0)
        )
        ttk.Label(calib_frame, textvariable=self.calib_status).grid(
            row=1,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(6, 0),
        )

        mode_frame = ttk.LabelFrame(self, text="Che do diem moc", padding=10)
        mode_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Radiobutton(
            mode_frame,
            text="2 diem moc (A, C)",
            variable=self.anchor_mode,
            value=2,
            command=self._rebuild_anchor_inputs,
        ).pack(anchor="w")
        ttk.Radiobutton(
            mode_frame,
            text="3 diem moc (A, C, D)",
            variable=self.anchor_mode,
            value=3,
            command=self._rebuild_anchor_inputs,
        ).pack(anchor="w")

        self.anchor_frame = ttk.LabelFrame(
            self,
            text="Nhap toa do PLC do duoc tai cac diem moc board moi",
            padding=10,
        )
        self.anchor_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        action_frame = ttk.Frame(self)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(action_frame, text="Tinh bu lech", command=self._compute_offset).pack(side=tk.LEFT)
        ttk.Button(action_frame, text="Nap offset da luu", command=self._load_saved_offset).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )

        result_frame = ttk.LabelFrame(self, text="Ket qua bu lech", padding=10)
        result_frame.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(result_frame, textvariable=self.offset_status, justify=tk.LEFT).pack(anchor="w")

        move_frame = ttk.LabelFrame(
            self,
            text="Nhap vi tri tren board va gui PLC sau khi da bu lech",
            padding=10,
        )
        move_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        move_frame.columnconfigure(1, weight=1)
        move_frame.columnconfigure(3, weight=1)
        self._add_entry(move_frame, "Board X (mm)", self.board_x, 0, 0)
        self._add_entry(move_frame, "Board Y (mm)", self.board_y, 0, 2)
        self._add_entry(move_frame, "Board ID", self.board_id, 1, 0)
        self._add_entry(move_frame, "Defect ID", self.defect_id, 1, 2)

        move_buttons = ttk.Frame(move_frame)
        move_buttons.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        ttk.Button(move_buttons, text="Xem truoc PLC", command=self._preview_move).pack(side=tk.LEFT)
        ttk.Button(move_buttons, text="Nap vao tab cu", command=self._copy_to_gateway_tab).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )
        ttk.Button(move_buttons, text="Gui PLC /api/plc/move", command=self._send_move).pack(
            side=tk.LEFT,
            padx=(8, 0),
        )
        ttk.Label(move_frame, textvariable=self.preview_text, justify=tk.LEFT).grid(
            row=3,
            column=0,
            columnspan=4,
            sticky="w",
            pady=(8, 0),
        )

        log_frame = ttk.LabelFrame(self, text="Nhat ky tab bu lech", padding=8)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=(10, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap=tk.NONE, font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

        self._rebuild_anchor_inputs()

    @staticmethod
    def _add_entry(
        parent: tk.Widget,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, padx=(0, 8), pady=4, sticky="w")
        ttk.Entry(parent, textvariable=variable).grid(
            row=row,
            column=column + 1,
            columnspan=columnspan,
            padx=(0, 12),
            pady=4,
            sticky="ew",
        )

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text)
        self.log.see(tk.END)

    def _load_calibration(self) -> None:
        try:
            self.coeffs = load_calibration_matrix(CALIB_JSON_PATH)
            diagnostics = self.coeffs.get("_diagnostics", {})
            n_points = diagnostics.get("n_points", "?")
            rms = diagnostics.get("rms_residual_mm")
            if rms is None:
                self.calib_status.set(f"Da nap calib goc thanh cong ({n_points} diem).")
            else:
                self.calib_status.set(
                    f"Da nap calib goc thanh cong ({n_points} diem, RMS={float(rms):.4f} mm)."
                )
        except Exception as exc:
            self.coeffs = None
            self.calib_status.set(f"Loi nap calib: {exc}")
        self._rebuild_anchor_inputs()

    def _anchor_names(self) -> list[str]:
        return ANCHOR_SETS[self.anchor_mode.get()]

    def _rebuild_anchor_inputs(self) -> None:
        for child in self.anchor_frame.winfo_children():
            child.destroy()

        headers = ["Diem", "Board (mm)", "PLC ky vong", "PLC X do", "PLC Y do"]
        for index, text in enumerate(headers):
            ttk.Label(self.anchor_frame, text=text, font=("", 9, "bold")).grid(
                row=0,
                column=index,
                padx=6,
                pady=(0, 4),
                sticky="w",
            )

        self.measured_vars = {}
        for row_index, name in enumerate(self._anchor_names(), start=1):
            board_x, board_y = ANCHOR_POINTS_POOL[name]
            ttk.Label(self.anchor_frame, text=name).grid(row=row_index, column=0, padx=6, pady=2, sticky="w")
            ttk.Label(self.anchor_frame, text=f"({board_x:.3f}, {board_y:.3f})").grid(
                row=row_index,
                column=1,
                padx=6,
                pady=2,
                sticky="w",
            )

            expected_text = "(chua co calib)"
            if self.coeffs is not None:
                plc_x, plc_y = board_to_plc(board_x, board_y, self.coeffs)
                expected_text = f"({plc_x:.3f}, {plc_y:.3f})"
            ttk.Label(self.anchor_frame, text=expected_text).grid(
                row=row_index,
                column=2,
                padx=6,
                pady=2,
                sticky="w",
            )

            x_var = tk.StringVar()
            y_var = tk.StringVar()
            ttk.Entry(self.anchor_frame, textvariable=x_var, width=12).grid(
                row=row_index,
                column=3,
                padx=6,
                pady=2,
                sticky="ew",
            )
            ttk.Entry(self.anchor_frame, textvariable=y_var, width=12).grid(
                row=row_index,
                column=4,
                padx=6,
                pady=2,
                sticky="ew",
            )
            self.measured_vars[name] = (x_var, y_var)

    def _compute_offset(self) -> None:
        if self.coeffs is None:
            messagebox.showerror("Chua co calib", "Vui long tao calib goc truoc khi tinh bu lech.")
            return

        expected_points = []
        measured_points = []

        try:
            for name in self._anchor_names():
                board_x, board_y = ANCHOR_POINTS_POOL[name]
                expected_points.append(board_to_plc(board_x, board_y, self.coeffs))
                x_var, y_var = self.measured_vars[name]
                measured_points.append((float(x_var.get().strip()), float(y_var.get().strip())))
        except ValueError:
            messagebox.showerror(
                "Du lieu khong hop le",
                "Vui long nhap day du toa do PLC do duoc cho tat ca diem moc.",
            )
            return

        try:
            R, t, theta, rms_error, max_error, residuals = kabsch_2d(expected_points, measured_points)
        except Exception as exc:
            messagebox.showerror("Loi tinh bu lech", str(exc))
            return

        self.R = R
        self.t = t

        lines = [
            f"Goc lech board: {math.degrees(theta):+.4f} do",
            f"Bu dich X:       {t[0]:+.4f} mm",
            f"Bu dich Y:       {t[1]:+.4f} mm",
            f"RMS residual:    {rms_error:.4f} mm",
            f"Max residual:    {max_error:.4f} mm",
        ]
        for name, residual in zip(self._anchor_names(), residuals):
            flag = ""
            if residual > 2 * rms_error and residual > 0.02:
                flag = "  <-- kiem tra lai diem nay"
            lines.append(f"{name}: {residual:.4f} mm{flag}")
        self.offset_status.set("\n".join(lines))

        offset_data = {
            "anchor_mode": len(self._anchor_names()),
            "theta_deg": math.degrees(theta),
            "tx": float(t[0]),
            "ty": float(t[1]),
            "rms_error_mm": float(rms_error),
            "max_error_mm": float(max_error),
            "anchor_points_board": [(name, ANCHOR_POINTS_POOL[name]) for name in self._anchor_names()],
            "anchor_points_expected_plc": expected_points,
            "anchor_points_measured_plc": measured_points,
            "residuals_mm": residuals.tolist(),
        }
        try:
            with open(OFFSET_JSON_PATH, "w", encoding="utf-8") as file:
                json.dump(offset_data, file, indent=4, ensure_ascii=False)
            self._append_log(f"\nDa luu offset vao {OFFSET_JSON_PATH}\n")
        except Exception as exc:
            self._append_log(f"\nKhong luu duoc offset: {exc}\n")

    def _load_saved_offset(self, startup: bool = False) -> None:
        if not os.path.exists(OFFSET_JSON_PATH):
            if not startup:
                messagebox.showinfo("Chua co offset", f"Khong tim thay file {OFFSET_JSON_PATH}")
            return

        try:
            with open(OFFSET_JSON_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
            self.R = rotation_matrix(math.radians(float(data["theta_deg"])))
            self.t = np.array([float(data["tx"]), float(data["ty"])], dtype=float)
            self.offset_status.set(
                "\n".join(
                    [
                        f"Da nap offset tu file: {OFFSET_JSON_PATH}",
                        f"Goc lech board: {float(data['theta_deg']):+.4f} do",
                        f"Bu dich X:       {float(data['tx']):+.4f} mm",
                        f"Bu dich Y:       {float(data['ty']):+.4f} mm",
                        f"RMS residual:    {float(data.get('rms_error_mm', 0.0)):.4f} mm",
                        f"Max residual:    {float(data.get('max_error_mm', 0.0)):.4f} mm",
                    ]
                )
            )
            if not startup:
                self._append_log(f"\nDa nap offset tu {OFFSET_JSON_PATH}\n")
        except Exception as exc:
            if startup:
                return
            messagebox.showerror("Loi doc offset", str(exc))

    def _compute_corrected_plc(self) -> tuple[float, float, tuple[float, float], tuple[float, float]]:
        if self.coeffs is None:
            raise RuntimeError("Chua nap ma tran calib goc.")
        if self.R is None or self.t is None:
            raise RuntimeError("Chua co du lieu bu lech. Hay tinh hoac nap offset truoc.")

        board_x, board_y = parse_coordinates(self.board_x.get(), self.board_y.get())
        nominal = board_to_plc(board_x, board_y, self.coeffs)
        corrected = apply_rigid_offset(nominal[0], nominal[1], self.R, self.t)
        return board_x, board_y, nominal, corrected

    def _preview_move(self) -> None:
        try:
            board_x, board_y, nominal, corrected = self._compute_corrected_plc()
        except Exception as exc:
            messagebox.showerror("Khong the tinh toa do", str(exc))
            return

        self.preview_text.set(
            f"Board ({board_x:g}, {board_y:g})\n"
            f"PLC chua bu: ({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"PLC da bu:   X={corrected[0]:.3f}, Y={corrected[1]:.3f}"
        )

    def _copy_to_gateway_tab(self) -> None:
        try:
            _, _, _, corrected = self._compute_corrected_plc()
        except Exception as exc:
            messagebox.showerror("Khong the nap toa do", str(exc))
            return

        self.gateway_tab.endpoint.set(MOVE_ENDPOINT)
        self.gateway_tab._on_endpoint_changed()
        self.gateway_tab.x_value.set(f"{corrected[0]:.3f}")
        self.gateway_tab.y_value.set(f"{corrected[1]:.3f}")
        self.gateway_tab.board_id.set(self.board_id.get())
        self.gateway_tab.defect_id.set(self.defect_id.get())
        self._append_log(
            f"\nDa nap vao tab cu: X={corrected[0]:.3f}, Y={corrected[1]:.3f}\n"
        )

    def _send_move(self) -> None:
        try:
            board_x, board_y, nominal, corrected = self._compute_corrected_plc()
        except Exception as exc:
            messagebox.showerror("Khong the gui PLC", str(exc))
            return

        self.preview_text.set(
            f"Board ({board_x:g}, {board_y:g})\n"
            f"PLC chua bu: ({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"PLC da bu:   X={corrected[0]:.3f}, Y={corrected[1]:.3f}"
        )

        if abs(corrected[0]) > 250 or abs(corrected[1]) > 250:
            should_continue = messagebox.askyesno(
                "Canh bao toa do lon",
                (
                    f"Toa do PLC da bu la X={corrected[0]:.3f}, Y={corrected[1]:.3f}.\n"
                    "Mot trong hai gia tri vuot qua nguong 250.\n"
                    "Ban co muon tiep tuc gui request khong?"
                ),
            )
            if not should_continue:
                return

        try:
            payload = build_payload(
                endpoint=MOVE_ENDPOINT,
                x_text=str(corrected[0]),
                y_text=str(corrected[1]),
                board_id_text=self.board_id.get(),
                defect_id_text=self.defect_id.get(),
            )
        except ValueError as exc:
            messagebox.showerror("Du lieu khong hop le", str(exc))
            return

        url = f"{self.gateway_tab.base_url.get().rstrip('/')}{MOVE_ENDPOINT}"
        forward_url = None
        if self.gateway_tab.forward_enabled.get():
            forward_url = self.gateway_tab.forward_url.get().strip() or None

        self._append_log(
            f"\n{'=' * 72}\n"
            f"Board target=({board_x:g}, {board_y:g})\n"
            f"PLC nominal=({nominal[0]:.3f}, {nominal[1]:.3f})\n"
            f"POST {url}\n"
            f"Request:\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        )

        threading.Thread(
            target=self._send_move_worker,
            args=(url, payload, forward_url),
            daemon=True,
        ).start()

    def _send_move_worker(self, url: str, payload: dict, forward_url: str | None) -> None:
        started = time.perf_counter()
        try:
            status_code, response_body = send_json("POST", url, payload)
            elapsed = time.perf_counter() - started
            message = f"\nHTTP {status_code} ({elapsed:.2f}s)\nResponse:\n{pretty_json(response_body)}\n"

            if forward_url and status_code == 200 and response_reports_success(response_body):
                message += (
                    f"\n[Forward] Da bo qua khoi request chinh. Se gui nen sang {forward_url}.\n"
                )
                self._start_background_forward(payload, forward_url)

            self.after(0, self._append_log, message)
        except error.URLError as exc:
            elapsed = time.perf_counter() - started
            self.after(0, self._append_log, f"\nLoi ket noi ({elapsed:.2f}s): {exc.reason}\n")
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.after(0, self._append_log, f"\nLoi ({elapsed:.2f}s): {exc}\n")

    def _start_background_forward(self, payload: dict, forward_url: str) -> None:
        threading.Thread(
            target=self._forward_move_worker,
            args=(payload, forward_url),
            daemon=True,
        ).start()

    def _forward_move_worker(self, payload: dict, forward_url: str) -> None:
        x, y = extract_sent_xy(MOVE_ENDPOINT, payload)
        if x is None or y is None:
            self.after(0, self._append_log, "\n[Forward] Bo qua: khong tim thay toa do X/Y trong payload.\n")
            return

        try:
            forward_status, forward_body = send_json(
                "POST",
                forward_url,
                {"x": x, "y": y},
                timeout=2,
            )
            message = (
                f"\n[Forward] POST {forward_url} (X={x}, Y={y}) -> HTTP {forward_status}\n"
                f"{pretty_json(forward_body)}\n"
            )
        except Exception as exc:
            message = (
                "\n[Forward] Khong gui duoc vi tri sang app do khoang cach, "
                "nhung PLC da duoc di chuyen thanh cong.\n"
                f"Chi tiet: {exc}\n"
            )
        self.after(0, self._append_log, message)


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PLC Gateway Tester v2 + Board Offset")
        self.root.geometry("1080x920")
        self.root.minsize(900, 720)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True)

        self.gateway_tab = GatewayTab(notebook)
        notebook.add(self.gateway_tab, text="PLC Gateway Tester")

        self.board_offset_tab = BoardOffsetTab(notebook, self.gateway_tab)
        notebook.add(self.board_offset_tab, text="Bu lech Board")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
