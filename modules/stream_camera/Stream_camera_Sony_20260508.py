# pyrefly: ignore [missing-import]
"""
Stream_camera_Sony_20260508.py
--------------------------------------------------------------------
Đọc camera Sony bằng OpenCV -> đẩy luồng RTSP qua FFmpeg, đồng thời mở
HTTP endpoint /snapshot cho gateway lấy ảnh.

Bản cập nhật: mượn các cải tiến đã kiểm chứng của
`AutoBoardOffset_YOLO_2Mat/gui/Do_khoang_cach_kem_camera_v7.py`:

  1) GOP 1 giây (-g fps -keyint_min fps). Trước đây không đặt -g nên
     x264 dùng mặc định 250 frame = 8,33 s/keyframe; client RTSP phải
     đợi keyframe kế tiếp mới hiện hình. Đo thực tế: 8,33 s -> 1,00 s.
  2) Ghi FFmpeg ở thread riêng qua Queue(maxsize=1). Trước đây ghi thẳng
     trong vòng lặp capture nên FFmpeg nghẽn pipe là chặn luôn việc đọc
     camera, tụt FPS và trễ dồn lại.
  3) Bắt tay TCP tới cổng RTSP trước khi gọi FFmpeg -> báo ngay "MediaMTX
     đã chạy chưa?" thay vì để FFmpeg treo im lặng.
  4) Watchdog phát hiện FFmpeg treo giữa chừng + đọc stderr FFmpeg và lọc
     ra dòng lỗi thật sự.
  5) Cờ giảm đệm ở muxer: -muxdelay 0 -muxpreload 0 -max_delay 0
     -flush_packets 1.

QUAN TRỌNG - lệnh FFmpeg thực chạy lấy từ file YAML dùng chung
(`Stream_cameras_configs.yml` cạnh thư mục Dev), vì `save_to_yaml_v2()`
nạp đè YAML lên giá trị mặc định trong code. Sửa mỗi code là KHÔNG đủ:
phải sửa `ffmpeg_cmd` trong YAML, hoặc xoá khối đó đi để code ghi lại.
Khi khởi động, chương trình tự cảnh báo nếu YAML còn là bản cũ.

Chạy:
    python Stream_camera_Sony_20260508.py
(Cần MediaMTX chạy trước, ví dụ
 D:\\Driver\\mediamtx_v1.18.1_windows_amd64\\mediamtx.exe)
"""

import cv2
import queue
import socket
import subprocess
import logging
import sys
import time
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from Config.ParamsBase import tactParametters

# Khi dong goi bang PyInstaller, bootloader khong ton trong PYTHONIOENCODING
# nhu python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8 tai code,
# neu khong logger in tieng Viet co dau se nem UnicodeEncodeError va crash exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("RTSP_Streamer")

# Nếu RTSP server không bắt tay được, FFmpeg KHÔNG báo lỗi mà treo im lặng.
# Nó vẫn nuốt vài frame đầu để probe input rồi mới kẹt, nên "ghi được frame
# đầu tiên" KHÔNG phải tín hiệu sống. Cảnh giác dựa trên độ lệch giữa lúc
# nhận frame và lúc ghi được frame: quá ngưỡng này coi như FFmpeg chết đứng.
RTSP_STALL_TIMEOUT = 6.0

# Từ khoá lọc dòng stderr đang là lỗi thật sự - FFmpeg in rất nhiều dòng
# banner/thông tin encoder, đưa nguyên dòng cuối ra thì vô nghĩa.
_ERROR_HINTS = (
    "error", "failed", "refused", "timed out", "timeout", "invalid",
    "unable", "no route", "not found", "denied", "unauthorized",
    "broken pipe", "conversion failed", "connection",
)


