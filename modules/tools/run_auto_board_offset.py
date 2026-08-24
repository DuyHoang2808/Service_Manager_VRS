"""
CLI tiện ích: chạy tự động bù lệch board (thay thế bu_lech_board.py thủ công) bằng cách
gọi HTTP sang endpoint /api/calib/auto-board-offset của `gateway/plc_offset_gateway.py`.

Hỗ trợ board 2 mặt (PA2): dùng --board-side A hoặc --board-side B.

Default cho --base-url/--plc-pc-ip/--plc-ip/--plc-port lấy từ file "cli_config.yaml"
cạnh file này (dùng chung với calibrate_camera_axis.py) - tự tạo với giá trị mặc định
nếu chưa có, và vẫn override được qua cờ dòng lệnh.

Cách chạy:
  python run_auto_board_offset.py --board-id BOARD-001
  python run_auto_board_offset.py --board-id BOARD-001 --board-side B
  python run_auto_board_offset.py --board-id BOARD-001 --board-side B --anchor-mode 3
  python run_auto_board_offset.py --board-id BOARD-001 --product-code 23691025-250616-0004-nvq-aoi

--product-code (tuỳ chọn): nếu truyền vào, tool sẽ gọi /api/products/select trên gateway
TRƯỚC khi đo, để gateway + Fiducial Detector Service chuyển đúng weights YOLO + file mapping
toạ độ của mã hàng này (xem products_registry.yaml cạnh gateway/plc_offset_gateway.py).
Bỏ qua cờ này nếu chỉ chạy 1 mã hàng duy nhất (hành vi cũ, không đổi).
"""

from __future__ import annotations

import argparse
import json
import sys

# Khi dong goi bang PyInstaller, bien PYTHONIOENCODING khong duoc bootloader
# ton trong nhu python.exe thuong - phai tu reconfigure stdout/stderr ve UTF-8
# tai code, neu khong print() tieng Viet co dau / emoji se nem UnicodeEncodeError
# va lam crash ca exe.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

import requests

from cli_config import CONFIG

Text = (
    "CLI tự động bù lệch board (thay bu_lech_board.py thủ công)\n"
    "Cách chạy:\n"
    "  python run_auto_board_offset.py --board-id BOARD-001              # mặt A (mặc định)\n"
    "  python run_auto_board_offset.py --board-id BOARD-001 --board-side B  # mặt B\n"
    "  python run_auto_board_offset.py --board-id BOARD-001 --anchor-mode 3 # 3 điểm mốc"
)

print(Text)

def main() -> None:
    parser = argparse.ArgumentParser(description="Tự động đo & tính bù lệch board bằng YOLO")
    parser.add_argument("--base-url", default=CONFIG["base_url"])
    parser.add_argument("--board-side", default="A", choices=("A", "B", "a", "b"),
                         help="Mặt board: A (mặc định) hoặc B")
    parser.add_argument("--anchor-mode", type=int, default=2, choices=(2, 3),
                         help="2 điểm (A,C, mặc định) hoặc 3 điểm (A,C,D)")
    parser.add_argument("--board-id", default=None)
    parser.add_argument("--product-code", default=None,
                         help="Mã hàng board (vd 23691025-250616-0004-nvq-aoi) - nếu có sẽ "
                              "gọi /api/products/select trước khi đo")
    parser.add_argument("--plc-pc-ip", default=CONFIG["plc_pc_ip"])
    parser.add_argument("--plc-ip", default=CONFIG["plc_ip"])
    parser.add_argument("--plc-port", type=int, default=CONFIG["plc_port"])
    args = parser.parse_args()

    board_side = args.board_side.upper()

    if args.product_code:
        print(f"--> Chọn mã hàng: {args.product_code} ...")
        try:
            sel_resp = requests.post(
                f"{args.base_url}/api/products/select", json={"product_code": args.product_code}, timeout=30,
            )
            sel_data = sel_resp.json()
        except requests.RequestException as e:
            print(f"❌ Không gọi được gateway để chọn mã hàng: {e}")
            sys.exit(1)
        if not sel_data.get("success"):
            print(f"❌ Chọn mã hàng thất bại: {sel_data.get('message')}")
            sys.exit(1)
        print(f"✅ Đã chọn mã hàng {args.product_code} (weights={sel_data.get('weights_path')})")

    payload = {
        "anchor_mode": args.anchor_mode,
        "board_side": board_side,
        "board_id": args.board_id,
        "plc_pc_ip": args.plc_pc_ip,
        "plc_ip": args.plc_ip,
        "plc_port": args.plc_port,
    }

    print(f"--> Gọi {args.base_url}/api/calib/auto-board-offset (mặt {board_side}) ...")
    try:
        resp = requests.post(f"{args.base_url}/api/calib/auto-board-offset", json=payload, timeout=60)
    except requests.RequestException as e:
        print(f"❌ Không gọi được gateway: {e}")
        sys.exit(1)

    data = resp.json()
    print(f"\nHTTP {resp.status_code}")
    print(json.dumps(data, indent=2, ensure_ascii=False))

    if data.get("success"):
        print(f"\n✅ Mặt {data.get('board_side', board_side)}: {data.get('message')}")
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
