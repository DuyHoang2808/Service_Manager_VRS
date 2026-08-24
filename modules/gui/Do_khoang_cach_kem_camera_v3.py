import math
import sys
import time
from pathlib import Path

import cv2

from PySide6.QtCore import QPoint, QThread, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QFrame,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

CAMERA_CONTROL_DIR = Path(__file__).resolve().parents[1] / "Giao_dien_camera"
if str(CAMERA_CONTROL_DIR) not in sys.path:
    sys.path.insert(0, str(CAMERA_CONTROL_DIR))

try:
    import serial
    import serial.tools.list_ports
    SERIAL_IMPORT_ERROR = None
except ImportError as exc:
    serial = None
    SERIAL_IMPORT_ERROR = str(exc)

try:
    from camera import SonyCamera
    SONY_CAMERA_IMPORT_ERROR = None
except ImportError as exc:
    SonyCamera = None
    SONY_CAMERA_IMPORT_ERROR = str(exc)


class LocalSonyZoomController:
    VISCA_HEADER = 0x81
    VISCA_TERMINATOR = 0xFF

    def __init__(self, port="COM3", baudrate=9600):
        self.port = port
        self.baudrate = baudrate
        self.serial = None
        self.is_connected = False

    def disconnect(self):
        if self.serial is not None:
            try:
                self.serial.close()
            finally:
                self.serial = None
                self.is_connected = False

    def _send_command(self, cmd):
        if not self.is_connected or self.serial is None:
            return None

        try:
            self.serial.reset_input_buffer()
            self.serial.write(cmd)
            self.serial.flush()

            response = bytearray()
            start_time = time.time()
            is_inquiry = len(cmd) > 1 and cmd[1] == 0x09

            while time.time() - start_time < 0.5:
                if self.serial.in_waiting > 0:
                    chunk = self.serial.read(self.serial.in_waiting)
                    response.extend(chunk)

                    if response and response[-1] == 0xFF:
                        try:
                            last_start = response.rindex(0x90)
                            if len(response) > last_start + 1:
                                msg_type = response[last_start + 1]
                                if is_inquiry:
                                    if (msg_type & 0xF0) == 0x50:
                                        return bytes(response)
                                elif (msg_type & 0xF0) in (0x40, 0x50):
                                    return bytes(response)
                        except ValueError:
                            pass
                else:
                    time.sleep(0.005)

            return bytes(response)
        except Exception:
            return None

    def get_zoom_position(self):
        response = self._send_command(
            bytes([self.VISCA_HEADER, 0x09, 0x04, 0x47, self.VISCA_TERMINATOR])
        )
        if response:
            try:
                for i, byte in enumerate(response):
                    if (byte & 0xF0) == 0x50:
                        if i + 5 < len(response):
                            p = response[i + 1] & 0x0F
                            q = response[i + 2] & 0x0F
                            r = response[i + 3] & 0x0F
                            s = response[i + 4] & 0x0F
                            return (p << 12) | (q << 8) | (r << 4) | s
                        break
            except (ValueError, IndexError):
                pass
        return None

    def zoom_direct(self, position):
        position = max(0, min(0x4000, position))
        p = (position >> 12) & 0x0F
        q = (position >> 8) & 0x0F
        r = (position >> 4) & 0x0F
        s = position & 0x0F
        response = self._send_command(
            bytes([self.VISCA_HEADER, 0x01, 0x04, 0x47, p, q, r, s, self.VISCA_TERMINATOR])
        )
        return response is not None

    def zoom_stop(self):
        response = self._send_command(
            bytes([self.VISCA_HEADER, 0x01, 0x04, 0x07, 0x00, self.VISCA_TERMINATOR])
        )
        return response is not None


def zoom_pos_to_multiplier(zoom_pos):
    multiplier = 1.0 + (zoom_pos / 16384.0) * 29.0
    return min(30.0, multiplier)


class CameraThread(QThread):
    frame_ready = Signal(object)
    status_signal = Signal(str)

    def __init__(self, camera_source="0", frame_width=1920, frame_height=1080):
        super().__init__()
        self.camera_source = camera_source
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.running = False
        self.cap = None

    def parse_source(self):
        source = self.camera_source.strip()
        if source.isdigit():
            return int(source)
        return source

    def run(self):
        self.running = True
        source = self.parse_source()

        self.cap = cv2.VideoCapture(source)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)

        if not self.cap.isOpened():
            self.status_signal.emit("Khong mo duoc camera.")
            self.running = False
            return

        self.status_signal.emit("Camera dang chay.")

        while self.running:
            ret, frame = self.cap.read()

            if not ret or frame is None:
                if isinstance(source, str):
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = self.cap.read()
                    if ret and frame is not None:
                        self.frame_ready.emit(frame)
                        self.msleep(10)
                        continue

                self.status_signal.emit("Khong doc duoc frame tu camera.")
                self.msleep(100)
                continue
            frame = cv2.flip(frame, 1)
            self.frame_ready.emit(frame)
            self.msleep(10)

        if self.cap is not None:
            self.cap.release()
            self.cap = None

        self.status_signal.emit("Camera da dung.")

    def stop(self):
        self.running = False
        self.wait(1000)


