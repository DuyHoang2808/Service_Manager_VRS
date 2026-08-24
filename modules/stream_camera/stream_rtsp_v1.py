# pyrefly: ignore [missing-import]
import cv2
import subprocess
import logging
import time

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("RTSP_Streamer")

def main():
    # =========================
    # Cấu hình Camera & RTSP
    # =========================
    camera_index = 1
    width = 1920
    height = 1080
    fps = 30
    
    # Địa chỉ RTSP server (MediaMTX mặc định chạy port 8554)
    rtsp_url = "rtsp://localhost:8554/mystream"

    # =========================
    # Khởi tạo Camera
    # =========================
    logger.info("Đang mở camera Sony...")
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        logger.warning("CAP_DSHOW thất bại, thử CAP_MSMF...")
        cap = cv2.VideoCapture(camera_index, cv2.CAP_MSMF)
        
    if not cap.isOpened():
        logger.error("Không thể mở camera!")
        return

    # Thiết lập thông số
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Đọc thử kích thước thực tế
    actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS) or fps
    
    logger.info(f"Camera đã mở: {actual_width}x{actual_height} @ {actual_fps} FPS")

    # =========================
    # Khởi tạo FFmpeg Subprocess
    # =========================
    # Đường dẫn tuyệt đối tới file ffmpeg.exe
    ffmpeg_path = r"D:\Driver\ffmpeg-2026-05-06-git-f2e5eff3ff-essentials_build\bin\ffmpeg.exe"
    
    # Lệnh FFmpeg nhận dữ liệu RAW (bgr24) từ stdin và mã hóa H264 đẩy lên MediaMTX
    ffmpeg_cmd = [
        ffmpeg_path,
        '-y',                           # Ghi đè output (nếu có)
        '-f', 'rawvideo',               # Format đầu vào là video thô
        '-vcodec', 'rawvideo',
        '-pix_fmt', 'bgr24',            # Định dạng màu của OpenCV là BGR
        '-s', f"{actual_width}x{actual_height}", # Kích thước khung hình
        '-r', str(fps),                 # Tốc độ khung hình (FPS)
        '-i', '-',                      # Đọc từ tiêu chuẩn đầu vào (stdin)
        '-c:v', 'libx264',              # Mã hóa video H.264
        '-preset', 'ultrafast',         # Preset để encode cực nhanh, giảm độ trễ
        '-tune', 'zerolatency',         # Tối ưu cho độ trễ bằng 0
        '-pix_fmt', 'yuv420p',          # Pixel format đầu ra tương thích RTSP
        '-f', 'rtsp',                   # Format đầu ra là RTSP
        '-rtsp_transport', 'tcp',       # (Tùy chọn) Gửi qua TCP để ổn định hơn
        rtsp_url                        # Địa chỉ stream
    ]

    logger.info(f"Đang khởi động FFmpeg đẩy luồng tới: {rtsp_url}")
    
    try:
        # Bắt đầu chạy FFmpeg
        process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE)
    except FileNotFoundError:
        logger.error("Không tìm thấy lệnh 'ffmpeg'. Hãy đảm bảo bạn đã cài FFmpeg và thêm nó vào biến môi trường PATH.")
        cap.release()
        return

    # =========================
    # Vòng lặp Capture và Đẩy Stream
    # =========================
    frame_count = 0
    start_time = time.time()
    
    logger.info("Bắt đầu đẩy luồng RTSP (Bấm Ctrl+C để dừng)...")
    
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                logger.warning("Không đọc được khung hình")
                time.sleep(0.01)
                continue

            # Ghi trực tiếp frame (đã mã hóa byte thô) vào luồng stdin của FFmpeg
            process.stdin.write(frame.tobytes())

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
        logger.info("Đang dọn dẹp tài nguyên...")
        process.stdin.close()
        process.wait()
        cap.release()
        logger.info("Đã đóng kết nối thành công.")

if __name__ == '__main__':
    main()
