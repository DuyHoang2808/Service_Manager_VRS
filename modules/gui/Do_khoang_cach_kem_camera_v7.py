"""
Do_khoang_cach_kem_camera_v7.py
--------------------------------------------------------------------
v6 (Do khoang cach + Camera / PLC Gateway / Bu lech Board / Chup anh) +
CHUC NANG MOI: "Phat stream" - GUI van la CHU camera (mo truc tiep
bang OpenCV nhu cu, frame raw dung de do luong khong doi), dong thoi
PHAT LAI luong frame do ra ngoai cho may khac xem:

  1) RTSP  : day frame raw vao FFmpeg -> rtsp://<host>:8554/mystream
             (tai su dung dung bo tham so low-latency cua
              D:\\Camera\\Dev\\Stream_camera\\Stream_camera_rtsp\\Stream_camera_Sony.py)
             -> CAN chay MediaMTX truoc.

  2) WEB   : HTTP server ngay trong GUI, khong can cai them gi:
                http://<ip>:9000/            -> trang xem truc tiep
                http://<ip>:9000/stream.mjpg -> luong MJPEG
                http://<ip>:9000/snapshot.jpg-> anh hien tai
                http://<ip>:9000/status      -> JSON trang thai

  3) DIEM GOC: nut "Phat hien diem goc (YOLO)" - gui frame hien tai sang
             Fiducial Detector Service (../fiducial_detector, port 8191) de tu
             dong tim diem goc thay vi click tay, roi tinh dx/dy/PLC world X,Y
             y het duong click. Tuong duong nut cung ten trong ban
             Calib_Phan_Cung_VRS/Do_Khoang_Cach/Do_khoang_cach_kem_camera_v6.py,
             nhung goi service thay vi tu nap ultralytics vao tien trinh GUI.

Ly do lam theo huong nay (GUI lam chu camera, khong phai client RTSP):
tren Windows camera USB/DirectShow chi 1 process mo duoc, va frame di
qua H.264 se mat chi tiet -> anh huong do chinh xac quy doi pixel->mm.
Vi vay GUI giu frame goc de do, chi ban sao moi bi nen de phat di.

KHONG sua v3/v4/v5/v6 va cac file goc - chi ke thua MergedMainWindowV6
va them 1 nhom UI "Phat stream (Server)" vao panel ben phai.

Chay:
    python Do_khoang_cach_kem_camera_v7.py

(PLC Gateway server van phai chay rieng: python run_plc_gateway.py)
(Neu dung RTSP: chay MediaMTX truoc, vi du
 D:\\Driver\\mediamtx_v1.18.1_windows_amd64\\mediamtx.exe)
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

import cv2
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
)

# Khi dong goi bang PyInstaller, bootloader khong ton trong PYTHONIOENCODING nhu
# python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8 tai code, neu
# khong cac dong print/log emoji/tieng Viet co dau (tu v3-v6 hoac bu_lech_board)
# se nem UnicodeEncodeError va crash exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

from Do_khoang_cach_kem_camera_v6 import MergedMainWindowV6  # noqa: E402

# Duong dan FFmpeg mac dinh (giong Stream_camera_Sony.py) - chi dung khi
# khong tim thay ffmpeg trong PATH.
FALLBACK_FFMPEG_PATH = (
    r"D:\Driver\ffmpeg-2026-05-06-git-f2e5eff3ff-essentials_build\bin\ffmpeg.exe"
)

DEFAULT_RTSP_URL = "rtsp://localhost:8554/mystream"
DEFAULT_WEB_PORT = 9000
DEFAULT_STREAM_FPS = 30
DEFAULT_JPEG_QUALITY = 75
DEFAULT_MAX_WIDTH = 1280  # 0 = giu nguyen do phan giai goc

# Neu RTSP server khong bat tay duoc, FFmpeg KHONG bao loi ma treo im lang (da
# kiem chung tren ffmpeg 2026-05-06: chay 25s khong thoat, khong log gi). No van
# nuot vai frame dau de probe input roi moi ket, nen "ghi duoc frame dau tien"
# KHONG phai tin hieu song. Canh gac dua tren do lech giua luc nhan frame va luc
# ghi duoc frame: qua nguong nay coi nhu FFmpeg chet dung va huy phat.
RTSP_STALL_TIMEOUT = 6.0

# Phat hien "diem goc" (fiducial): GUI KHONG tu nap YOLO ma goi service
# AutoBoardOffset_YOLO_2Mat/fiducial_detector (chay rieng: run_fiducial_service.py).
# Loi the so voi nap model trong GUI: khong keo theo torch/ultralytics vao tien
# trinh GUI, khong tranh VRAM voi service, va service tra ve tam da TINH CHINH
# SUB-PIXEL theo contour - quan trong vi ta dang quy doi pixel -> mm.
FIDUCIAL_SERVICE_URL = "http://localhost:8191"
FIDUCIAL_DETECT_PATH = "/api/detect-marker"
DEFAULT_FIDUCIAL_CONF = 0.5
FIDUCIAL_TIMEOUT = 15.0
FIDUCIAL_JPEG_QUALITY = 95  # gui anh net cho service, dung ha xuong 75 nhu luong xem


def resolve_ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.path.isfile(FALLBACK_FFMPEG_PATH):
        return FALLBACK_FFMPEG_PATH
    return "ffmpeg"


def get_lan_ip() -> str:
    """Lay IP LAN cua may nay (khong thuc su gui goi tin nao)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


