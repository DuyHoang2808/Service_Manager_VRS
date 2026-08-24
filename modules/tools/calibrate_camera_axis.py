"""
CLI tiện ích: hiệu chỉnh ma trận trục camera<->máy T (chạy 1 LẦN khi setup máy hoặc sau
khi tháo/lắp lại camera - KHÔNG chạy lại mỗi khi đổi board).

Chỉ là 1 lớp mỏng gọi HTTP sang endpoint /api/calib/camera-axis của
`gateway/plc_offset_gateway.py` (toàn bộ logic thật nằm ở gateway, không lặp lại ở đây).

Yêu cầu trước khi chạy:
  1. gateway/run_gateway.py đang chạy (mặc định http://localhost:8093)
  2. fiducial_detector/run_fiducial_service.py đang chạy VÀ đã có file .pt (mock mode sẽ báo lỗi)
  3. PLC + camera thật đã kết nối (script này sẽ DI CHUYỂN MÁY THẬT)

Default cho --base-url/--plc-pc-ip/--plc-ip/--plc-port lấy từ file "cli_config.yaml"
cạnh file này (dùng chung với run_auto_board_offset.py) - tự tạo với giá trị mặc định
nếu chưa có, và vẫn override được qua cờ dòng lệnh.

Cách chạy:
  python calibrate_camera_axis.py --anchor A --delta-mm 5.0
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

from cli_config import CONFIG

Text = "CLI tiện ích: hiệu chỉnh ma trận trục camera<->máy T (chạy 1 LẦN khi setup máy hoặc sau \r\n" \
    "khi tháo/lắp lại camera - KHÔNG chạy lại mỗi khi đổi board).\r\n" \
    "Chỉ là 1 lớp mỏng gọi HTTP sang endpoint /api/calib/camera-axis của\r\n" \
    "`gateway/plc_offset_gateway.py` (toàn bộ logic thật nằm ở gateway, không lặp lại ở đây).\r\n" \
    "Yêu cầu trước khi chạy:\r\n" \
    "1. gateway/run_gateway.py đang chạy (mặc định http://localhost:8093)\r\n" \
    "2. fiducial_detector/run_fiducial_service.py đang chạy VÀ đã có file .pt (mock mode sẽ báo lỗi)\r\n" \
    "3. PLC + camera thật đã kết nối (script này sẽ DI CHUYỂN MÁY THẬT)\r\n" \
    "Cách chạy:\r\n" \
    "python calibrate_camera_axis.py --anchor A --delta-mm 5.0"
print(Text)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hiệu chỉnh ma trận trục camera<->máy T")
    parser.add_argument("--base-url", default=CONFIG["base_url"], help="URL gateway (mặc định %(default)s)")
    parser.add_argument("--anchor", default="A", help="Tên điểm mốc dùng làm gốc P0 (mặc định A)")
    parser.add_argument("--delta-mm", type=float, default=1.0, help="Bước dịch chuyển thử nghiệm (mm), mặc định 5.0")
    parser.add_argument("--plc-pc-ip", default=CONFIG["plc_pc_ip"])
    parser.add_argument("--plc-ip", default=CONFIG["plc_ip"])
    parser.add_argument("--plc-port", type=int, default=CONFIG["plc_port"])
    args = parser.parse_args()

    payload = {
        "anchor_name": args.anchor,
        "delta_mm": args.delta_mm,
        "plc_pc_ip": args.plc_pc_ip,
        "plc_ip": args.plc_ip,
        "plc_port": args.plc_port,
    }

    try:
        info_resp = requests.get(
            f"{args.base_url}/api/calib/anchor-info",
            params={"anchor_name": args.anchor}, timeout=10,
        )
        info = info_resp.json()
        board_xy = info.get("board_xy")
        plc_xy = info.get("plc_expected_xy")
        print(f"📍 anchor_name='{args.anchor}' -> Board=({board_xy[0]:.3f}, {board_xy[1]:.3f}) mm", end="")
        if plc_xy:
            print(f" -> PLC kỳ vọng=({plc_xy[0]:.3f}, {plc_xy[1]:.3f})")
        else:
            print(f" (không suy ra được PLC: {info.get('plc_expected_xy_error')})")
    except requests.RequestException as e:
        print(f"⚠️  Không tra được toạ độ anchor '{args.anchor}': {e}")

    print(f"\n--> Gọi {args.base_url}/api/calib/camera-axis với payload:")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("\n⚠️  MÁY SẼ DI CHUYỂN THẬT (3 lần: P0, P0+dX, P0+dY). Nhấn Enter để tiếp tục, Ctrl+C để huỷ.")
    input()

    try:
        resp = requests.post(f"{args.base_url}/api/calib/camera-axis", json=payload, timeout=60)
    except requests.RequestException as e:
        print(f"❌ Không gọi được gateway: {e}")
        sys.exit(1)

    print(f"\nHTTP {resp.status_code}")
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))

    data = resp.json()
    if data.get("success"):
        print("\n✅ Đã hiệu chỉnh xong, ma trận T đã được lưu vào plc_offset_gateway_config.json")
        print(f"   camera_axis_matrix = {data.get('camera_axis_matrix')}")
        print(f"   {data.get('sanity_check')}")
    else:
        print(f"\n❌ Hiệu chỉnh thất bại: {data.get('message')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
