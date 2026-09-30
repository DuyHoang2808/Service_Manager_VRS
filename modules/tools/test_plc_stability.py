"""
test_plc_stability.py

CLI tiện ích: điều khiển PLC di chuyển liên tục qua 1 danh sách toạ độ cho sẵn (KHÔNG chụp
ảnh/AI - chỉ gọi /api/plc/move của gateway/plc_offset_gateway.py) để kiểm tra độ ổn định
phần cứng (PLC, driver, dây tín hiệu...) khi chạy lặp lại nhiều lần / thời gian dài.

Danh sách toạ độ đọc từ file JSON "test_positions.json" cạnh file này - tự tạo với vài điểm
mẫu nếu chưa có (sửa trực tiếp file JSON đó, KHÔNG cần sửa code), dạng:
[
  {"label": "P1", "x": 0.0, "y": 0.0},
  {"label": "P2", "x": 50.0, "y": 0.0}
]
(hoặc đơn giản hơn: [[0.0, 0.0], [50.0, 0.0]] cũng được, script tự đặt label P1, P2...)

Cách chạy:
  python test_plc_stability.py                       # chạy vô hạn, dừng bằng Ctrl+C
  python test_plc_stability.py --cycles 100           # chạy đúng 100 vòng (hết list = 1 vòng) rồi dừng
  python test_plc_stability.py --delay-ms 300         # nghỉ THÊM 300ms sau mỗi lệnh move (đã xác nhận xong)
  python test_plc_stability.py --positions-file D:\\duong_dan_khac.json
  python test_plc_stability.py --base-url http://192.168.3.50:8093
  python test_plc_stability.py --move-timeout-ms 3000 # override plc_move_timeout_ms gửi kèm
  python test_plc_stability.py --axis-speed-mm-s 60   # tốc độ trục thật, dùng để tính bù an toàn

An toàn chống ghi đè thanh ghi khi PLC chưa tới nơi:
  /api/plc/move là request ĐỒNG BỘ - gateway chỉ trả response sau khi wait_for_plc_position()
  xác nhận PLC đã dừng (đọc motion-status D466-469). Vì requests.post() ở đây là lệnh
  BLOCKING, script KHÔNG BAO GIỜ gửi lệnh move tiếp theo trước khi nhận được response của
  lệnh trước - --delay-ms chỉ là nghỉ THÊM sau khi đã xác nhận xong, không phải điều kiện
  để an toàn.

  Rủi ro thật chỉ xảy ra nếu PLC không kịp báo "đang chạy" (mất tín hiệu/wiring sai) khiến
  gateway rơi vào nhánh "silent fallback" và trả "success" SỚM HƠN thời gian di chuyển thực
  tế. Để phòng trường hợp này, script tự tính khoảng cách giữa 2 điểm liên tiếp, ước lượng
  thời gian di chuyển tối thiểu = distance / --axis-speed-mm-s, so với elapsed_s gateway
  thực trả về; nếu elapsed_s ngắn hơn ước lượng (đã nhân --safety-factor) thì tự NGHỦ BÙ
  thêm trước khi gửi lệnh kế tiếp, kèm cảnh báo trong log (cột expected_min_s/extra_wait_s).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Giong cac tool khac trong thu muc nay: dam bao stdout/stderr la UTF-8 de print()
# tieng Viet co dau khong nem UnicodeEncodeError (dac biet khi dong goi PyInstaller).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import requests

from cli_config import CONFIG

RUNTIME_DIR = Path(__file__).resolve().parent
POSITIONS_FILE = RUNTIME_DIR / "test_positions.json"
LOG_FILE = RUNTIME_DIR / "plc_stability_log.csv"

# CHI dung lam fallback khi KHONG lay duoc config that tu gateway (xem fetch_axis_speed_mm_s()
# ben duoi - mac dinh script tu GET /api/camera-config de luon khop plc_axis_speed_mm_per_s
# THAT dang cau hinh, tranh bi lech nhu khi ai do doi toc do trong
# plc_offset_gateway_config.json ma quen truyen lai --axis-speed-mm-s).
DEFAULT_AXIS_SPEED_MM_PER_S = 60.0
DEFAULT_SAFETY_FACTOR = 1.3


def fetch_axis_speed_mm_s(base_url: str) -> float:
    """Doc plc_axis_speed_mm_per_s THAT tu gateway dang chay (GET /api/camera-config) - day
    la nguon that duy nhat (GATEWAY_CONFIG), tranh script dung 1 default rieng bi lech voi
    config that moi luc ai do doi toc do truc trong plc_offset_gateway_config.json."""
    try:
        resp = requests.get(f"{base_url}/api/camera-config", timeout=5)
        resp.raise_for_status()
        speed = float(resp.json()["plc_axis_speed_mm_per_s"])
        print(f"--> Đọc plc_axis_speed_mm_per_s={speed:g}mm/s từ gateway ({base_url}/api/camera-config)")
        return speed
    except Exception as exc:
        print(
            f"⚠️  Không lấy được plc_axis_speed_mm_per_s từ gateway ({exc}) - "
            f"dùng fallback {DEFAULT_AXIS_SPEED_MM_PER_S}mm/s. Truyền --axis-speed-mm-s để tự set."
        )
        return DEFAULT_AXIS_SPEED_MM_PER_S

DEFAULT_POSITIONS: List[Dict[str, Any]] = [
    {"label": "P1", "x": 0.0, "y": 0.0},
    {"label": "P2", "x": 50.0, "y": 0.0},
    {"label": "P3", "x": 50.0, "y": 50.0},
    {"label": "P4", "x": 0.0, "y": 50.0},
]


def load_positions(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        path.write_text(json.dumps(DEFAULT_POSITIONS, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"⚠️  Chưa có {path.name}, đã tạo file mẫu với {len(DEFAULT_POSITIONS)} điểm test.\n"
            f"    Sửa toạ độ thật vào {path} rồi chạy lại script."
        )
        return DEFAULT_POSITIONS

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"{path} phải là 1 JSON list toạ độ, không được rỗng.")

    positions: List[Dict[str, Any]] = []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            x, y = item["x"], item["y"]
            label = item.get("label", f"P{i + 1}")
        else:
            x, y = item  # [x, y]
            label = f"P{i + 1}"
        positions.append({"label": label, "x": float(x), "y": float(y)})
    return positions


def send_move(base_url: str, x: float, y: float, timeout_ms: Optional[int]) -> Tuple[bool, int, float, str]:
    payload: Dict[str, Any] = {"x": x, "y": y}
    if timeout_ms is not None:
        payload["plc_move_timeout_ms"] = timeout_ms

    started = time.perf_counter()
    try:
        resp = requests.post(f"{base_url}/api/plc/move", json=payload, timeout=60)
        elapsed = time.perf_counter() - started
        try:
            body = resp.json()
        except ValueError:
            body = {}
        ok = resp.status_code == 200 and bool(body.get("success"))
        message = body.get("message") or body.get("detail") or resp.text[:200]
        return ok, resp.status_code, elapsed, message
    except requests.RequestException as exc:
        elapsed = time.perf_counter() - started
        return False, 0, elapsed, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test ổn định phần cứng PLC: di chuyển liên tục qua 1 list toạ độ cho sẵn"
    )
    parser.add_argument("--base-url", default=CONFIG["base_url"])
    parser.add_argument("--positions-file", default=str(POSITIONS_FILE))
    parser.add_argument(
        "--cycles", type=int, default=0,
        help="Số vòng lặp hết list (0 = chạy vô hạn tới khi Ctrl+C, mặc định)",
    )
    parser.add_argument("--delay-ms", type=int, default=0, help="Nghỉ giữa 2 lệnh move (ms)")
    parser.add_argument(
        "--move-timeout-ms", type=int, default=None,
        help="Override plc_move_timeout_ms gửi kèm mỗi request move "
             "(bỏ trống = dùng default cấu hình sẵn trong gateway)",
    )
    parser.add_argument(
        "--max-consecutive-errors", type=int, default=5,
        help="Dừng script nếu lỗi liên tiếp quá số này (an toàn, tránh spam khi PLC mất kết nối)",
    )
    parser.add_argument(
        "--axis-speed-mm-s", type=float, default=None,
        help="Tốc độ trục thật (mm/s) - dùng để TỰ ước lượng thời gian di chuyển tối thiểu "
             "giữa 2 điểm và cảnh báo/nghỉ bù nếu gateway báo xong đáng ngờ sớm. "
             "Bỏ trống (mặc định) = tự GET /api/camera-config để lấy plc_axis_speed_mm_per_s "
             "THẬT đang cấu hình trên gateway - chỉ cần set tay nếu muốn override riêng.",
    )
    parser.add_argument(
        "--safety-factor", type=float, default=DEFAULT_SAFETY_FACTOR,
        help=f"Hệ số nhân an toàn cho ước lượng thời gian di chuyển (mặc định {DEFAULT_SAFETY_FACTOR})",
    )
    args = parser.parse_args()

    if args.axis_speed_mm_s is None:
        args.axis_speed_mm_s = fetch_axis_speed_mm_s(args.base_url)

    positions = load_positions(Path(args.positions_file))
    cycles_desc = "vô hạn (Ctrl+C để dừng)" if args.cycles == 0 else str(args.cycles)
    print(
        f"--> {len(positions)} điểm, base_url={args.base_url}, số vòng={cycles_desc}, "
        f"axis_speed={args.axis_speed_mm_s:g}mm/s"
    )

    log_is_new = not LOG_FILE.exists()
    log_fh = LOG_FILE.open("a", newline="", encoding="utf-8")
    writer = csv.writer(log_fh)
    if log_is_new:
        writer.writerow(
            [
                "timestamp", "cycle", "label", "x", "y", "http_status", "elapsed_s",
                "success", "expected_min_s", "extra_wait_s", "message",
            ]
        )

    total = 0
    total_ok = 0
    total_fail = 0
    consecutive_errors = 0
    suspicious_early_count = 0
    elapsed_list: List[float] = []
    cycle = 0
    aborted = False
    last_xy: Optional[Tuple[float, float]] = None

    try:
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            for pos in positions:
                ok, status, elapsed, message = send_move(args.base_url, pos["x"], pos["y"], args.move_timeout_ms)
                total += 1
                elapsed_list.append(elapsed)
                ts = datetime.now().isoformat(timespec="seconds")

                # Uoc luong thoi gian di chuyen toi thieu tu vi tri TRUOC do (chi khi lenh
                # nay OK va co vi tri truoc do biet chac chan) - dung de phat hien gateway
                # bao "success" dang ngo som (xem docstring dau file: nhanh silent fallback).
                expected_min_s = 0.0
                extra_wait_s = 0.0
                if ok and last_xy is not None and args.axis_speed_mm_s > 0:
                    distance_mm = math.hypot(pos["x"] - last_xy[0], pos["y"] - last_xy[1])
                    expected_min_s = distance_mm / args.axis_speed_mm_s
                    required_s = expected_min_s * args.safety_factor
                    if elapsed < required_s:
                        extra_wait_s = required_s - elapsed
                        suspicious_early_count += 1
                        print(
                            f"    ⚠️  Gateway báo xong sau {elapsed:.2f}s nhưng quãng đường "
                            f"{distance_mm:.1f}mm cần tối thiểu ~{expected_min_s:.2f}s @ "
                            f"{args.axis_speed_mm_s:g}mm/s -> nghỉ bù thêm {extra_wait_s:.2f}s "
                            f"trước khi gửi lệnh tiếp (có thể PLC báo motion-status sai/chậm)."
                        )
                        time.sleep(extra_wait_s)

                if ok:
                    total_ok += 1
                    consecutive_errors = 0
                    last_xy = (pos["x"], pos["y"])
                    print(f"[{cycle}] {pos['label']} ({pos['x']:.3f},{pos['y']:.3f}) OK HTTP {status} {elapsed:.2f}s")
                else:
                    total_fail += 1
                    consecutive_errors += 1
                    # Khong biet PLC that su dang o dau khi lenh loi -> bo last_xy de lan sau
                    # khong tinh khoang cach sai tu 1 vi tri khong chac chan.
                    last_xy = None
                    print(
                        f"[{cycle}] {pos['label']} ({pos['x']:.3f},{pos['y']:.3f}) LỖI "
                        f"HTTP {status} {elapsed:.2f}s - {message}"
                    )

                writer.writerow(
                    [
                        ts, cycle, pos["label"], pos["x"], pos["y"], status, f"{elapsed:.3f}",
                        ok, f"{expected_min_s:.3f}", f"{extra_wait_s:.3f}", message,
                    ]
                )
                log_fh.flush()

                if consecutive_errors >= args.max_consecutive_errors:
                    print(f"❌ Dừng: {consecutive_errors} lỗi liên tiếp (PLC có thể mất kết nối/hỏng).")
                    aborted = True
                    break

                if args.delay_ms > 0:
                    time.sleep(args.delay_ms / 1000.0)

            if aborted:
                break
            print(f"--- Hết vòng {cycle}: {total_ok} OK / {total_fail} lỗi / {total} tổng ---")

    except KeyboardInterrupt:
        print("\n⏹️  Dừng theo yêu cầu (Ctrl+C).")
    finally:
        log_fh.close()
        avg = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0.0
        mx = max(elapsed_list) if elapsed_list else 0.0
        mn = min(elapsed_list) if elapsed_list else 0.0
        print(
            f"\n===== TỔNG KẾT =====\n"
            f"Tổng lệnh move          : {total}\n"
            f"Thành công              : {total_ok}\n"
            f"Lỗi                     : {total_fail}\n"
            f"Nghi báo xong quá sớm   : {suspicious_early_count} (xem cột extra_wait_s trong log)\n"
            f"Elapsed avg/min/max (s) : {avg:.2f} / {mn:.2f} / {mx:.2f}\n"
            f"Log chi tiết            : {LOG_FILE}\n"
        )

    if aborted:
        sys.exit(1)


if __name__ == "__main__":
    main()