class CameraView(QWidget):
    clicked_on_image = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.qimage = None
        self.frame_w = 0
        self.frame_h = 0
        self.click_x = None
        self.click_y = None
        self.dx_mm = None
        self.dy_mm = None
        self.distance_mm = None
        self.point1 = None
        self.point2 = None
        self.two_point_dx_mm = None
        self.two_point_dy_mm = None
        self.two_point_distance_mm = None
        self.grid_enabled = False
        self.grid_step_mm = None
        self.grid_mm_per_pixel_x = None
        self.grid_mm_per_pixel_y = None
        self.measure_zoom_enabled = True
        self.view_zoom = 1.0
        self.view_pan_x = 0.0
        self.view_pan_y = 0.0
        self.min_view_zoom = 1.0
        self.max_view_zoom = 8.0
        self.cursor_pos = None
        
        self.lock_click = False  
        self.is_two_point_mode = False  

        self.setMinimumSize(800, 500)
        self.setMouseTracking(True)

    def set_frame(self, frame_bgr):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb.shape
        bytes_per_line = channels * width

        self.qimage = QImage(
            rgb.data,
            width,
            height,
            bytes_per_line,
            QImage.Format_RGB888,
        ).copy()

        self.frame_w = width
        self.frame_h = height
        self.update()

    def set_click_result(self, click_x, click_y, dx_mm, dy_mm, distance_mm):
        self.click_x = click_x
        self.click_y = click_y
        self.dx_mm = dx_mm
        self.dy_mm = dy_mm
        self.distance_mm = distance_mm
        self.update()

    def set_two_point_result(self, point1, point2, dx_mm, dy_mm, distance_mm):
        self.point1 = point1
        self.point2 = point2
        self.two_point_dx_mm = dx_mm
        self.two_point_dy_mm = dy_mm
        self.two_point_distance_mm = distance_mm
        self.update()

    def set_grid_overlay(self, enabled, step_mm=None, mm_per_pixel_x=None, mm_per_pixel_y=None):
        self.grid_enabled = enabled
        self.grid_step_mm = step_mm
        self.grid_mm_per_pixel_x = mm_per_pixel_x
        self.grid_mm_per_pixel_y = mm_per_pixel_y
        self.update()

    def clear_two_points(self):
        self.point1 = None
        self.point2 = None
        self.two_point_dx_mm = None
        self.two_point_dy_mm = None
        self.two_point_distance_mm = None
        self.update()

    def set_measure_zoom_enabled(self, enabled):
        self.measure_zoom_enabled = enabled
        if not enabled:
            self.reset_view_zoom()

    def reset_view_zoom(self):
        self.view_zoom = 1.0
        self.view_pan_x = 0.0
        self.view_pan_y = 0.0
        self.update()

    def clear_click(self):
        self.click_x = None
        self.click_y = None
        self.dx_mm = None
        self.dy_mm = None
        self.distance_mm = None
        self.clear_two_points()
        self.update()

    def get_base_image_display_rect(self):
        if self.qimage is None:
            return QRect()

        widget_w = self.width()
        widget_h = self.height()
        img_w = self.qimage.width()
        img_h = self.qimage.height()

        if img_w <= 0 or img_h <= 0:
            return QRect()

        scale = min(widget_w / img_w, widget_h / img_h)
        display_w = int(img_w * scale)
        display_h = int(img_h * scale)
        pos_x = int((widget_w - display_w) / 2)
        pos_y = int((widget_h - display_h) / 2)
        return QRect(pos_x, pos_y, display_w, display_h)

    def get_image_display_rect(self):
        base_rect = self.get_base_image_display_rect()
        if base_rect.width() <= 0 or base_rect.height() <= 0:
            return QRect()

        display_w = int(base_rect.width() * self.view_zoom)
        display_h = int(base_rect.height() * self.view_zoom)
        self.clamp_view_pan(display_w, display_h)

        pos_x = int((self.width() - display_w) / 2 + self.view_pan_x)
        pos_y = int((self.height() - display_h) / 2 + self.view_pan_y)
        return QRect(pos_x, pos_y, display_w, display_h)

    def clamp_view_pan(self, display_w=None, display_h=None):
        if self.qimage is None:
            self.view_pan_x = 0.0
            self.view_pan_y = 0.0
            return

        if display_w is None or display_h is None:
            base_rect = self.get_base_image_display_rect()
            display_w = base_rect.width() * self.view_zoom
            display_h = base_rect.height() * self.view_zoom

        max_pan_x = max(0.0, (display_w - self.width()) / 2.0)
        max_pan_y = max(0.0, (display_h - self.height()) / 2.0)
        self.view_pan_x = max(-max_pan_x, min(max_pan_x, self.view_pan_x))
        self.view_pan_y = max(-max_pan_y, min(max_pan_y, self.view_pan_y))

    def image_to_view_point(self, img_x, img_y):
        rect = self.get_image_display_rect()
        if rect.width() <= 0 or rect.height() <= 0:
            return None

        view_x = rect.left() + img_x * rect.width() / self.frame_w
        view_y = rect.top() + img_y * rect.height() / self.frame_h
        return int(view_x), int(view_y)

    def mousePressEvent(self, event):
        if self.qimage is None:
            return

        if not self.is_two_point_mode and self.lock_click:
            return

        rect = self.get_image_display_rect()
        pos = event.position().toPoint()
        if not rect.contains(pos):
            return

        x_in_rect = pos.x() - rect.left()
        y_in_rect = pos.y() - rect.top()
        img_x = int(x_in_rect * self.frame_w / rect.width())
        img_y = int(y_in_rect * self.frame_h / rect.height())

        img_x = max(0, min(self.frame_w - 1, img_x))
        img_y = max(0, min(self.frame_h - 1, img_y))
        self.clicked_on_image.emit(img_x, img_y)

    def mouseMoveEvent(self, event):
        if self.qimage is None:
            self.cursor_pos = None
            self.update()
            return

        pos = event.position().toPoint()
        rect = self.get_image_display_rect()
        self.cursor_pos = QPoint(pos) if rect.contains(pos) else None
        self.update()

    def leaveEvent(self, event):
        self.cursor_pos = None
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event):
        if (
            self.qimage is None
            or not (event.modifiers() & Qt.ControlModifier)
        ):
            super().wheelEvent(event)
            return

        delta_y = event.angleDelta().y()
        if delta_y == 0:
            event.accept()
            return

        old_rect = self.get_image_display_rect()
        if old_rect.width() <= 0 or old_rect.height() <= 0:
            event.accept()
            return

        mouse_pos = event.position()
        if not old_rect.contains(mouse_pos.toPoint()):
            super().wheelEvent(event)
            return

        img_x = (mouse_pos.x() - old_rect.left()) * self.frame_w / old_rect.width()
        img_y = (mouse_pos.y() - old_rect.top()) * self.frame_h / old_rect.height()

        factor = 1.15 if delta_y > 0 else 1.0 / 1.15
        old_zoom = self.view_zoom
        self.view_zoom = max(
            self.min_view_zoom,
            min(self.max_view_zoom, self.view_zoom * factor),
        )

        if self.view_zoom == old_zoom:
            event.accept()
            return

        base_rect = self.get_base_image_display_rect()
        new_w = base_rect.width() * self.view_zoom
        new_h = base_rect.height() * self.view_zoom
        centered_left = (self.width() - new_w) / 2.0
        centered_top = (self.height() - new_h) / 2.0

        self.view_pan_x = mouse_pos.x() - img_x * new_w / self.frame_w - centered_left
        self.view_pan_y = mouse_pos.y() - img_y * new_h / self.frame_h - centered_top
        self.clamp_view_pan(new_w, new_h)
        self.update()
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        if self.qimage is None:
            painter.setPen(QColor(220, 220, 220))
            painter.setFont(QFont("Arial", 14))
            painter.drawText(self.rect(), Qt.AlignCenter, "Chua co hinh anh camera")
            return

        rect = self.get_image_display_rect()
        painter.drawImage(rect, self.qimage)

        center_x = rect.left() + rect.width() / 2
        center_y = rect.top() + rect.height() / 2

        painter.setPen(QPen(QColor(0, 255, 0), 1, Qt.DashLine))
        painter.drawLine(int(center_x), rect.top(), int(center_x), rect.bottom())
        painter.drawLine(rect.left(), int(center_y), rect.right(), int(center_y))

        painter.setPen(QPen(QColor(0, 255, 0), 2))
        painter.drawEllipse(int(center_x) - 5, int(center_y) - 5, 10, 10)
        painter.drawText(int(center_x) + 8, int(center_y) - 8, "CENTER")

        self.draw_measure_grid(painter, rect, center_x, center_y)
        self.draw_cursor_guides(painter, rect)

        if self.click_x is not None and self.click_y is not None:
            point = self.image_to_view_point(self.click_x, self.click_y)
            if point is not None:
                view_x, view_y = point
                painter.setPen(QPen(QColor(255, 0, 0), 2))
                painter.drawLine(int(center_x), int(center_y), view_x, view_y)
                painter.setPen(QPen(QColor(255, 0, 0), 3))
                painter.drawEllipse(view_x - 6, view_y - 6, 12, 12)

                painter.setPen(QPen(QColor(0, 0, 0), 2))
                painter.setFont(QFont("Arial", 11))
                info = (
                    f"Click: ({self.click_x}, {self.click_y}) px\n"
                    f"dx = {self.dx_mm:.3f} mm\n"
                    f"dy = {self.dy_mm:.3f} mm\n"
                    f"d = {self.distance_mm:.3f} mm"
                )
                if self.lock_click:
                    info += "\n[LOCKED]"
                painter.drawText(view_x + 10, view_y - 10, info)

        if self.point1 is not None:
            p1_view = self.image_to_view_point(self.point1[0], self.point1[1])
            if p1_view is not None:
                painter.setPen(QPen(QColor(0, 170, 255), 3))
                painter.drawEllipse(p1_view[0] - 6, p1_view[1] - 6, 12, 12)
                painter.setFont(QFont("Arial", 11))
                painter.setPen(QPen(QColor(0, 170, 255), 2))
                painter.drawText(p1_view[0] + 10, p1_view[1] + 18, f"P1 ({self.point1[0]}, {self.point1[1]})")

        if self.point2 is not None:
            p2_view = self.image_to_view_point(self.point2[0], self.point2[1])
            if p2_view is not None:
                painter.setPen(QPen(QColor(255, 170, 0), 3))
                painter.drawEllipse(p2_view[0] - 6, p2_view[1] - 6, 12, 12)
                painter.setFont(QFont("Arial", 11))
                painter.setPen(QPen(QColor(255, 170, 0), 2))
                painter.drawText(p2_view[0] + 10, p2_view[1] + 18, f"P2 ({self.point2[0]}, {self.point2[1]})")

        if self.point1 is not None and self.point2 is not None:
            p1_view = self.image_to_view_point(self.point1[0], self.point1[1])
            p2_view = self.image_to_view_point(self.point2[0], self.point2[1])
            if p1_view is not None and p2_view is not None:
                painter.setPen(QPen(QColor(0, 255, 128), 2))
                painter.drawLine(p1_view[0], p1_view[1], p2_view[0], p2_view[1])

                mid_x = int((p1_view[0] + p2_view[0]) / 2)
                mid_y = int((p1_view[1] + p2_view[1]) / 2)
                painter.setPen(QPen(QColor(0, 0, 0), 2))
                info = (
                    f"P1-P2\n"
                    f"dx = {self.two_point_dx_mm:.3f} mm\n"
                    f"dy = {self.two_point_dy_mm:.3f} mm\n"
                    f"d = {self.two_point_distance_mm:.3f} mm"
                )
                painter.drawText(mid_x + 10, mid_y - 10, info)

    def draw_measure_grid(self, painter, rect, center_x, center_y):
        if (
            not self.grid_enabled
            or self.grid_step_mm is None
            or self.grid_step_mm <= 0
            or self.grid_mm_per_pixel_x is None
            or self.grid_mm_per_pixel_y is None
            or self.grid_mm_per_pixel_x <= 0
            or self.grid_mm_per_pixel_y <= 0
        ):
            return

        step_x = self.grid_step_mm * rect.width() / (self.frame_w * self.grid_mm_per_pixel_x)
        step_y = self.grid_step_mm * rect.height() / (self.frame_h * self.grid_mm_per_pixel_y)
        if step_x < 4 or step_y < 4:
            return

        painter.setPen(QPen(QColor(255, 165, 0, 255), 1, Qt.DotLine))

        x = center_x
        while x <= rect.right():
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
            x += step_x

        x = center_x - step_x
        while x >= rect.left():
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
            x -= step_x

        y = center_y
        while y <= rect.bottom():
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
            y += step_y

        y = center_y - step_y
        while y >= rect.top():
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
            y -= step_y

    def draw_cursor_guides(self, painter, rect):
        if self.cursor_pos is None or not rect.contains(self.cursor_pos):
            return

        painter.save()
        painter.setClipRect(rect)
        painter.setPen(QPen(QColor(0, 0, 0), 1, Qt.DashLine))
        x = self.cursor_pos.x()
        y = self.cursor_pos.y()
        painter.drawLine(x, rect.top(), x, rect.bottom())
        painter.drawLine(rect.left(), y, rect.right(), y)
        painter.restore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camera Click Measure - Pixel to mm (v3: toa do thuc P1/P2)")
        self.resize(1350, 780)

        self.camera_thread = None
        self.sony_camera = None
        self.zoom_camera_class = SonyCamera or LocalSonyZoomController
        self.is_zoom_busy = False
        self.last_click_x = None
        self.last_click_y = None
        self.measure_two_points_check = None
        self.point1 = None
        self.point2 = None
        
        # Biến xác định mục tiêu click hiện tại: "P1" hoặc "P2"
        self.current_target_point = None 

        self.view = CameraView()
        self.view.clicked_on_image.connect(self.on_image_clicked)

        self.source_edit = QLineEdit("0")
        self.frame_width_spin = QSpinBox()
        self.frame_width_spin.setRange(1, 10000)
        self.frame_width_spin.setValue(1920)
        self.frame_width_spin.setSuffix(" px")

        self.frame_height_spin = QSpinBox()
        self.frame_height_spin.setRange(1, 10000)
        self.frame_height_spin.setValue(1080)
        self.frame_height_spin.setSuffix(" px")

        self.start_btn = QPushButton("Start Camera")
        self.stop_btn = QPushButton("Stop Camera")
        self.clear_btn = QPushButton("Clear Point")
        self.stop_btn.setEnabled(False)
        self.start_btn.setMinimumHeight(30)
        self.stop_btn.setMinimumHeight(30)
        self.clear_btn.setMinimumHeight(30)
        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #2e7d32; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #7f8c8d; color: #dcdcdc; }"
        )
        self.stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #7f8c8d; color: #dcdcdc; }"
        )

        self.measure_two_points_check = QCheckBox("Do khoang cach giua 2 diem bat ky")
        self.grid_from_sample_check = QCheckBox("Chia o theo khoang cach click")
        self.lock_sample_check = QCheckBox("Khoa diem lay mau (Lock Target)")

        # Thêm 2 nút bấm chọn điểm mốc P1 và P2
        self.btn_select_p1 = QPushButton("Click Chọn P1")
        self.btn_select_p2 = QPushButton("Click Chọn P2")
        self.btn_select_p1.setCheckable(True)
        self.btn_select_p2.setCheckable(True)
        self.btn_select_p1.setEnabled(False)
        self.btn_select_p2.setEnabled(False)
        self.btn_select_p1.setMinimumHeight(28)
        self.btn_select_p2.setMinimumHeight(28)
        p1_button_style = (
            "QPushButton { background-color: #eaf2ff; }"
            "QPushButton:checked { background-color: #00aaff; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #ececec; color: #a0a0a0; }"
        )
        p2_button_style = (
            "QPushButton { background-color: #fff4e5; }"
            "QPushButton:checked { background-color: #ffaa00; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #ececec; color: #a0a0a0; }"
        )
        self.btn_select_p1.setStyleSheet(p1_button_style)
        self.btn_select_p2.setStyleSheet(p2_button_style)

        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(1.0, 30.0)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setValue(1.0)
        self.zoom_spin.setSuffix(" x")

        self.fov_x_spin = QDoubleSpinBox()
        self.fov_x_spin.setRange(0.001, 10000.0)
        self.fov_x_spin.setDecimals(3)
        self.fov_x_spin.setValue(150.0)
        self.fov_x_spin.setSuffix(" mm")

        self.fov_y_spin = QDoubleSpinBox()
        self.fov_y_spin.setRange(0.001, 10000.0)
        self.fov_y_spin.setDecimals(3)
        self.fov_y_spin.setValue(100.0)
        self.fov_y_spin.setSuffix(" mm")

        self.plc_x_spin = QDoubleSpinBox()
        self.plc_x_spin.setRange(-100000.0, 100000.0)
        self.plc_x_spin.setDecimals(3)
        self.plc_x_spin.setValue(0.0)
        self.plc_x_spin.setSuffix(" mm")

        self.plc_y_spin = QDoubleSpinBox()
        self.plc_y_spin.setRange(-100000.0, 100000.0)
        self.plc_y_spin.setDecimals(3)
        self.plc_y_spin.setValue(0.0)
        self.plc_y_spin.setSuffix(" mm")

        self.invert_y_check = QCheckBox("Dao chieu Y khi quy doi sang toa do may")
        self.invert_y_check.setChecked(True)

        # Tọa độ thực (mm) của điểm P1 do người dùng nhập vào
        self.p1_real_x_spin = QDoubleSpinBox()
        self.p1_real_x_spin.setRange(-100000.0, 100000.0)
        self.p1_real_x_spin.setDecimals(3)
        self.p1_real_x_spin.setValue(0.0)
        self.p1_real_x_spin.setSuffix(" mm")

        self.p1_real_y_spin = QDoubleSpinBox()
        self.p1_real_y_spin.setRange(-100000.0, 100000.0)
        self.p1_real_y_spin.setDecimals(3)
        self.p1_real_y_spin.setValue(0.0)
        self.p1_real_y_spin.setSuffix(" mm")

        self.com_port_combo = QComboBox()
        self.com_port_combo.setEditable(True)
        self.com_port_combo.addItems(self.get_com_ports())
        self.refresh_ports_btn = QPushButton("Refresh COM")
        self.baud_combo = QComboBox()
        self.baud_combo.addItems(["9600", "19200", "38400", "115200"])
        self.baud_combo.setCurrentText("9600")

        self.connect_zoom_btn = QPushButton("Connect Zoom")
        self.disconnect_zoom_btn = QPushButton("Disconnect Zoom")
        self.read_zoom_btn = QPushButton("Read Zoom")
        self.zoom_in_btn = QPushButton("Zoom In +")
        self.zoom_out_btn = QPushButton("Zoom Out -")
        self.zoom_stop_btn = QPushButton("Zoom Stop")
        self.zoom_stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; }"
            "QPushButton:disabled { background-color: #ececec; color: #a0a0a0; }"
        )

        self.status_label = QLabel("Camera: Chua chay")
        self.image_size_label = QLabel("Anh: -")
        self.zoom_status_label = QLabel("Zoom: Chua ket noi")
        self.zoom_raw_label = QLabel("VISCA: -")
        self.zoom_value_label = QLabel("Zoom cam: -")
        self.click_pixel_label = QLabel("Click: -")
        self.camera_coord_label = QLabel("Tam: -")
        self.distance_label = QLabel("d tam: -")
        self.two_point_label = QLabel("P1-P2: -")
        self.p2_real_label = QLabel("P2 thuc: -")
        self.plc_move_label = QLabel("Di chuyen PLC: -")
        self.plc_move_label.setStyleSheet(
            "background-color: #fff8dc; color: #7a5c00; border: 1px solid #e0c34a;"
            " border-radius: 4px; padding: 4px;"
        )
        self.grid_label = QLabel("Luoi: -")
        self.machine_coord_label = QLabel("PLC: -")

        self.start_btn.clicked.connect(self.start_camera)
        self.stop_btn.clicked.connect(self.stop_camera)
        self.clear_btn.clicked.connect(self.clear_point)
        self.measure_two_points_check.stateChanged.connect(self.clear_two_point_measure)
        self.grid_from_sample_check.stateChanged.connect(self.update_grid_from_last_click)
        self.lock_sample_check.stateChanged.connect(self.on_lock_sample_changed)
        
        # Kết nối sự kiện nút bấm chọn P1/P2 mới
        self.btn_select_p1.clicked.connect(self.prepare_select_p1)
        self.btn_select_p2.clicked.connect(self.prepare_select_p2)

        self.connect_zoom_btn.clicked.connect(self.connect_zoom_camera)
        self.disconnect_zoom_btn.clicked.connect(self.disconnect_zoom_camera)
        self.read_zoom_btn.clicked.connect(self.refresh_zoom_value)
        self.refresh_ports_btn.clicked.connect(self.refresh_com_ports)
        self.zoom_in_btn.clicked.connect(lambda: self.step_zoom(0x200))
        self.zoom_out_btn.clicked.connect(lambda: self.step_zoom(-0x200))
        self.zoom_stop_btn.clicked.connect(self.stop_zoom_motion)

        self.zoom_spin.valueChanged.connect(self.recalculate_last_click)
        self.fov_x_spin.valueChanged.connect(self.recalculate_last_click)
        self.fov_y_spin.valueChanged.connect(self.recalculate_last_click)
        self.plc_x_spin.valueChanged.connect(self.recalculate_last_click)
        self.plc_y_spin.valueChanged.connect(self.recalculate_last_click)
        self.invert_y_check.stateChanged.connect(self.recalculate_last_click)
        self.p1_real_x_spin.valueChanged.connect(self.recalculate_last_click)
        self.p1_real_y_spin.valueChanged.connect(self.recalculate_last_click)

        self.zoom_timer = QTimer(self)
        self.zoom_timer.setInterval(1500)
        self.zoom_timer.timeout.connect(self.refresh_zoom_value)

        self.build_ui()
        self.setup_info_labels()
        self.update_zoom_controls_enabled(False)

    def setup_info_labels(self):
        info_labels = [
            self.status_label,
            self.image_size_label,
            self.zoom_status_label,
            self.zoom_raw_label,
            self.zoom_value_label,
            self.click_pixel_label,
            self.camera_coord_label,
            self.distance_label,
            self.two_point_label,
            self.p2_real_label,
            self.plc_move_label,
            self.grid_label,
            self.machine_coord_label,
        ]

        for label in info_labels:
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            label.setContentsMargins(0, 0, 0, 0)

        compact_font = QFont("Consolas", 9)
        for label in [
            self.click_pixel_label,
            self.camera_coord_label,
            self.distance_label,
            self.two_point_label,
            self.p2_real_label,
            self.grid_label,
            self.machine_coord_label,
            self.zoom_raw_label,
            self.zoom_value_label,
        ]:
            label.setFont(compact_font)

        # Ket qua di chuyen PLC la thong tin quan trong nhat khi do 2 diem -> lam noi bat
        emphasis_font = QFont("Consolas", 10)
        emphasis_font.setBold(True)
        self.plc_move_label.setFont(emphasis_font)

    def build_ui(self):
        central = QWidget()
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)
        main_layout.addWidget(self.view, stretch=1)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        camera_group = QGroupBox("Ket noi Camera")
        camera_form = QFormLayout(camera_group)
        camera_form.setContentsMargins(8, 8, 8, 8)
        camera_form.setSpacing(4)
        camera_form.addRow("Camera source:", self.source_edit)
        frame_size_layout = QHBoxLayout()
        frame_size_layout.addWidget(self.frame_width_spin)
        frame_size_layout.addWidget(self.frame_height_spin)
        camera_form.addRow("Frame size:", frame_size_layout)
        camera_button_layout = QGridLayout()
        camera_button_layout.setHorizontalSpacing(4)
        camera_button_layout.setVerticalSpacing(4)
        camera_button_layout.addWidget(self.start_btn, 0, 0)
        camera_button_layout.addWidget(self.stop_btn, 0, 1)
        camera_button_layout.addWidget(self.clear_btn, 1, 0, 1, 2)
        camera_form.addRow(camera_button_layout)

        measure_group = QGroupBox("Che do do luong")
        measure_form = QFormLayout(measure_group)
        measure_form.setContentsMargins(8, 8, 8, 8)
        measure_form.setSpacing(4)
        measure_form.addRow(self.measure_two_points_check)

        # Layout chứa 2 nút chọn P1 và P2 mới
        p1p2_layout = QHBoxLayout()
        p1p2_layout.addWidget(self.btn_select_p1)
        p1p2_layout.addWidget(self.btn_select_p2)
        measure_form.addRow(p1p2_layout)

        measure_form.addRow(self.grid_from_sample_check)
        measure_form.addRow(self.lock_sample_check)

        param_group = QGroupBox("Thong so quy doi")
        param_form = QFormLayout(param_group)
        param_form.setContentsMargins(8, 8, 8, 8)
        param_form.setSpacing(4)
        param_form.addRow("Zoom hien tai:", self.zoom_spin)
        param_form.addRow("FOV X o 1x:", self.fov_x_spin)
        param_form.addRow("FOV Y o 1x:", self.fov_y_spin)
        param_form.addRow("PLC X:", self.plc_x_spin)
        param_form.addRow("PLC Y:", self.plc_y_spin)
        param_form.addRow(self.invert_y_check)

        p1_real_group = QGroupBox("Toa do thuc P1 (nhap tay)")
        p1_real_form = QFormLayout(p1_real_group)
        p1_real_form.setContentsMargins(8, 8, 8, 8)
        p1_real_form.setSpacing(4)
        p1_real_form.addRow("P1 thuc X:", self.p1_real_x_spin)
        p1_real_form.addRow("P1 thuc Y:", self.p1_real_y_spin)

        zoom_group = QGroupBox("Sony Zoom Control")
        zoom_form = QFormLayout(zoom_group)
        zoom_form.setContentsMargins(8, 8, 8, 8)
        zoom_form.setSpacing(4)
        zoom_form.addRow("COM Port:", self.com_port_combo)
        zoom_form.addRow("Baud rate:", self.baud_combo)
        zoom_connect_layout = QGridLayout()
        zoom_connect_layout.setHorizontalSpacing(4)
        zoom_connect_layout.setVerticalSpacing(4)
        zoom_connect_layout.addWidget(self.connect_zoom_btn, 0, 0)
        zoom_connect_layout.addWidget(self.disconnect_zoom_btn, 0, 1)
        zoom_connect_layout.addWidget(self.read_zoom_btn, 1, 0)
        zoom_connect_layout.addWidget(self.refresh_ports_btn, 1, 1)
        zoom_form.addRow(zoom_connect_layout)

        zoom_button_layout = QHBoxLayout()
        zoom_button_layout.addWidget(self.zoom_out_btn)
        zoom_button_layout.addWidget(self.zoom_in_btn)
        zoom_button_layout.addWidget(self.zoom_stop_btn)
        zoom_form.addRow(zoom_button_layout)

        status_group = QGroupBox("Trang thai")
        status_layout = QVBoxLayout(status_group)
        status_layout.setContentsMargins(8, 8, 8, 8)
        status_layout.setSpacing(3)
        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.image_size_label)
        status_layout.addWidget(self.zoom_status_label)
        status_layout.addWidget(self.zoom_raw_label)
        status_layout.addWidget(self.zoom_value_label)

        result_single_group = QGroupBox("Ket qua - Diem tam")
        result_single_layout = QVBoxLayout(result_single_group)
        result_single_layout.setContentsMargins(8, 8, 8, 8)
        result_single_layout.setSpacing(3)
        result_single_layout.addWidget(self.click_pixel_label)
        result_single_layout.addWidget(self.camera_coord_label)
        result_single_layout.addWidget(self.distance_label)
        result_single_layout.addWidget(self.machine_coord_label)
        result_single_layout.addWidget(self.grid_label)

        result_two_point_group = QGroupBox("Ket qua - Do 2 diem (P1/P2)")
        result_two_point_layout = QVBoxLayout(result_two_point_group)
        result_two_point_layout.setContentsMargins(8, 8, 8, 8)
        result_two_point_layout.setSpacing(3)
        result_two_point_layout.addWidget(self.two_point_label)
        result_two_point_layout.addWidget(self.p2_real_label)
        result_two_point_layout.addWidget(self.plc_move_label)

        right_layout.addWidget(camera_group)
        right_layout.addWidget(measure_group)
        right_layout.addWidget(p1_real_group)
        right_layout.addWidget(param_group)
        right_layout.addWidget(zoom_group)
        right_layout.addWidget(status_group)
        right_layout.addWidget(result_single_group)
        right_layout.addWidget(result_two_point_group)
        right_layout.addStretch()

        scroll_area = QScrollArea()
        scroll_area.setWidget(right_panel)
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        scroll_area.setMaximumWidth(400)
        scroll_area.setMinimumWidth(340)

        main_layout.addWidget(scroll_area)
        self.setCentralWidget(central)

    def get_com_ports(self):
        ports = []

        if serial is not None:
            try:
                ports.extend(port.device for port in serial.tools.list_ports.comports())
            except Exception:
                pass

        try:
            import clr  # type: ignore
            import System  # type: ignore

            ports.extend(System.IO.Ports.SerialPort.GetPortNames())
        except Exception:
            pass

        unique_ports = []
        for port in ports:
            if port and port not in unique_ports:
                unique_ports.append(port)

        return unique_ports or ["COM1", "COM2", "COM3"]

    def refresh_com_ports(self):
        current_text = self.com_port_combo.currentText().strip()
        ports = self.get_com_ports()

        self.com_port_combo.blockSignals(True)
        self.com_port_combo.clear()
        self.com_port_combo.addItems(ports)
        if current_text:
            if current_text not in ports:
                self.com_port_combo.addItem(current_text)
            self.com_port_combo.setCurrentText(current_text)
        self.com_port_combo.blockSignals(False)

        self.zoom_status_label.setText(f"Zoom: Tim thay {len(ports)} cong COM")

    def update_zoom_controls_enabled(self, connected):
        can_use_zoom = self.zoom_camera_class is not None and serial is not None
        self.connect_zoom_btn.setEnabled(not connected and can_use_zoom)
        self.disconnect_zoom_btn.setEnabled(connected)
        self.read_zoom_btn.setEnabled(connected)
        self.zoom_in_btn.setEnabled(connected)
        self.zoom_out_btn.setEnabled(connected)
        self.zoom_stop_btn.setEnabled(connected)
        self.com_port_combo.setEnabled(not connected)
        self.baud_combo.setEnabled(not connected)
        self.refresh_ports_btn.setEnabled(not connected)

        if not can_use_zoom:
            details = []
            if SERIAL_IMPORT_ERROR:
                details.append(f"pyserial: {SERIAL_IMPORT_ERROR}")
            if SONY_CAMERA_IMPORT_ERROR:
                details.append(f"camera.py: {SONY_CAMERA_IMPORT_ERROR}")

            message = "Zoom: Thieu camera.py hoac pyserial"
            if details:
                message = f"{message} ({'; '.join(details)})"
            self.zoom_status_label.setText(message)

    def start_camera(self):
        source = self.source_edit.text().strip()
        if not source:
            QMessageBox.warning(self, "Loi", "Ban chua nhap camera source.")
            return

        self.camera_thread = CameraThread(
            source,
            self.frame_width_spin.value(),
            self.frame_height_spin.value(),
        )
        self.camera_thread.frame_ready.connect(self.on_frame_ready)
        self.camera_thread.status_signal.connect(self.on_status_changed)
        self.camera_thread.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def stop_camera(self):
        if self.camera_thread is not None:
            self.camera_thread.stop()
            self.camera_thread = None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def clear_point(self):
        self.lock_sample_check.setChecked(False)
        self.last_click_x = None
        self.last_click_y = None
        self.point1 = None
        self.point2 = None
        self.current_target_point = None
        self.btn_select_p1.setChecked(False)
        self.btn_select_p2.setChecked(False)
        self.view.clear_click()

        self.click_pixel_label.setText("Click: -")
        self.camera_coord_label.setText("Tam: -")
        self.distance_label.setText("d tam: -")
        self.two_point_label.setText("P1-P2: -")
        self.p2_real_label.setText("P2 thuc: -")
        self.plc_move_label.setText("Di chuyen PLC: -")
        self.grid_label.setText("Luoi: -")
        self.machine_coord_label.setText("PLC: -")
        self.view.set_grid_overlay(False)

    def clear_two_point_measure(self, state):
        self.point1 = None
        self.point2 = None
        self.current_target_point = None
        self.btn_select_p1.setChecked(False)
        self.btn_select_p2.setChecked(False)
        self.view.clear_two_points()
        
        is_checked = state == Qt.Checked.value or state == 2
        self.view.is_two_point_mode = is_checked

        self.btn_select_p1.setEnabled(is_checked)
        self.btn_select_p2.setEnabled(is_checked)

        if is_checked:
            self.two_point_label.setText(
                "P1-P2: Bật đo tự do. Nhập tọa độ thực P1 rồi nhấn 'Click Chọn P1' hoặc 'P2'"
            )
        else:
            self.two_point_label.setText("P1-P2: -")
        self.p2_real_label.setText("P2 thuc: -")
        self.plc_move_label.setText("Di chuyen PLC: -")

    def prepare_select_p1(self):
        if self.btn_select_p1.isChecked():
            self.btn_select_p2.setChecked(False)
            self.current_target_point = "P1"
            self.two_point_label.setText("Trạng thái: Đang chờ click chọn P1 trên màn hình...")
        else:
            self.current_target_point = None
            self.two_point_label.setText("P1-P2: Hủy chọn P1.")

    def prepare_select_p2(self):
        if self.btn_select_p2.isChecked():
            self.btn_select_p1.setChecked(False)
            self.current_target_point = "P2"
            self.two_point_label.setText("Trạng thái: Đang chờ click chọn P2 trên màn hình (P1 cố định)...")
        else:
            self.current_target_point = None
            self.two_point_label.setText("P1-P2: Hủy chọn P2.")

    def on_lock_sample_changed(self, state):
        is_locked = state == Qt.Checked.value or state == 2
        self.view.lock_click = is_locked
        if is_locked:
            self.status_label.setText("Camera: [ĐÃ KHÓA MẪU] Chỉ chặn đổi điểm tâm gốc.")
        else:
            self.status_label.setText("Camera: Đang chạy.")
        self.view.update()

    def on_status_changed(self, text):
        if self.lock_sample_check.isChecked():
            self.status_label.setText(f"Camera: {text} [ĐÃ KHÓA MẪU]")
        else:
            self.status_label.setText(f"Camera: {text}")

    def on_frame_ready(self, frame):
        height, width = frame.shape[:2]
        self.image_size_label.setText(f"Anh: {width} x {height} px")
        self.view.set_frame(frame)

    def connect_zoom_camera(self):
        if self.zoom_camera_class is None or serial is None:
            detail_lines = []
            if SERIAL_IMPORT_ERROR:
                detail_lines.append(f"pyserial: {SERIAL_IMPORT_ERROR}")
            if SONY_CAMERA_IMPORT_ERROR:
                detail_lines.append(f"camera.py: {SONY_CAMERA_IMPORT_ERROR}")

            QMessageBox.warning(
                self,
                "Thieu thu vien",
                "Khong su dung duoc pyserial hoac bo dieu khien zoom Sony."
                + (f"\n\nChi tiet:\n" + "\n".join(detail_lines) if detail_lines else ""),
            )
            return

        self.disconnect_zoom_camera()
        port = self.com_port_combo.currentText().strip()
        baudrate = int(self.baud_combo.currentText())

        if not port:
            QMessageBox.warning(self, "Loi", "Ban chua nhap cong COM.")
            return

        self.sony_camera = self.zoom_camera_class(port=port, baudrate=baudrate)

        try:
            self.sony_camera.serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=1,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
            )
            self.sony_camera.is_connected = True
        except Exception as exc:
            self.zoom_status_label.setText(f"Zoom: Ket noi that bai ({port})")
            self.sony_camera = None
            self.update_zoom_controls_enabled(False)
            QMessageBox.critical(
                self,
                "Loi ket noi COM",
                f"Khong mo duoc cong {port} @ {baudrate}.\n\nChi tiet: {exc}",
            )
            return

        self.zoom_status_label.setText(
            f"Zoom: Da ket noi {port} @ {baudrate}"
        )
        self.update_zoom_controls_enabled(True)
        self.refresh_zoom_value()
        self.zoom_timer.start()

    def disconnect_zoom_camera(self):
        self.zoom_timer.stop()

        if self.sony_camera is not None:
            try:
                self.sony_camera.disconnect()
            finally:
                self.sony_camera = None

        self.zoom_raw_label.setText("VISCA: -")
        self.zoom_value_label.setText("Zoom cam: -")
        self.zoom_status_label.setText("Zoom: Chua ket noi")
        self.update_zoom_controls_enabled(False)

    def refresh_zoom_value(self):
        if self.sony_camera is None or not self.sony_camera.is_connected:
            return

        zoom_pos = self.sony_camera.get_zoom_position()
        if zoom_pos is None:
            self.zoom_status_label.setText("Zoom: Khong doc duoc gia tri")
            return

        self.zoom_status_label.setText("Zoom: Dang ket noi")
        self.set_zoom_value_from_camera(zoom_pos)

    def set_zoom_value_from_camera(self, zoom_pos):
        zoom_value = zoom_pos_to_multiplier(zoom_pos)
        self.zoom_raw_label.setText(f"VISCA: 0x{zoom_pos:04X} ({zoom_pos})")
        self.zoom_value_label.setText(f"Zoom cam: {zoom_value:.1f}x")

        self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(zoom_value)
        self.zoom_spin.blockSignals(False)
        self.recalculate_last_click()

    def step_zoom(self, delta):
        if self.sony_camera is None or not self.sony_camera.is_connected or self.is_zoom_busy:
            return

        self.is_zoom_busy = True
        try:
            current_pos = self.sony_camera.get_zoom_position()
            if current_pos is None:
                self.zoom_status_label.setText("Zoom: Khong doc duoc vi tri")
                return

            target_pos = max(0x0000, min(0x4000, current_pos + delta))
            if not self.sony_camera.zoom_direct(target_pos):
                self.zoom_status_label.setText("Zoom: Gui lenh that bai")
                return

            self.zoom_status_label.setText("Zoom: Da gui lenh")
            self.set_zoom_value_from_camera(target_pos)
        finally:
            self.is_zoom_busy = False

    def stop_zoom_motion(self):
        if self.sony_camera is None or not self.sony_camera.is_connected:
            return

        self.sony_camera.zoom_stop()
        self.zoom_status_label.setText("Zoom: Da dung")
        self.refresh_zoom_value()

    def get_current_fov(self):
        zoom = self.zoom_spin.value()
        fov_x_1x = self.fov_x_spin.value()
        fov_y_1x = self.fov_y_spin.value()
        return fov_x_1x / zoom, fov_y_1x / zoom

    def get_mm_per_pixel(self):
        image_w = self.view.frame_w
        image_h = self.view.frame_h
        if image_w <= 0 or image_h <= 0:
            return None

        fov_x, fov_y = self.get_current_fov()
        return fov_x / image_w, fov_y / image_h

    def calculate_click_result(self, click_x, click_y):
        image_w = self.view.frame_w
        image_h = self.view.frame_h
        if image_w <= 0 or image_h <= 0:
            return None

        mm_per_pixel = self.get_mm_per_pixel()
        if mm_per_pixel is None:
            return None

        mm_per_pixel_x, mm_per_pixel_y = mm_per_pixel
        center_x = image_w / 2.0
        center_y = image_h / 2.0
        dx_pixel = click_x - center_x
        dy_pixel = click_y - center_y

        dx_mm = dx_pixel * mm_per_pixel_x
        dy_mm = dy_pixel * mm_per_pixel_y
        distance_mm = math.sqrt(dx_mm ** 2 + dy_mm ** 2)

        plc_x = self.plc_x_spin.value()
        plc_y = self.plc_y_spin.value()
        world_x = plc_x + dx_mm
        world_y = plc_y - dy_mm if self.invert_y_check.isChecked() else plc_y + dy_mm

        return {
            "dx_mm": dx_mm,
            "dy_mm": dy_mm,
            "distance_mm": distance_mm,
            "world_x": world_x,
            "world_y": world_y,
        }

    def calculate_two_point_result(self):
        if self.point1 is None or self.point2 is None:
            return None

        mm_per_pixel = self.get_mm_per_pixel()
        if mm_per_pixel is None:
            return None

        mm_per_pixel_x, mm_per_pixel_y = mm_per_pixel
        dx_pixel = self.point2[0] - self.point1[0]
        dy_pixel = self.point2[1] - self.point1[1]
        dx_mm = dx_pixel * mm_per_pixel_x
        dy_mm = dy_pixel * mm_per_pixel_y
        distance_mm = math.hypot(dx_mm, dy_mm)

        return {
            "dx_pixel": dx_pixel,
            "dy_pixel": dy_pixel,
            "dx_mm": dx_mm,
            "dy_mm": dy_mm,
            "distance_mm": distance_mm,
        }

    def calculate_p2_real_result(self):
        """Suy ra toa do thuc cua P2 tu toa do thuc P1 (nhap tay) va offset pixel P1->P2.

        Dong thoi tinh toa do thuc cua tam anh (theo he quy chieu P1) va luong
        di chuyen PLC can thiet de tam anh trung voi P2.
        """
        if self.point1 is None or self.point2 is None:
            return None

        mm_per_pixel = self.get_mm_per_pixel()
        if mm_per_pixel is None:
            return None

        mm_per_pixel_x, mm_per_pixel_y = mm_per_pixel
        invert_y = self.invert_y_check.isChecked()

        def pixel_offset_to_real(dx_pixel, dy_pixel):
            dx_mm = dx_pixel * mm_per_pixel_x
            dy_mm = dy_pixel * mm_per_pixel_y
            return dx_mm, (-dy_mm if invert_y else dy_mm)

        p1_real_x = self.p1_real_x_spin.value()
        p1_real_y = self.p1_real_y_spin.value()

        # Toa do thuc P2 = P1 thuc + offset thuc P1->P2
        d12_real_x, d12_real_y = pixel_offset_to_real(
            self.point2[0] - self.point1[0],
            self.point2[1] - self.point1[1],
        )
        p2_real_x = p1_real_x + d12_real_x
        p2_real_y = p1_real_y + d12_real_y

        # Toa do thuc cua tam anh (theo he quy chieu P1)
        center_x = self.view.frame_w / 2.0
        center_y = self.view.frame_h / 2.0
        dc1_real_x, dc1_real_y = pixel_offset_to_real(
            self.point1[0] - center_x,
            self.point1[1] - center_y,
        )
        center_real_x = p1_real_x - dc1_real_x
        center_real_y = p1_real_y - dc1_real_y

        # Luong di chuyen de tam trung P2 = toa do P2 - toa do tam
        move_x = p2_real_x - center_real_x
        move_y = p2_real_y - center_real_y

        # Toa do PLC dich (dua tren PLC hien tai) de tam trung P2
        plc_target = self.calculate_click_result(self.point2[0], self.point2[1])

        return {
            "p2_real_x": p2_real_x,
            "p2_real_y": p2_real_y,
            "center_real_x": center_real_x,
            "center_real_y": center_real_y,
            "move_x": move_x,
            "move_y": move_y,
            "plc_target_x": plc_target["world_x"] if plc_target else None,
            "plc_target_y": plc_target["world_y"] if plc_target else None,
        }

    def on_image_clicked(self, click_x, click_y):
        # Chế độ đo 2 điểm tự do bằng nút bấm thủ công
        if self.measure_two_points_check.isChecked():
            if self.current_target_point == "P1":
                self.point1 = (click_x, click_y)
                self.btn_select_p1.setChecked(False)
                self.current_target_point = None
                
                # Sau khi gán P1, tự động tính toán hiển thị (nếu đã từng có P2)
                if self.point2 is not None:
                    self.update_two_point_display()
                else:
                    self.view.set_two_point_result(self.point1, None, 0.0, 0.0, 0.0)
                    self.two_point_label.setText(
                        f"Đã cố định P1=({click_x}, {click_y}) px, "
                        f"tọa độ thực P1=({self.p1_real_x_spin.value():.3f}, "
                        f"{self.p1_real_y_spin.value():.3f}) mm. Hãy bấm nút 'Chọn P2'"
                    )
            
            elif self.current_target_point == "P2":
                self.point2 = (click_x, click_y)
                # GIỮ CHẾ ĐỘ CHỌN P2 LIÊN TỤC: Không tắt Checked của btn_select_p2
                # Việc này cho phép người dùng click liên tục trên màn hình để dời P2 đi nơi khác.
                self.update_two_point_display()
            else:
                self.two_point_label.setText("Hãy nhấn chọn nút 'Click Chọn P1' hoặc 'Click Chọn P2' trước khi click màn hình.")
            return

        # Chế độ đo tâm mặc định
        self.last_click_x = click_x
        self.last_click_y = click_y
        self.update_result_display(click_x, click_y)

    def recalculate_last_click(self):
        if self.last_click_x is not None and self.last_click_y is not None:
            self.update_result_display(self.last_click_x, self.last_click_y)

        if self.point1 is not None and self.point2 is not None:
            self.update_two_point_display()

        self.update_grid_from_last_click()

    def update_grid_from_last_click(self, *_args):
        if not self.grid_from_sample_check.isChecked():
            self.view.set_grid_overlay(False)
            self.grid_label.setText("Luoi: -")
            return

        if self.last_click_x is None or self.last_click_y is None:
            self.view.set_grid_overlay(False)
            self.grid_label.setText("Luoi: can click 1 diem mau")
            return

        result = self.calculate_click_result(self.last_click_x, self.last_click_y)
        mm_per_pixel = self.get_mm_per_pixel()
        if result is None or mm_per_pixel is None:
            self.view.set_grid_overlay(False)
            self.grid_label.setText("Luoi: chua co du lieu anh")
            return

        step_mm = result["distance_mm"]
        if step_mm <= 0:
            self.view.set_grid_overlay(False)
            self.grid_label.setText("Luoi: diem mau trung tam")
            return

        mm_per_pixel_x, mm_per_pixel_y = mm_per_pixel
        self.view.set_grid_overlay(True, step_mm, mm_per_pixel_x, mm_per_pixel_y)
        self.grid_label.setText(f"Luoi: moi o = {step_mm:.4f} mm")

    def update_two_point_display(self):
        result = self.calculate_two_point_result()
        if result is None:
            # Nếu chỉ mới có 1 trong 2 điểm
            self.view.set_two_point_result(self.point1, self.point2, 0.0, 0.0, 0.0)
            self.p2_real_label.setText("P2 thuc: -")
            self.plc_move_label.setText("Di chuyen PLC: -")
            return

        self.view.set_two_point_result(
            self.point1,
            self.point2,
            result["dx_mm"],
            result["dy_mm"],
            result["distance_mm"],
        )

        self.two_point_label.setText(
            "P1-P2: "
            f"P1=({self.point1[0]},{self.point1[1]}) "
            f"P2=({self.point2[0]},{self.point2[1]}) px\n"
            f"dx={result['dx_mm']:.4f} dy={result['dy_mm']:.4f} "
            f"d={result['distance_mm']:.4f} mm"
        )

        self.update_p2_real_display()

    def update_p2_real_display(self):
        real = self.calculate_p2_real_result()
        if real is None:
            self.p2_real_label.setText("P2 thuc: -")
            self.plc_move_label.setText("Di chuyen PLC: -")
            return

        self.p2_real_label.setText(
            "P2 thuc (theo P1): "
            f"X={real['p2_real_x']:.4f}, Y={real['p2_real_y']:.4f} mm\n"
            "Tam thuc (theo P1): "
            f"X={real['center_real_x']:.4f}, Y={real['center_real_y']:.4f} mm"
        )

        move_text = (
            "Di chuyen PLC de tam trung P2:\n"
            f"dX={real['move_x']:+.4f}, dY={real['move_y']:+.4f} mm"
        )
        if real["plc_target_x"] is not None and real["plc_target_y"] is not None:
            move_text += (
                f"\nPLC dich (tu PLC hien tai): "
                f"X={real['plc_target_x']:.4f}, Y={real['plc_target_y']:.4f} mm"
            )
        self.plc_move_label.setText(move_text)

    def update_result_display(self, click_x, click_y):
        result = self.calculate_click_result(click_x, click_y)
        if result is None:
            return

        self.view.set_click_result(
            click_x,
            click_y,
            result["dx_mm"],
            result["dy_mm"],
            result["distance_mm"],
        )

        self.click_pixel_label.setText(f"Click: x={click_x}, y={click_y} px")
        self.camera_coord_label.setText(
            f"Tam: dx={result['dx_mm']:.4f}, dy={result['dy_mm']:.4f} mm"
        )
        self.distance_label.setText(
            f"d tam: {result['distance_mm']:.4f} mm"
        )
        self.machine_coord_label.setText(
            f"PLC: X={result['world_x']:.4f}, Y={result['world_y']:.4f} mm"
        )
        self.update_grid_from_last_click()

    def closeEvent(self, event):
        self.stop_camera()
        self.disconnect_zoom_camera()
        event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())