class CameraConfig(tactParametters):
    def __init__(self, ModuleName="CameraSonyConfig"):
        super().__init__(ModuleName=ModuleName)
        # =========================
        # Cấu hình mặc định
        # =========================
        self.camera_index = 0
        self.width = 1920
        self.height = 1080
        self.fps = 30
        self.rtsp_url = "rtsp://localhost:8554/mystream"
        self.snapshot_host = "0.0.0.0"
        self.snapshot_port = 8001
        self.ffmpeg_path = r"D:\Driver\ffmpeg-2026-05-06-git-f2e5eff3ff-essentials_build\bin\ffmpeg.exe"

        # Lật ngang khung hình (giữ nguyên hành vi cũ)
        self.flip_horizontal = True

        # Định dạng ảnh trả về ở /snapshot. Mặc định PNG (không nén mất mát)
        # vì ảnh này dùng để đo pixel -> mm. Đổi sang "jpg" thì nhanh hơn ~10
        # lần nhưng là nén mất mát. Endpoint /snapshot.png và /snapshot.jpg
        # luôn trả đúng định dạng trong tên bất kể giá trị này.
        self.snapshot_format = "png"
        self.snapshot_jpeg_quality = 95

        # {gop} được thay bằng số frame giữa 2 keyframe (= fps -> 1 giây)
        self.ffmpeg_cmd = [
                            "{ffmpeg_path}",
                            "-y",
                            # ---- Input: raw frame từ stdin ----
                            "-f", "rawvideo",
                            "-vcodec", "rawvideo",
                            "-pix_fmt", "bgr24",
                            "-s", "{actual_width}x{actual_height}",
                            "-r", "{fps}",
                            "-i", "-",
                            # ---- Encoder: tối ưu độ trễ ----
                            "-c:v", "libx264",
                            "-preset", "ultrafast",
                            "-tune", "zerolatency",
                            "-crf", "23",
                            # GOP: 1 keyframe/giây -> client vào xem nhanh
                            "-g", "{gop}",
                            "-keyint_min", "{gop}",
                            "-bf", "0",
                            "-profile:v", "baseline",
                            "-pix_fmt", "yuv420p",
                            "-x264-params", "nal-hrd=cbr:force-cfr=1",
                            # ---- Output RTSP: giảm đệm tối đa ----
                            "-f", "rtsp",
                            "-rtsp_transport", "tcp",
                            "-muxdelay", "0",
                            "-muxpreload", "0",
                            "-max_delay", "0",
                            "-flush_packets", "1",
                            "{rtsp_url}"
                        ]

        # Tải/Lưu cấu hình vào file YAML
        self.save_to_yaml_v2(ModuleName=ModuleName)