# =====================================================================
# Kho frame dung chung: giu frame MOI NHAT, thread-safe
# =====================================================================
class LatestFrameStore:
    """Chi giu 1 frame moi nhat + so thu tu, danh thuc nguoi doc dang cho.

    GUI thread chi lam viec re nhat: gan tham chieu + notify_all.
    Moi viec nang (encode JPEG, ghi pipe FFmpeg) deu o thread khac.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0

    def put(self, frame) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            self._cond.notify_all()

    def get_latest(self):
        with self._cond:
            return self._frame, self._seq

    def wait_for_new(self, last_seq: int, timeout: float):
        """Cho den khi co frame moi hon last_seq. Tra ve (frame, seq)."""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait(timeout)
            return self._frame, self._seq

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()


def scale_frame(frame, max_width: int):
    """Thu nho frame neu rong hon max_width (giu ty le). max_width<=0 -> giu nguyen."""
    if max_width and max_width > 0:
        height, width = frame.shape[:2]
        if width > max_width:
            new_height = max(1, int(round(height * max_width / float(width))))
            return cv2.resize(
                frame, (max_width, new_height), interpolation=cv2.INTER_AREA
            )
    return frame


# =====================================================================
# Publisher 1: RTSP qua FFmpeg
# =====================================================================
class RtspPublisher:
    """Day frame BGR raw vao stdin FFmpeg -> RTSP.

    Kien truc giong Stream_camera_Sony.py: Queue(maxsize=1) luon giu frame
    moi nhat, thread ghi rieng, pipe bufsize=0.
    """

    def __init__(self, on_status: Optional[Callable[[str], None]] = None) -> None:
        self._on_status = on_status or (lambda _text: None)
        self._queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._proc: Optional[subprocess.Popen] = None
        self._writer_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_submit_ts = 0.0
        self._last_write_ts = 0.0
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self._running = False
        self.width = 0
        self.height = 0
        self.url = ""

    @property
    def running(self) -> bool:
        return self._running

    @staticmethod
    def build_ffmpeg_cmd(ffmpeg_path: str, width: int, height: int, fps: int, url: str):
        gop = max(1, fps)  # 1 keyframe / giay
        return [
            ffmpeg_path,
            "-y",
            # ---- Input: raw video tu stdin ----
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            # ---- Encoder: toi uu latency ----
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-crf", "23",
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-bf", "0",
            "-profile:v", "baseline",
            "-pix_fmt", "yuv420p",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            # ---- Output RTSP: giam buffer toi da ----
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-max_delay", "0",
            "-flush_packets", "1",
            url,
        ]

    @staticmethod
    def probe_endpoint(url: str, timeout: float = 2.0) -> tuple[bool, str]:
        """Thu bat tay TCP toi host:port cua URL RTSP truoc khi goi FFmpeg.

        Bat som truong hop hay gap nhat: quen chay MediaMTX. Neu de FFmpeg tu
        xu ly thi no treo im lang chu khong bao loi.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 554
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, ""
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            return False, f"khong ket noi duoc toi {host}:{port} ({reason})"

    def start(self, ffmpeg_path: str, width: int, height: int, fps: int, url: str) -> bool:
        if self._running:
            return True

        reachable, reason = self.probe_endpoint(url)
        if not reachable:
            self._on_status(f"RTSP: {reason}. MediaMTX da chay chua?")
            return False

        self.width = width
        self.height = height
        self.url = url
        self._stop_event.clear()
        self._last_submit_ts = self._last_write_ts = time.time()
        with self._stderr_lock:
            self._stderr_tail = []

        cmd = self.build_ffmpeg_cmd(ffmpeg_path, width, height, fps, url)

        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # unbuffered: ghi thang, khong buffer them
                creationflags=creation_flags,
            )
        except FileNotFoundError:
            self._on_status(f"RTSP: khong tim thay FFmpeg tai '{ffmpeg_path}'")
            self._proc = None
            return False
        except Exception as exc:
            self._on_status(f"RTSP: khong khoi dong duoc FFmpeg ({exc})")
            self._proc = None
            return False

        self._running = True

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="RtspFfmpegStderr", daemon=True
        )
        self._stderr_thread.start()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="RtspFfmpegWriter", daemon=True
        )
        self._writer_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="RtspFfmpegWatchdog", daemon=True
        )
        self._watchdog_thread.start()

        self._on_status(f"RTSP: dang phat {width}x{height}@{fps} -> {url}")
        return True

    def _watchdog_loop(self) -> None:
        """FFmpeg co the treo im lang giua chung. Chi bao dong khi ta CO frame
        de gui ma mai khong ghi duoc - nen camera dung phat thi khong bao nham."""
        while self._running and not self._stop_event.wait(1.0):
            if not self._running:
                return

            starved = self._last_submit_ts - self._last_write_ts
            if starved <= RTSP_STALL_TIMEOUT:
                continue

            detail = self.last_error()
            self._on_status(
                f"RTSP: FFmpeg khong nuot frame trong {starved:.0f}s - da huy phat. "
                + (detail or "Kiem tra MediaMTX va duong dan RTSP.")
            )
            self.stop()
            return

    def submit(self, frame) -> None:
        """Goi tu GUI thread: bo frame cu, day frame moi nhat vao."""
        if not self._running:
            return
        self._last_submit_ts = time.time()
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except Exception:
                pass
        try:
            self._queue.put_nowait(frame)
        except Exception:
            pass

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw_line in iter(proc.stderr.readline, b""):
                text = raw_line.decode(errors="ignore").strip()
                if not text:
                    continue
                with self._stderr_lock:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail.pop(0)
        except Exception:
            pass

    # Tu khoa loc dong stderr dang loi that su - FFmpeg in rat nhieu dong
    # banner/thong tin encoder, dua nguyen dong cuoi ra thi vo nghia.
    _ERROR_HINTS = (
        "error", "failed", "refused", "timed out", "timeout", "invalid",
        "unable", "no route", "not found", "denied", "unauthorized",
        "connection", "broken pipe", "conversion failed",
    )

    def last_error(self) -> str:
        with self._stderr_lock:
            for line in reversed(self._stderr_tail):
                lowered = line.lower()
                if any(hint in lowered for hint in self._ERROR_HINTS):
                    return line
        return ""

    def _writer_loop(self) -> None:
        proc = self._proc
        expected_bytes = self.width * self.height * 3

        while self._running and proc is not None:
            if proc.poll() is not None:
                detail = self.last_error()
                self._on_status(
                    f"RTSP: FFmpeg dung (ma {proc.returncode})."
                    + (f" {detail}" if detail else " Kiem tra MediaMTX da chay chua.")
                )
                self._running = False
                break

            try:
                frame = self._queue.get(timeout=0.2)
            except Exception:
                continue

            # Bao ve: kich thuoc phai khop voi -s da khai bao voi FFmpeg
            if frame.shape[0] != self.height or frame.shape[1] != self.width:
                frame = cv2.resize(frame, (self.width, self.height))

            data = frame.tobytes()
            if len(data) != expected_bytes:
                continue

            try:
                # Luu y: neu FFmpeg treo (khong doc stdin), lenh nay se block
                # cho den khi stop() giet tien trinh -> khi do no nem OSError.
                proc.stdin.write(data)
            except (BrokenPipeError, OSError, ValueError) as exc:
                if self._running:
                    self._on_status(f"RTSP: mat ket noi toi FFmpeg ({exc})")
                self._running = False
                break

            self._last_write_ts = time.time()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()  # danh thuc watchdog dang ngu

        proc, self._proc = self._proc, None
        writer, self._writer_thread = self._writer_thread, None
        self._watchdog_thread = None

        # PHAI giet tien trinh TRUOC khi dong stdin: neu FFmpeg dang treo,
        # writer thread ket trong stdin.write() va dang giu khoa cua file
        # object -> dong stdin o day se deadlock. Giet FFmpeg lam dau doc pipe
        # bien mat, write() nem loi va writer thread thoat.
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass

        if writer is not None and writer is not threading.current_thread():
            writer.join(timeout=3)
            if writer.is_alive():
                # Khong doi duoc thi thoi, dung dong stdin keo treo GUI.
                proc = None

        if proc is not None:
            for stream in (proc.stdin, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break


# =====================================================================
# Publisher 2: Web (MJPEG + snapshot) - khong can thu vien ngoai
# =====================================================================
VIEWER_HTML = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VRS Camera - Live</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font-family: Segoe UI, Roboto, sans-serif;
         background:#111; color:#eee; display:flex; flex-direction:column;
         min-height:100vh; }
  header { padding:10px 16px; background:#1b1b1b; border-bottom:1px solid #333;
           display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  .dot { width:10px; height:10px; border-radius:50%; background:#2e7d32; }
  main { flex:1; display:flex; align-items:center; justify-content:center;
         padding:12px; }
  img { max-width:100%; max-height:calc(100vh - 110px); background:#000;
        border:1px solid #333; }
  a.btn { color:#eee; background:#333; padding:6px 12px; border-radius:4px;
          text-decoration:none; font-size:13px; }
  a.btn:hover { background:#444; }
  footer { padding:8px 16px; font-size:12px; color:#888; }
</style>
</head>
<body>
<header>
  <span class="dot"></span>
  <h1>VRS Camera - Live stream</h1>
  <a class="btn" href="/snapshot.jpg" target="_blank">Tai anh hien tai</a>
  <a class="btn" href="/status" target="_blank">Trang thai</a>
</header>
<main><img id="live" src="/stream.mjpg" alt="live stream"></main>
<footer>Nguon: GUI Do khoang cach (v7). Anh da duoc nen JPEG - chi de xem,
khong dung de do luong.</footer>
<script>
  // Neu luong bi dut (GUI dung camera), tu ket noi lai sau 2s.
  var img = document.getElementById('live');
  img.onerror = function () {
    setTimeout(function () { img.src = '/stream.mjpg?t=' + Date.now(); }, 2000);
  };
</script>
</body>
</html>
"""


class WebPublisher:
    """HTTP server phat MJPEG + snapshot tu frame moi nhat cua GUI."""

    BOUNDARY = "vrsframe"

    def __init__(self, on_status: Optional[Callable[[str], None]] = None) -> None:
        self._on_status = on_status or (lambda _text: None)
        self._raw_store = LatestFrameStore()   # frame BGR goc (da scale)
        self._jpeg_store = LatestFrameStore()  # bytes JPEG da encode
        self._encoder_thread: Optional[threading.Thread] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._server_thread: Optional[threading.Thread] = None
        self._running = False
        self.port = 0
        self.jpeg_quality = DEFAULT_JPEG_QUALITY
        self.max_fps = DEFAULT_STREAM_FPS

    @property
    def running(self) -> bool:
        return self._running

    def start(self, port: int, jpeg_quality: int, max_fps: int) -> bool:
        if self._running:
            return True

        self.port = port
        self.jpeg_quality = jpeg_quality
        self.max_fps = max(1, max_fps)

        try:
            self._server = ThreadingHTTPServer(
                ("0.0.0.0", port), self._make_handler()
            )
        except OSError as exc:
            self._server = None
            self._on_status(f"Web: khong mo duoc port {port} ({exc})")
            return False

        self._server.daemon_threads = True
        self._running = True

        self._encoder_thread = threading.Thread(
            target=self._encoder_loop, name="WebJpegEncoder", daemon=True
        )
        self._encoder_thread.start()

        self._server_thread = threading.Thread(
            target=self._server.serve_forever, name="WebStreamServer", daemon=True
        )
        self._server_thread.start()

        self._on_status(f"Web: dang phat tai http://{get_lan_ip()}:{port}/")
        return True

    def submit(self, frame) -> None:
        """Goi tu GUI thread - chi gan tham chieu, khong encode."""
        if self._running:
            self._raw_store.put(frame)

    # -------- encode JPEG o thread rieng, gioi han theo max_fps --------
    def _encoder_loop(self) -> None:
        last_seq = 0
        # He so 0.9: tranh bi jitter lam rot mat 1 nhip -> tut con nua FPS
        min_interval = 0.9 / float(self.max_fps)
        last_encode = 0.0

        while self._running:
            frame, seq = self._raw_store.wait_for_new(last_seq, timeout=0.5)
            if not self._running:
                break
            if frame is None or seq == last_seq:
                continue
            last_seq = seq

            now = time.time()
            if now - last_encode < min_interval:
                continue
            last_encode = now

            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality]
            )
            if ok:
                self._jpeg_store.put(buffer.tobytes())

        self._jpeg_store.wake_all()

    def snapshot_jpeg(self) -> Optional[bytes]:
        """Anh chat luong cao hon luong stream, encode ngay khi co yeu cau."""
        frame, _seq = self._raw_store.get_latest()
        if frame is None:
            return None
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return buffer.tobytes() if ok else None

    def _make_handler(self):
        publisher = self

        class StreamHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _send_bytes(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache, max-age=0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 (ten do BaseHTTPRequestHandler quy dinh)
                path = self.path.split("?")[0].rstrip("/") or "/"

                if path in ("/", "/viewer", "/viewer.html", "/index.html"):
                    self._send_bytes(
                        200, "text/html; charset=utf-8", VIEWER_HTML.encode("utf-8")
                    )
                    return

                if path in ("/snapshot", "/snapshot.jpg"):
                    jpeg = publisher.snapshot_jpeg()
                    if jpeg is None:
                        self._send_bytes(
                            503,
                            "application/json",
                            b'{"error":"snapshot_not_ready"}',
                        )
                        return
                    self._send_bytes(200, "image/jpeg", jpeg)
                    return

                if path == "/status":
                    _frame, seq = publisher._raw_store.get_latest()
                    body = json.dumps(
                        {
                            "running": publisher._running,
                            "frames_received": seq,
                            "jpeg_quality": publisher.jpeg_quality,
                            "max_fps": publisher.max_fps,
                        }
                    ).encode("utf-8")
                    self._send_bytes(200, "application/json", body)
                    return

                if path in ("/stream.mjpg", "/stream", "/video"):
                    self._serve_mjpeg()
                    return

                self._send_bytes(404, "application/json", b'{"error":"not_found"}')

            def _serve_mjpeg(self) -> None:
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    f"multipart/x-mixed-replace; boundary={WebPublisher.BOUNDARY}",
                )
                self.send_header("Cache-Control", "no-store, no-cache, max-age=0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

                last_seq = 0
                try:
                    while publisher._running:
                        jpeg, seq = publisher._jpeg_store.wait_for_new(last_seq, 1.0)
                        if jpeg is None or seq == last_seq:
                            continue  # timeout: vong lai de con phat hien client thoat
                        last_seq = seq

                        header = (
                            f"--{WebPublisher.BOUNDARY}\r\n"
                            f"Content-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpeg)}\r\n\r\n"
                        ).encode("ascii")
                        self.wfile.write(header)
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass  # client dong tab - binh thuong

            def log_message(self, fmt, *args):
                return  # tranh spam console

        return StreamHandler

    def stop(self) -> None:
        self._running = False
        self._raw_store.wake_all()
        self._jpeg_store.wake_all()

        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass


# =====================================================================
# Cua so chinh v7
# =====================================================================
class MergedMainWindowV7(MergedMainWindowV6):
    """v6 + phat lai luong camera ra RTSP va/hoac Web (MJPEG)."""

    stream_status_changed = Signal(str)
    origin_detected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            "VRS Control Center - Do khoang cach + PLC Gateway + Bu lech Board "
            "+ Chup anh + Phat stream + Phat hien diem goc"
        )

        self.rtsp_publisher = RtspPublisher(on_status=self.stream_status_changed.emit)
        self.web_publisher = WebPublisher(on_status=self.stream_status_changed.emit)
        self.stream_status_changed.connect(self._on_stream_status)
        self.origin_detected.connect(self._on_origin_detected)

        self._stream_max_width = DEFAULT_MAX_WIDTH
        self._last_frame_shape = None
        self._last_bgr_frame = None
        self._detect_busy = False

        # Them nhom stream truoc, nhom fiducial sau: ca hai cung chen vao vi tri
        # ngay duoi "Ket noi Camera" nen thu tu cuoi cung la
        # Camera -> Phat hien diem goc -> Phat stream.
        self._add_stream_group()
        self._add_detect_origin_group()

    # ---------------------------------------------------------------- UI
    def _add_stream_group(self) -> None:
        """Chen nhom 'Phat stream (Server)' vao panel phai, ngay duoi nhom
        'Ket noi Camera'. Dung self.start_btn (co san tu v3) de tim lai
        camera_group va layout cua panel phai -> khong phai sua v3."""
        self.rtsp_enable_check = QCheckBox("Phat RTSP (can MediaMTX)")
        self.rtsp_url_edit = QLineEdit(DEFAULT_RTSP_URL)

        self.ffmpeg_path_edit = QLineEdit(resolve_ffmpeg_path())
        self.ffmpeg_browse_btn = QPushButton("...")
        self.ffmpeg_browse_btn.setMaximumWidth(32)
        self.ffmpeg_browse_btn.clicked.connect(self._browse_ffmpeg)

        self.web_enable_check = QCheckBox("Phat Web (MJPEG + snapshot)")
        self.web_enable_check.setChecked(True)
        self.web_port_spin = QSpinBox()
        self.web_port_spin.setRange(1, 65535)
        self.web_port_spin.setValue(DEFAULT_WEB_PORT)

        self.stream_fps_spin = QSpinBox()
        self.stream_fps_spin.setRange(1, 120)
        self.stream_fps_spin.setValue(DEFAULT_STREAM_FPS)
        self.stream_fps_spin.setSuffix(" fps")

        self.jpeg_quality_spin = QSpinBox()
        self.jpeg_quality_spin.setRange(10, 100)
        self.jpeg_quality_spin.setValue(DEFAULT_JPEG_QUALITY)
        self.jpeg_quality_spin.setSuffix(" %")

        self.stream_width_spin = QSpinBox()
        self.stream_width_spin.setRange(0, 10000)
        self.stream_width_spin.setValue(DEFAULT_MAX_WIDTH)
        self.stream_width_spin.setSuffix(" px")
        self.stream_width_spin.setToolTip(
            "Rong toi da khi phat (0 = giu nguyen do phan giai goc).\n"
            "Chi anh huong luong phat di, KHONG anh huong frame dung de do."
        )

        self.stream_start_btn = QPushButton("Bat phat stream")
        self.stream_stop_btn = QPushButton("Dung phat")
        self.stream_start_btn.setMinimumHeight(30)
        self.stream_stop_btn.setMinimumHeight(30)
        self.stream_stop_btn.setEnabled(False)
        self.stream_start_btn.setStyleSheet(
            "QPushButton { background-color: #1565c0; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #7f8c8d; color: #dcdcdc; }"
        )
        self.stream_stop_btn.setStyleSheet(
            "QPushButton { background-color: #c0392b; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #7f8c8d; color: #dcdcdc; }"
        )
        self.stream_start_btn.clicked.connect(self.start_stream)
        self.stream_stop_btn.clicked.connect(self.stop_stream)

        self.stream_status_label = QLabel("Stream: Chua phat")
        self.stream_status_label.setWordWrap(True)
        self.stream_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        stream_group = QGroupBox("Phat stream (Server)")
        stream_form = QFormLayout(stream_group)
        stream_form.setContentsMargins(8, 8, 8, 8)
        stream_form.setSpacing(4)

        stream_form.addRow(self.web_enable_check)
        stream_form.addRow("Web port:", self.web_port_spin)
        stream_form.addRow(self.rtsp_enable_check)
        stream_form.addRow("RTSP URL:", self.rtsp_url_edit)

        ffmpeg_layout = QHBoxLayout()
        ffmpeg_layout.addWidget(self.ffmpeg_path_edit)
        ffmpeg_layout.addWidget(self.ffmpeg_browse_btn)
        stream_form.addRow("FFmpeg:", ffmpeg_layout)

        stream_form.addRow("FPS phat:", self.stream_fps_spin)
        stream_form.addRow("Chat luong JPEG:", self.jpeg_quality_spin)
        stream_form.addRow("Rong toi da:", self.stream_width_spin)

        stream_button_layout = QHBoxLayout()
        stream_button_layout.addWidget(self.stream_start_btn)
        stream_button_layout.addWidget(self.stream_stop_btn)
        stream_form.addRow(stream_button_layout)
        stream_form.addRow(self.stream_status_label)

        self._insert_group_below_camera(stream_group)

    def _insert_group_below_camera(self, group: QGroupBox) -> None:
        """Chen 1 QGroupBox vao panel phai, ngay duoi nhom "Ket noi Camera".

        self.start_btn la widget co san tu v3 - qua no tim nguoc ra camera_group
        va layout cua panel phai, nen khong phai sua v3. Neu cau truc UI doi,
        rot ve cach cua v6: nhet thang vao camera_form.
        """
        try:
            camera_group = self.start_btn.parentWidget()
            right_layout = camera_group.parentWidget().layout()
            right_layout.insertWidget(right_layout.indexOf(camera_group) + 1, group)
        except Exception:
            try:
                self.start_btn.parentWidget().layout().addRow(group)
            except Exception:
                pass

    # ------------------------------------------- UI phat hien diem goc
    def _add_detect_origin_group(self) -> None:
        self.fiducial_url_edit = QLineEdit(FIDUCIAL_SERVICE_URL)
        self.fiducial_url_edit.setToolTip(
            "Dia chi Fiducial Detector Service.\n"
            "Chay bang: python AutoBoardOffset_YOLO_2Mat/fiducial_detector/run_fiducial_service.py"
        )

        self.fiducial_conf_spin = QDoubleSpinBox()
        self.fiducial_conf_spin.setRange(0.05, 0.99)
        self.fiducial_conf_spin.setSingleStep(0.05)
        self.fiducial_conf_spin.setDecimals(2)
        self.fiducial_conf_spin.setValue(DEFAULT_FIDUCIAL_CONF)

        self.detect_origin_btn = QPushButton("Phat hien diem goc (YOLO)...")
        self.detect_origin_btn.setMinimumHeight(30)
        self.detect_origin_btn.setStyleSheet(
            "QPushButton { background-color: #6a1b9a; color: white; font-weight: bold; }"
            "QPushButton:disabled { background-color: #7f8c8d; color: #dcdcdc; }"
        )
        self.detect_origin_btn.clicked.connect(self.detect_origin_point)

        self.fiducial_status_label = QLabel("Fiducial: Chua chay lan nao")
        self.fiducial_status_label.setWordWrap(True)
        self.fiducial_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        origin_group = QGroupBox("Phat hien diem goc (Fiducial Service)")
        origin_form = QFormLayout(origin_group)
        origin_form.setContentsMargins(8, 8, 8, 8)
        origin_form.setSpacing(4)
        origin_form.addRow("Service URL:", self.fiducial_url_edit)
        origin_form.addRow("Nguong conf:", self.fiducial_conf_spin)
        origin_form.addRow(self.detect_origin_btn)
        origin_form.addRow(self.fiducial_status_label)

        self._insert_group_below_camera(origin_group)

    def detect_origin_point(self) -> None:
        """Gui frame hien tai sang Fiducial Service; ket qua tra ve duoc xu ly y
        het nhu nguoi dung click tay vao diem do (dung lai update_result_display
        co san cua v3 -> dx/dy/PLC world X,Y tinh giong het duong click)."""
        if self._detect_busy:
            return

        frame = self._last_bgr_frame
        if frame is None:
            QMessageBox.warning(
                self,
                "Chua co anh",
                "Camera chua co frame nao de nhan dien. Vui long bat camera truoc.",
            )
            return

        url = self.fiducial_url_edit.text().strip().rstrip("/") + FIDUCIAL_DETECT_PATH
        conf = self.fiducial_conf_spin.value()

        self._detect_busy = True
        self.detect_origin_btn.setEnabled(False)
        self.fiducial_status_label.setText("Fiducial: Dang gui anh sang service...")

        threading.Thread(
            target=self._detect_origin_worker,
            args=(frame, url, conf),
            name="FiducialDetect",
            daemon=True,
        ).start()

    def _detect_origin_worker(self, frame, url: str, conf: float) -> None:
        """Chay o thread rieng: encode + goi HTTP. Suy luan YOLO ton hang tram ms,
        de tren GUI thread se lam dung hinh."""
        try:
            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, FIDUCIAL_JPEG_QUALITY]
            )
            if not ok:
                self.origin_detected.emit({"error": "Khong encode duoc anh JPEG"})
                return

            payload = json.dumps(
                {
                    "image_base64": base64.b64encode(buffer.tobytes()).decode("ascii"),
                    "confidence_threshold": conf,
                }
            ).encode("utf-8")

            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=FIDUCIAL_TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
            self.origin_detected.emit(data)

        except urllib.error.HTTPError as exc:
            self.origin_detected.emit({"error": f"service tra loi HTTP {exc.code}"})
        except urllib.error.URLError as exc:
            self.origin_detected.emit(
                {
                    "error": f"khong goi duoc {url} ({exc.reason}). "
                    "Da chay run_fiducial_service.py chua?"
                }
            )
        except Exception as exc:
            self.origin_detected.emit({"error": str(exc)})

    def _on_origin_detected(self, data: dict) -> None:
        """Chay tren GUI thread (qua Signal)."""
        self._detect_busy = False
        self.detect_origin_btn.setEnabled(True)

        error = data.get("error")
        if error:
            self.fiducial_status_label.setText(f"Fiducial: LOI - {error}")
            return

        if not data.get("found"):
            message = data.get("message") or "Khong phat hien duoc diem goc."
            self.fiducial_status_label.setText(f"Fiducial: {message}")
            return

        # Giu do chinh xac sub-pixel cua service, chi lam tron de nhan cho de doc.
        click_x = round(float(data["cx"]), 2)
        click_y = round(float(data["cy"]), 2)

        self.last_click_x = click_x
        self.last_click_y = click_y
        self.update_result_display(click_x, click_y)

        confidence = data.get("confidence")
        refined = "sub-pixel" if data.get("refined") else "tam bbox"
        self.fiducial_status_label.setText(
            f"Fiducial: tim thay tai ({click_x}, {click_y}) px [{refined}], "
            f"conf={confidence:.2f}, {data.get('num_candidates', 0)} ung vien"
            if confidence is not None
            else f"Fiducial: tim thay tai ({click_x}, {click_y}) px [{refined}]"
        )

    def _browse_ffmpeg(self) -> None:
        current = self.ffmpeg_path_edit.text().strip()
        start_dir = os.path.dirname(current) if os.path.isfile(current) else ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "Chon ffmpeg.exe", start_dir, "FFmpeg (ffmpeg.exe);;Tat ca (*.*)"
        )
        if path:
            self.ffmpeg_path_edit.setText(path)

    # ------------------------------------------------------------ stream
    @property
    def is_streaming(self) -> bool:
        return self.rtsp_publisher.running or self.web_publisher.running

    def start_stream(self) -> None:
        if self.camera_thread is None or self._last_frame_shape is None:
            QMessageBox.warning(
                self,
                "Chua co camera",
                "Hay bat camera (Start Camera) va doi co frame dau tien "
                "truoc khi phat stream.",
            )
            return

        want_web = self.web_enable_check.isChecked()
        want_rtsp = self.rtsp_enable_check.isChecked()
        if not want_web and not want_rtsp:
            QMessageBox.warning(
                self, "Chua chon kenh", "Hay tich chon 'Phat Web' hoac 'Phat RTSP'."
            )
            return

        self._stream_max_width = self.stream_width_spin.value()
        fps = self.stream_fps_spin.value()

        # Kich thuoc thuc te sau khi scale - RTSP can biet truoc de khai bao -s
        height, width = self._last_frame_shape
        if self._stream_max_width and width > self._stream_max_width:
            height = max(1, int(round(height * self._stream_max_width / float(width))))
            width = self._stream_max_width

        started = []

        if want_web and not self.web_publisher.running:
            if self.web_publisher.start(
                self.web_port_spin.value(), self.jpeg_quality_spin.value(), fps
            ):
                started.append("Web")

        if want_rtsp and not self.rtsp_publisher.running:
            if self.rtsp_publisher.start(
                self.ffmpeg_path_edit.text().strip(),
                width,
                height,
                fps,
                self.rtsp_url_edit.text().strip() or DEFAULT_RTSP_URL,
            ):
                started.append("RTSP")

        if not started and not self.is_streaming:
            return  # thong bao loi da hien qua _on_stream_status

        self._update_stream_buttons()
        self._refresh_stream_status_text()

    def stop_stream(self) -> None:
        self.rtsp_publisher.stop()
        self.web_publisher.stop()
        self._update_stream_buttons()
        self.stream_status_label.setText("Stream: Da dung")

    def _update_stream_buttons(self) -> None:
        streaming = self.is_streaming
        self.stream_start_btn.setEnabled(not streaming)
        self.stream_stop_btn.setEnabled(streaming)
        for widget in (
            self.web_enable_check,
            self.web_port_spin,
            self.rtsp_enable_check,
            self.rtsp_url_edit,
            self.ffmpeg_path_edit,
            self.ffmpeg_browse_btn,
            self.stream_fps_spin,
            self.jpeg_quality_spin,
            self.stream_width_spin,
        ):
            widget.setEnabled(not streaming)

    def _refresh_stream_status_text(self) -> None:
        lines = []
        if self.web_publisher.running:
            ip = get_lan_ip()
            port = self.web_publisher.port
            lines.append(f"Web: http://{ip}:{port}/  (snapshot: /snapshot.jpg)")
        if self.rtsp_publisher.running:
            lines.append(f"RTSP: {self.rtsp_publisher.url}")
        self.stream_status_label.setText(
            "Stream: " + (" | ".join(lines) if lines else "Chua phat")
        )

    def _on_stream_status(self, text: str) -> None:
        """Nhan tin nhan tu thread publisher (qua Signal -> GUI thread)."""
        self.stream_status_label.setText(f"Stream: {text}")
        self._update_stream_buttons()

    # ------------------------------------------------- hook luong frame
    def on_frame_ready(self, frame) -> None:
        """Chay tren GUI thread. Ve len CameraView nhu cu, roi day ban sao
        (da scale) sang cac publisher. Frame goc dung de do KHONG bi doi."""
        super().on_frame_ready(frame)

        # Frame BGR goc, chua nen - dung cho Fiducial Service. Day la frame DA
        # lat guong boi CameraThread, tuc cung he toa do voi cu click chuot.
        self._last_bgr_frame = frame
        self._last_frame_shape = frame.shape[:2]

        if not self.is_streaming:
            return

        out_frame = scale_frame(frame, self._stream_max_width)

        if self.web_publisher.running:
            self.web_publisher.submit(out_frame)
        if self.rtsp_publisher.running:
            self.rtsp_publisher.submit(out_frame)

    def stop_camera(self) -> None:
        """Camera tat thi stream cung phai tat (khong con nguon frame)."""
        if self.is_streaming:
            self.stop_stream()
        self._last_frame_shape = None
        self._last_bgr_frame = None
        super().stop_camera()

    def closeEvent(self, event):  # noqa: N802 (ten do Qt quy dinh)
        self.rtsp_publisher.stop()
        self.web_publisher.stop()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    window = MergedMainWindowV7()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
