# pyrefly: ignore [missing-import]
import cv2
import subprocess
import logging
import queue
import time
import threading
from Config.ParamsBase import tactParametters

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("RTSP_Streamer")


class CameraConfig(tactParametters):
    def __init__(self, ModuleName="CameraSonyConfig"):
        super().__init__(ModuleName=ModuleName)

        # Camera
        self.camera_index = 1
        self.width = 1920
        self.height = 1080
        self.fps = 30

        # RTSP output
        self.rtsp_url = "rtsp://localhost:8554/mystream"

        # FFmpeg path
        self.ffmpeg_path = (
            r"D:\Driver\ffmpeg-2026-05-06-git-f2e5eff3ff-essentials_build\bin\ffmpeg.exe"
        )

        # Save config
        self.save_to_yaml_v2(ModuleName=ModuleName)


class SonyCameraStreamer:
    def __init__(self, config: CameraConfig = None):
        self.config = config if config else CameraConfig()
        self.cap = None
        self.process = None
        self.stderr_thread = None
        self.capture_thread = None
        self.running = False

        # ✅ Queue maxsize=1: luôn giữ frame MỚI NHẤT, bỏ frame cũ
        self.frame_queue = queue.Queue(maxsize=1)

    def build_ffmpeg_cmd(self, actual_width, actual_height):
        # ✅ GOP nhỏ = keyframe xuất hiện thường xuyên hơn → giảm trễ decode
        gop = self.config.fps  # 1 keyframe mỗi giây (thay vì 2s)

        return [
            self.config.ffmpeg_path,
            "-y",

            # =========================
            # Input raw video
            # =========================
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{actual_width}x{actual_height}",
            "-r", str(self.config.fps),
            "-i", "-",

            # =========================
            # Encoder - tối ưu latency
            # =========================
            "-c:v", "libx264",
            "-preset", "ultrafast",       # encode nhanh nhất
            "-tune", "zerolatency",       # tắt lookahead buffer
            "-crf", "23",
            "-g", str(gop),               # keyframe mỗi 1s
            "-keyint_min", str(gop),      # đồng bộ với GOP
            "-bf", "0",                   # tắt B-frames
            "-profile:v", "baseline",     # baseline không dùng B-frames
            "-pix_fmt", "yuv420p",
            "-x264-params", "nal-hrd=cbr:force-cfr=1",  # ổn định bitrate

            # =========================
            # RTSP output - giảm buffer tối đa
            # =========================
            "-f", "rtsp",
            "-rtsp_transport", "tcp",
            "-muxdelay", "0",             # ✅ không delay ở muxer
            "-muxpreload", "0",           # ✅ không preload
            "-max_delay", "0",            # ✅ không buffer thêm
            "-flush_packets", "1",        # ✅ flush ngay sau mỗi packet
            self.config.rtsp_url
        ]

    def read_ffmpeg_stderr(self):
        """Read FFmpeg stderr continuously"""
        try:
            while self.running and self.process:
                line = self.process.stderr.readline()
                if not line:
                    break

                decoded = line.decode(errors="ignore").strip()
                if decoded:
                    logger.error(f"FFmpeg: {decoded}")
        except Exception as e:
            logger.error(f"Lỗi đọc stderr FFmpeg: {e}")

    def _capture_loop(self):
        """
        ✅ Thread riêng: liên tục đọc frame từ camera.
        Nếu queue đầy (frame cũ chưa được xử lý), bỏ frame cũ và
        đẩy frame mới vào → luôn giữ frame MỚI NHẤT trong queue.
        """
        logger.info("[CaptureThread] Bắt đầu đọc frame từ camera...")
        while self.running:
            ret, frame = self.cap.read()

            if not ret:
                logger.warning("[CaptureThread] Không đọc được frame")
                time.sleep(0.005)
                continue

            # Nếu queue đầy → bỏ frame cũ, đẩy frame mới vào
            if self.frame_queue.full():
                try:
                    self.frame_queue.get_nowait()   # bỏ frame cũ
                except Exception:
                    pass

            try:
                self.frame_queue.put_nowait(frame)  # đẩy frame mới
            except Exception:
                pass

        logger.info("[CaptureThread] Đã dừng.")

    def start(self):
        logger.info("Đang mở camera Sony...")

        # Try DirectShow first
        self.cap = cv2.VideoCapture(self.config.camera_index, cv2.CAP_DSHOW)

        if not self.cap.isOpened():
            logger.warning("CAP_DSHOW thất bại, thử CAP_MSMF...")
            self.cap = cv2.VideoCapture(self.config.camera_index, cv2.CAP_MSMF)

        if not self.cap.isOpened():
            logger.error("Không thể mở camera!")
            return

        # Camera settings
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.config.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.config.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.config.fps)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # ✅ buffer tối thiểu = 1 frame

        # Read actual config
        actual_width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)

        if actual_fps <= 0:
            actual_fps = self.config.fps

        logger.info(
            f"Camera đã mở: {actual_width}x{actual_height} @ {actual_fps} FPS"
        )

        ffmpeg_cmd = self.build_ffmpeg_cmd(actual_width, actual_height)

        logger.info(f"Đang khởi động FFmpeg tới: {self.config.rtsp_url}")
        logger.info("FFmpeg command:")
        logger.info(" ".join(ffmpeg_cmd))

        try:
            self.process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0          # ✅ unbuffered pipe = ghi thẳng, không buffer thêm
            )
        except FileNotFoundError:
            logger.error("Không tìm thấy ffmpeg.exe")
            self.cap.release()
            return
        except Exception as e:
            logger.error(f"Lỗi khởi động FFmpeg: {e}")
            self.cap.release()
            return

        self.running = True

        # ✅ Thread 1: đọc stderr FFmpeg
        self.stderr_thread = threading.Thread(
            target=self.read_ffmpeg_stderr,
            daemon=True
        )
        self.stderr_thread.start()

        # ✅ Thread 2: capture frame từ camera liên tục
        self.capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True
        )
        self.capture_thread.start()

        logger.info("Bắt đầu stream RTSP (Ctrl+C để dừng)...")

        frame_count = 0
        start_time = time.time()

        try:
            while True:
                # Check FFmpeg alive
                if self.process.poll() is not None:
                    logger.error(
                        f"FFmpeg đã dừng với mã lỗi: {self.process.returncode}"
                    )
                    break

                # ✅ Lấy frame mới nhất từ queue (timeout 0.1s)
                try:
                    frame = self.frame_queue.get(timeout=0.1)
                except Exception:
                    # Không có frame trong 100ms → cảnh báo
                    logger.warning("Không nhận được frame từ camera (timeout)")
                    continue

                # ✅ Ghi frame thẳng vào FFmpeg stdin (unbuffered)
                try:
                    self.process.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    logger.error("FFmpeg pipe đã bị đóng")
                    break
                except OSError as e:
                    logger.error(f"Lỗi ghi frame vào FFmpeg: {e}")
                    break

                frame_count += 1
                elapsed = time.time() - start_time

                if elapsed >= 5:
                    fps = frame_count / elapsed
                    logger.info(
                        f"Streaming FPS: {fps:.2f} | Queue size: {self.frame_queue.qsize()}"
                    )
                    frame_count = 0
                    start_time = time.time()

        except KeyboardInterrupt:
            logger.info("Dừng bởi người dùng")

        except Exception as e:
            logger.error(f"Lỗi runtime: {e}")

        finally:
            self.cleanup()

    def cleanup(self):
        logger.info("Đang dọn dẹp tài nguyên...")
        self.running = False

        if self.cap:
            try:
                self.cap.release()
            except:
                pass

        if self.process:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
            except:
                pass

            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except:
                try:
                    self.process.kill()
                except:
                    pass

        logger.info("Đã đóng kết nối thành công.")


if __name__ == "__main__":
    streamer = SonyCameraStreamer()
    streamer.start()