class SonyCameraStreamer:
    def __init__(self, config: CameraConfig = None):
        if config is None:
            self.config = CameraConfig()
        else:
            self.config = config
        self.cap = None
        self.process = None
        self.latest_frame = None
        self.frame_lock = threading.Lock()
        self.http_server = None
        self.http_thread = None

        # Hàng đợi 1 khe: luôn giữ frame mới nhất, frame cũ bị bỏ.
        # Nhờ vậy FFmpeg chậm cũng không bao giờ chặn vòng đọc camera.
        self._frame_queue: "queue.Queue" = queue.Queue(maxsize=1)
        self._stop_event = threading.Event()
        self._writer_thread = None
        self._stderr_thread = None
        self._watchdog_thread = None
        self._last_submit_ts = 0.0
        self._last_write_ts = 0.0
        self._stderr_tail = []
        self._stderr_lock = threading.Lock()
        self._ffmpeg_alive = False

    # =====================================================================
    # Snapshot
    # =====================================================================
    def _encode_latest(self, fmt: str) -> Optional[bytes]:
        with self.frame_lock:
            frame = None if self.latest_frame is None else self.latest_frame.copy()

        if frame is None:
            return None

        if fmt == "jpg":
            ok, buf = cv2.imencode(
                ".jpg", frame,
                [cv2.IMWRITE_JPEG_QUALITY, int(self.config.snapshot_jpeg_quality)]
            )
        else:
            ok, buf = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 3])

        return buf.tobytes() if ok else None

    def get_snapshot_png(self) -> Optional[bytes]:
        """Giữ lại cho code cũ đang gọi tên hàm này."""
        return self._encode_latest("png")

    def start_snapshot_server(self):
        streamer = self

        class SnapshotHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]

                if path in ("/snapshot", "/snapshot.png", "/snapshot.jpg"):
                    if path == "/snapshot.jpg":
                        fmt = "jpg"
                    elif path == "/snapshot.png":
                        fmt = "png"
                    else:
                        fmt = "jpg" if str(streamer.config.snapshot_format).lower() in ("jpg", "jpeg") else "png"
                else:
                    self.send_response(404)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"not_found"}')
                    return

                image_bytes = streamer._encode_latest(fmt)
                if image_bytes is None:
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"snapshot_not_ready"}')
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg" if fmt == "jpg" else "image/png")
                self.send_header("Content-Length", str(len(image_bytes)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.end_headers()
                self.wfile.write(image_bytes)

            def log_message(self, format, *args):
                return

        self.http_server = ThreadingHTTPServer(
            (self.config.snapshot_host, self.config.snapshot_port),
            SnapshotHandler
        )
        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            daemon=True
        )
        self.http_thread.start()
        logger.info(
            f"Snapshot endpoint ready: http://{self.config.snapshot_host}:{self.config.snapshot_port}/snapshot"
            f" (mặc định {str(self.config.snapshot_format).upper()}, còn có /snapshot.png và /snapshot.jpg)"
        )

    # =====================================================================
    # FFmpeg
    # =====================================================================
    @staticmethod
    def probe_rtsp_endpoint(url: str, timeout: float = 2.0):
        """Bắt tay TCP tới host:port của URL RTSP trước khi gọi FFmpeg.

        Bắt sớm trường hợp hay gặp nhất: quên chạy MediaMTX. Nếu để FFmpeg
        tự xử lý thì nó treo im lặng chứ không báo lỗi.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 554
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, ""
        except OSError as exc:
            reason = getattr(exc, "strerror", None) or str(exc)
            return False, f"không kết nối được tới {host}:{port} ({reason})"

    def _build_ffmpeg_cmd(self, actual_width: int, actual_height: int):
        gop = max(1, int(self.config.fps))
        formatted_cmd = []
        for arg in self.config.ffmpeg_cmd:
            formatted_cmd.append(arg.format(
                ffmpeg_path=self.config.ffmpeg_path,
                actual_width=actual_width,
                actual_height=actual_height,
                fps=self.config.fps,
                gop=gop,
                rtsp_url=self.config.rtsp_url,
            ))
        return formatted_cmd

    def _warn_if_config_outdated(self):
        """YAML nạp đè code. Nếu YAML còn là bản cũ thì mọi cải tiến ở đây vô hiệu."""
        cmd = [str(a) for a in self.config.ffmpeg_cmd]
        thieu = [c for c in ("-g", "-flush_packets") if c not in cmd]
        if thieu:
            logger.warning(
                "ffmpeg_cmd trong Stream_cameras_configs.yml đang là BẢN CŨ "
                f"(thiếu {', '.join(thieu)}). Client sẽ vào xem chậm (~8s). "
                "Hãy xoá khối 'ffmpeg_cmd' trong file YAML đó rồi chạy lại để "
                "chương trình ghi lại tham số mới."
            )

    def _stderr_loop(self):
        proc = self.process
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

    def last_ffmpeg_error(self) -> str:
        with self._stderr_lock:
            for text in reversed(self._stderr_tail):
                low = text.lower()
                if any(hint in low for hint in _ERROR_HINTS):
                    return text
        return ""

    def _writer_loop(self):
        """Lấy frame mới nhất trong hàng đợi và ghi vào stdin FFmpeg."""
        while not self._stop_event.is_set():
            try:
                frame = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if frame is None:
                break

            try:
                self.process.stdin.write(frame.tobytes())
                self._last_write_ts = time.time()
            except (BrokenPipeError, OSError, ValueError) as e:
                detail = self.last_ffmpeg_error()
                logger.error(f"FFmpeg đã đóng pipe: {e}. {detail}")
                self._ffmpeg_alive = False
                self._stop_event.set()
                return

    def _watchdog_loop(self):
        """Chỉ báo động khi ta CÓ frame để gửi mà mãi không ghi được -
        nên camera ngừng phát thì không báo nhầm."""
        while not self._stop_event.wait(1.0):
            starved = self._last_submit_ts - self._last_write_ts
            if starved <= RTSP_STALL_TIMEOUT:
                continue

            detail = self.last_ffmpeg_error()
            logger.error(
                f"FFmpeg không nuốt frame trong {starved:.0f}s - dừng phát. "
                + (detail or "Kiểm tra MediaMTX và đường dẫn RTSP.")
            )
            self._ffmpeg_alive = False
            self._stop_event.set()
            return

    def _submit_frame(self, frame):
        """Bỏ frame cũ chưa kịp gửi, đẩy frame mới nhất vào."""
        self._last_submit_ts = time.time()
        if self._frame_queue.full():
            try:
                self._frame_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            pass

    # =====================================================================
    # Vòng đời
    # =====================================================================
    def start(self):
        self._warn_if_config_outdated()

        # =========================
        # Khởi tạo Camera
        # =========================
        logger.info("Đang mở camera Sony...")
        self.cap = cv2.VideoCapture(self.config.camera_index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            logger.warning("CAP_DSHOW thất bại, thử CAP_MSMF...")
            self.cap = cv2.VideoCapture(self.config.camera_index, cv2.CAP_MSMF)

        if not self.cap.isOpened():
            logger.error("Không thể mở camera!")
            return

        # Thiết lập thông số
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.config.fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Đọc thử kích thước thực tế
        actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS) or self.config.fps

        logger.info(f"Camera đã mở: {actual_width}x{actual_height} @ {actual_fps} FPS")

        # =========================
        # Kiểm tra RTSP server trước
        # =========================
        reachable, reason = self.probe_rtsp_endpoint(self.config.rtsp_url)
        if not reachable:
            logger.error(f"RTSP: {reason}. MediaMTX đã chạy chưa?")
            self.cap.release()
            return

        # =========================
        # Khởi tạo FFmpeg Subprocess
        # =========================
        formatted_cmd = self._build_ffmpeg_cmd(actual_width, actual_height)
        gop = max(1, int(self.config.fps))
        logger.info(f"Đang khởi động FFmpeg đẩy luồng tới: {self.config.rtsp_url}")
        logger.info(f"GOP = {gop} frame ({gop / max(1, self.config.fps):.1f}s/keyframe)")

        try:
            self.process = subprocess.Popen(
                formatted_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,  # không đệm thêm: ghi thẳng
            )
        except FileNotFoundError:
            logger.error(
                "Không tìm thấy lệnh 'ffmpeg'. Hãy đảm bảo bạn đã cài FFmpeg "
                "và thêm nó vào biến môi trường PATH."
            )
            self.cap.release()
            return

        self._ffmpeg_alive = True
        self._stop_event.clear()
        self._last_submit_ts = self._last_write_ts = time.time()

        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="FfmpegStderr", daemon=True)
        self._stderr_thread.start()

        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="FfmpegWriter", daemon=True)
        self._writer_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name="FfmpegWatchdog", daemon=True)
        self._watchdog_thread.start()

        # =========================
        # Vòng lặp Capture
        # =========================
        frame_count = 0
        start_time = time.time()

        logger.info("Bắt đầu đẩy luồng RTSP (Bấm Ctrl+C để dừng)...")

        try:
            while not self._stop_event.is_set():
                ret, frame = self.cap.read()
                if not ret:
                    logger.warning("Không đọc được khung hình")
                    time.sleep(0.01)
                    continue

                if self.config.flip_horizontal:
                    frame = cv2.flip(frame, 1)

                with self.frame_lock:
                    self.latest_frame = frame.copy()

                # Không ghi thẳng vào FFmpeg nữa: đẩy sang thread ghi riêng
                self._submit_frame(frame)

                # Tính toán và in FPS sau mỗi 5 giây
                frame_count += 1
                elapsed = time.time() - start_time
                if elapsed >= 5.0:
                    current_fps = frame_count / elapsed
                    logger.info(f"Đang đẩy RTSP FPS: {current_fps:.2f}")
                    start_time = time.time()
                    frame_count = 0

        except KeyboardInterrupt:
            logger.info("Đã nhận lệnh dừng từ bàn phím.")
        except Exception as e:
            logger.error(f"Lỗi trong vòng lặp capture: {e}")
        finally:
            self.cleanup()

    def cleanup(self):
        logger.info("Đang dọn dẹp tài nguyên...")
        self._stop_event.set()

        # Đánh thức thread ghi để nó thoát ngay
        try:
            self._frame_queue.put_nowait(None)
        except queue.Full:
            pass

        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=2.0)

        if self.http_server:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception as e:
                logger.error(f"Lỗi khi đóng snapshot server: {e}")

        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg không tự thoát - buộc dừng.")
                self.process.kill()
            except Exception as e:
                logger.error(f"Lỗi khi đóng FFmpeg process: {e}")

        if self.cap:
            self.cap.release()
        logger.info("Đã đóng kết nối thành công.")


if __name__ == '__main__':
    streamer = SonyCameraStreamer()
    streamer.start_snapshot_server()
    streamer.start()
