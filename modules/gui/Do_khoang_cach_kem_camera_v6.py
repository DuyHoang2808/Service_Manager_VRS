"""
Do_khoang_cach_kem_camera_v6.py
--------------------------------------------------------------------
v5 (Do khoang cach + Camera / PLC Gateway Tester / Bu lech Board) +
CHUC NANG MOI: "Chup anh hien tai" - chup lai frame moi nhat cua luong
camera dang hien thi tren tab "Do khoang cach + Camera", hien popup
(hop thoai luu file chuan cua he dieu hanh) de nguoi dung tu dat TEN
FILE va chon DUONG DAN luu, roi luu ra file anh (PNG/JPG/BMP).

Khong sua Do_khoang_cach_kem_camera_v5.py (va cac file goc ma v5 da
dung: v3, v4, Funs/test_plc_gateway_gui_v2_with_board_offset.py,
bu_lech_board.py) - chi import lai MergedMainWindow tu v5 va them 1
nut "Chup anh..." vao nhom "Ket noi Camera" (canh nut Start/Stop/Clear),
theo dung cach v5 da tung them nut vao UI co san cua v3/v4 (tim lai
container widget qua 1 widget da co san, roi gan them vao layout).

Chay:
    python Do_khoang_cach_kem_camera_v6.py

(PLC Gateway server van phai chay rieng: python run_plc_gateway.py)
"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from Do_khoang_cach_kem_camera_v5 import MergedMainWindow  # noqa: E402

DEFAULT_CAPTURE_DIR = os.path.join(THIS_DIR, "Captures")


class MergedMainWindowV6(MergedMainWindow):
    """MergedMainWindow (v5) nguyen ven + 1 nut "Chup anh..." de luu lai
    frame hien tai cua camera, co popup dat ten file va chon duong dan luu.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "VRS Control Center - Do khoang cach + PLC Gateway + Bu lech Board + Chup anh"
        )
        self._last_capture_dir = DEFAULT_CAPTURE_DIR
        self._add_capture_button()

    def _add_capture_button(self) -> None:
        """Them nut 'Chup anh...' vao ngay trong nhom 'Ket noi Camera' (cung
        cho voi Start/Stop/Clear camera) cua tab 'Do khoang cach + Camera'.
        self.start_btn la widget co san trong MainWindow (v3) - dung no de
        tim lai container/layout cha (camera_form: QFormLayout), tranh phai
        sua Do_khoang_cach_kem_camera_v3.py.
        """
        try:
            container_widget = self.start_btn.parentWidget()
            container_layout = container_widget.layout()
        except Exception:
            return

        self.capture_btn = QPushButton("Chup anh hien tai...")
        self.capture_btn.clicked.connect(self.capture_current_frame)
        container_layout.addRow(self.capture_btn)

    def capture_current_frame(self) -> None:
        """Lay frame moi nhat dang hien thi tren CameraView (self.view.qimage),
        hien popup luu file (chon ten + duong dan), roi luu ra file anh."""
        qimage = self.view.qimage
        if qimage is None or qimage.isNull():
            QMessageBox.warning(
                self,
                "Chua co anh",
                "Camera chua co frame nao de chup. Vui long bat camera truoc.",
            )
            return

        os.makedirs(self._last_capture_dir, exist_ok=True)
        default_name = f"capture_{time.strftime('%Y%m%d_%H%M%S')}.png"
        default_path = os.path.join(self._last_capture_dir, default_name)

        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Luu anh chup tu camera",
            default_path,
            "PNG (*.png);;JPEG (*.jpg *.jpeg);;Bitmap (*.bmp);;Tat ca (*.*)",
        )
        if not path:
            self.status_label.setText("Camera: Da huy chup anh")
            return

        if not os.path.splitext(path)[1]:
            path += ".png"

        if qimage.save(path):
            self._last_capture_dir = os.path.dirname(path) or self._last_capture_dir
            self.status_label.setText(f"Camera: Da luu anh chup vao {path}")
        else:
            QMessageBox.critical(
                self,
                "Loi luu anh",
                f"Khong luu duoc anh vao:\n{path}",
            )


def main() -> None:
    app = QApplication(sys.argv)
    window = MergedMainWindowV6()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
