"""
CLI tiện ích: chạy tự động bù lệch board (thay thế bu_lech_board.py thủ công) bằng cách
gọi HTTP sang endpoint /api/calib/auto-board-offset của `gateway/plc_offset_gateway.py`.

Default cho --base-url/--plc-pc-ip/--plc-ip/--plc-port lấy từ file "cli_config.yaml"
cạnh file này (dùng chung với calibrate_camera_axis.py) - tự tạo với giá trị mặc định
nếu chưa có, và vẫn override được qua cờ dòng lệnh.

Cách chạy (board mới gá lên máy):
  python run_auto_board_offset.py --board-id BOARD-2026-07-14-001
  python run_auto_board_offset.py --anchor-mode 3   # nếu muốn dùng 3 điểm mốc thay vì 2
"""

from __future__ import annotations

import argparse
import json
import sys

import requests

from cli_config import CONFIG

Text = "CLI tiện ích: chạy tự động bù lệch board (thay thế bu_lech_board.py thủ công) bằng cách\r\n" \
    "gọi HTTP sang endpoint /api/calib/auto-board-offset của `gateway/plc_offset_gateway.py`.\r\n" \
    "Cách chạy (board mới gá lên máy):\r\n" \
    "python run_auto_board_offset.py --board-id BOARD-2026-07-14-001\r\n" \
    "python run_auto_board_offset.py --anchor-mode 3   # nếu muốn dùng 3 điểm mốc thay vì 2"

print(Text)

def main() -> None:
    parser = argparse.ArgumentParser(description="Tự động đo & tính bù lệch board bằng YOLO")
    parser.add_argument("--base-url", default=CONFIG["base_url"])
    parser.add_argument("--anchor-mode", type=int, default=2, choices=(2, 3),
                         help="2 điểm (A,C, mặc định) hoặc 3 điểm (A,C,D)")
    parser.add_argument("--board-id", default=None)
    parser.add_argument("--plc-pc-ip", default=CONFIG["plc_pc_ip"])
    parser.add_argument("--plc-ip", default=CONFIG["plc_ip"])
    parser.add_argument("--plc-port", type=int, default=CONFIG["plc_port"])
    args = parser.parse_args()

    payload = {
        "anchor_mode": args.anchor_mode,
        "board_id": args.board_id,
        "plc_pc_ip": args.plc_pc_ip,
        "plc_ip": args.plc_ip,
        "plc_port": args.plc_port,
    }

    print(f"--> Gọi {args.base_url}/api/calib/auto-board-offset ...")
    try:
        resp = requests.post(f"{args.base_url}/api/calib/auto-board-offset", json=payload, timeout=60)
    except requests.RequestException as e:
        print(f"❌ Không gọi được gateway: {e}")
        sys.exit(1)

    data = resp.json()
    print(f"\nHTTP {resp.status_code}")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if data.get("success"):
        print(f"\n✅ {data.get('message')}")
        print(f"   theta_deg={data.get('theta_deg'):+.4f}  tx={data.get('tx'):+.4f}  ty={data.get('ty'):+.4f}")
        print(f"   rms_error_mm={data.get('rms_error_mm'):.4f}  max_error_mm={data.get('max_error_mm'):.4f}")
        print(f"   Đã lưu vào: {data.get('offset_saved_path')}")
        if data.get("warning"):
            print(f"   {data.get('warning')}")
    else:
        print(f"\n❌ Thất bại: {data.get('message')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
