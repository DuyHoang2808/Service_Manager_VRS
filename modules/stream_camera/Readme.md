# Stream Camera Sony — RTSP

Stream video từ camera Sony qua giao thức RTSP sử dụng FFmpeg và MediaMTX.

---

## Yêu cầu hệ thống

| Thành phần | Phiên bản / Đường dẫn |
|---|---|
| Python | 3.10+ |
| FFmpeg | `D:\Driver\ffmpeg-...\bin\ffmpeg.exe` |
| MediaMTX | `D:\Driver\mediamtx_v1.18.1_windows_amd64\mediamtx.exe` |
| Virtual Env | `D:\Camera\Dev\Camera_venv` |

---

## Cấu trúc file

```
Stream_camera_rtsp/
├── Stream_camera_Sony.py        # ✅ Script chính (phiên bản tối ưu latency)
├── Stream_toiuudelay.py         # Backup tham khảo
├── Stream_camera_Sony_20260508.py  # Backup trước khi tối ưu
├── Run_stream_camSony.bat       # Script chạy tự động (MediaMTX + Stream)
├── Config/                      # Thư mục config YAML
└── Readme.md                    # File này
```

---

## Cách chạy

### Bước 1 — Khởi động MediaMTX (RTSP Server)

```bash
D:\Driver\mediamtx_v1.18.1_windows_amd64\mediamtx.exe
```

Hoặc dùng script tự động:

```bat
Run_stream_camSony.bat
```

> Script sẽ tự khởi động MediaMTX, đợi 5 giây rồi mở stream.

---

### Bước 2 — Chạy script stream

```bash
cd D:\Camera\Dev\Stream_camera\Stream_camera_rtsp
D:\Camera\Dev\Camera_venv\Scripts\python.exe Stream_camera_Sony.py
```

---

### Bước 3 — Xem stream

#### FFplay (khuyến nghị — độ trễ thấp nhất)

```bash
ffplay -fflags nobuffer -flags low_delay -framedrop -strict experimental rtsp://localhost:8554/mystream
```
Trên máy tính khác
```bash
ffplay -fflags nobuffer -flags low_delay -framedrop -strict experimental rtsp://192.168.10.163:8554/mystream
```

#### VLC Player

```bash
vlc rtsp://localhost:8554/mystream --network-caching=100 --live-caching=100
```

> Hoặc vào **Tools → Preferences → Input/Codecs → Network caching** → đặt `100ms`

---

## Tối ưu độ trễ

### Kiến trúc backend (2-Thread + Queue)

```
[Camera] ──cap.read()──► [CaptureThread] ──put_nowait()──► [Queue maxsize=1]
                                                                    │
                                                          [MainThread] ──get()──► [FFmpeg stdin]
```

- **CaptureThread** đọc frame liên tục, tự bỏ frame cũ khi queue đầy
- **MainThread** luôn lấy frame mới nhất → ghi ngay vào FFmpeg pipe
- `bufsize=0` trên pipe → không buffer thêm ở Python

### Tham số FFmpeg

| Tham số | Giá trị | Mục đích |
|---|---|---|
| `-preset` | `ultrafast` | Encode nhanh nhất |
| `-tune` | `zerolatency` | Tắt lookahead buffer |
| `-bf` | `0` | Không dùng B-frames |
| `-g` | `fps` (30) | Keyframe mỗi 1 giây |
| `-muxdelay` | `0` | Không delay ở muxer |
| `-flush_packets` | `1` | Flush ngay sau mỗi packet |
| `-max_delay` | `0` | Không buffer thêm |

### Kỳ vọng độ trễ

| Giai đoạn | Thời gian |
|---|---|
| Camera → OpenCV | ~33ms (1 frame @30fps) |
| OpenCV → FFmpeg encode | ~10–20ms (ultrafast) |
| FFmpeg → RTSP server | ~0ms (flush_packets=1) |
| Network (LAN) | ~1–5ms |
| Player buffer (FFplay) | ~50–100ms |
| **Tổng cộng** | **~100–160ms** |

---

## Lịch sử phiên bản

| Ngày | File | Thay đổi |
|---|---|---|
| 2026-05-08 | `Stream_camera_Sony.py` | Tối ưu latency: 2-thread, Queue(maxsize=1), FFmpeg params |
| 2026-05-08 | `Stream_toiuudelay.py` | Phiên bản tham khảo tối ưu delay |
| trước | `Stream_camera_Sony_20260508.py` | Backup bản gốc |