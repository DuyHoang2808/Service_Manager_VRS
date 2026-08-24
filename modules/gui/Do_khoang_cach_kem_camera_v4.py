"""
Do_khoang_cach_kem_camera_v4.py
Bản v3 + HTTP API để nhận vị trí PLC tự động (không cần nhập tay).

Chạy:
    python Do_khoang_cach_kem_camera_v4.py
    (cần file Do_khoang_cach_kem_camera_v3.py nằm cùng thư mục)

API:
    POST http://<ip-máy-này>:8090/api/plc-position
         Body JSON: {"x": 123.456, "y": 78.9}
         -> Cập nhật ngay ô "PLC X" / "PLC Y" trên giao diện,
            mọi kết quả đo được tính lại tự động.

    GET  http://<ip-máy-này>:8090/api/plc-position
         -> Trả về vị trí PLC hiện tại đang hiển thị trên GUI.

Cập nhật GUI từ HTTP thread được chuyển qua Qt Signal nên thread-safe.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

# Cho phép chạy từ thư mục bất kỳ
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from Do_khoang_cach_kem_camera_v3 import MainWindow  # noqa: E402

API_HOST = "0.0.0.0"   # nhận từ mọi máy trong mạng; đổi thành "127.0.0.1" nếu chỉ dùng nội bộ
API_PORT = 8090
API_PATH = "/api/plc-position"


class PlcPositionBridge(QObject):
    """Cầu nối HTTP thread -> GUI thread + snapshot vị trí hiện tại."""

    position_received = Signal(float, float)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._current = {"x": 0.0, "y": 0.0}

    def set_current(self, x: float, y: float) -> None:
        with self._lock:
            self._current = {"x": x, "y": y}

    def get_current(self) -> dict:
        with self._lock:
            return dict(self._current)


def make_api_handler(bridge: PlcPositionBridge):
    """Tạo handler class gắn với bridge cụ thể."""

    class ApiHandler(BaseHTTPRequestHandler):
        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _clean_path(self) -> str:
            return self.path.split("?")[0].rstrip("/")

        def do_GET(self):
            path = self._clean_path()
            if path == "":
                self._reply(200, {
                    "service": "Do khoang cach - PLC position API",
                    "status": "running",
                    "endpoint": API_PATH,
                })
            elif path == API_PATH:
                self._reply(200, {"success": True, **bridge.get_current()})
            else:
                self._reply(404, {"success": False, "message": f"Khong ho tro GET {self.path}"})

        def do_POST(self):
            if self._clean_path() != API_PATH:
                self._reply(404, {"success": False, "message": f"Khong ho tro POST {self.path}"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                x = float(data["x"])
                y = float(data["y"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._reply(400, {
                    "success": False,
                    "message": f"Body phai la JSON co 'x' va 'y' dang so. Chi tiet: {exc}",
                })
                return

            bridge.position_received.emit(x, y)  # thread-safe -> GUI thread
            self._reply(200, {
                "success": True,
                "message": f"Da cap nhat PLC X={x}, Y={y}",
                "x": x,
                "y": y,
            })

        def log_message(self, fmt, *args):
            pass  # tránh spam console

    return ApiHandler


class MainWindowV4(MainWindow):
    """v3 + HTTP server nhận vị trí PLC."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(
            "Camera Click Measure v4 - nhan vi tri PLC qua API (port %d)" % API_PORT
        )

        self.bridge = PlcPositionBridge()
        self.bridge.position_received.connect(self.on_plc_position_received)

        # Giữ snapshot vị trí hiện tại (đồng bộ trên GUI thread) cho GET
        self.plc_x_spin.valueChanged.connect(self._sync_bridge_current)
        self.plc_y_spin.valueChanged.connect(self._sync_bridge_current)
        self._sync_bridge_current()

        self.api_server = None
        self.api_thread = None
        self.start_api_server()

    def _sync_bridge_current(self, *_args) -> None:
        self.bridge.set_current(self.plc_x_spin.value(), self.plc_y_spin.value())

    def start_api_server(self) -> None:
        try:
            self.api_server = ThreadingHTTPServer(
                (API_HOST, API_PORT), make_api_handler(self.bridge)
            )
        except OSError as exc:
            self.api_server = None
            self.status_label.setText(
                f"Camera: KHONG mo duoc API port {API_PORT} ({exc}). "
                f"Kiem tra port co bi chiem khong."
            )
            return

        self.api_thread = threading.Thread(
            target=self.api_server.serve_forever, name="PlcPositionApi", daemon=True
        )
        self.api_thread.start()
        self.status_label.setText(
            f"Camera: API nhan vi tri PLC dang chay: http://localhost:{API_PORT}{API_PATH}"
        )

    def on_plc_position_received(self, x: float, y: float) -> None:
        """Chạy trên GUI thread. setValue tự kích hoạt tính lại kết quả đo."""
        self.plc_x_spin.setValue(x)
        self.plc_y_spin.setValue(y)
        self.status_label.setText(
            f"Camera: Da nhan vi tri PLC tu API: X={x:.3f}, Y={y:.3f} mm"
        )

    def closeEvent(self, event):
        if self.api_server is not None:
            self.api_server.shutdown()
            self.api_server.server_close()
            self.api_server = None
        super().closeEvent(event)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindowV4()
    window.show()
    sys.exit(app.exec())
