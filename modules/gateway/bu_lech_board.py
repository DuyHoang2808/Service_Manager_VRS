"""
BÙ LỆCH BOARD THEO THỜI GIAN THỰC (Kabsch / Procrustes N điểm mốc)
--------------------------------------------------------------------
Mục đích: mỗi khi gá board mới lên máy VRS (đã cố định 2 cạnh nhưng vẫn
có thể lệch 1-2mm do khe hở cơ khí của jig), script này giúp:
  1. Cho phép chọn ngay từ đầu chương trình dùng 2 điểm mốc (nhanh, khớp
     tuyệt đối) hay 3 điểm mốc (Kabsch, chậm hơn 1 chút nhưng có kiểm tra
     residual để phát hiện đo lỗi).
  2. Đo lại các điểm mốc đã chọn bằng cách di chuyển camera/PLC tới khớp
     từng điểm trên board, đọc tọa độ PLC thực tế.
  3. Dùng thuật toán Kabsch (bình phương tối thiểu) để tìm ra phép xoay + dịch
     chuyển RIGID (không méo) khớp tốt nhất giữa vị trí board hiện tại so với
     vị trí board lúc calib gốc. Với N=2 điểm, kết quả giống hệt công thức
     2-điểm đóng (khớp tuyệt đối). Với N=3, thuật toán tự động "trung bình
     hoá" sai số đo đạc giữa các điểm, đồng thời báo cáo sai số dư (RMS) để
     phát hiện đo nhầm hoặc méo phi tuyến cục bộ.
  4. Áp dụng phép xoay + dịch đó lên MỌI điểm gia công, dựa trên ma trận
     bilinear tĩnh đã có sẵn trong vrs_calib_4diem.json (không cần calib
     lại từ đầu).

Thuật toán Kabsch (2D):
    centroid_P, centroid_Q = trung bình các điểm kỳ vọng / đo thực tế
    H = (P - centroid_P)^T . (Q - centroid_Q)
    U, S, V^T = SVD(H)
    R = V . diag(1, det(V.U^T)) . U^T      (đảm bảo không bị phản chiếu gương)
    t = centroid_Q - R . centroid_P
"""

import json
import math
import os

import numpy as np

# --- Kho điểm mốc khả dụng (tọa độ thiết kế trên Board), lấy từ calib_6diem.xlsx ---
# Điểm mốc KHÔNG bắt buộc phải là góc/gốc tọa độ - chỉ cần biết đúng tọa độ
# thiết kế (Board X, Y). Muốn đổi/thêm điểm khác: sửa/thêm trực tiếp vào đây,
# lấy đúng theo cột "Board X", "Board Y" của điểm đó trong file Excel.
ANCHOR_POINTS_POOL = {
    "A": (9.9, 10),
    "B": (9.9, 394),
    "C": (596.6, 393),
    "D": (597.9, 10),
    "E": (69, 8),
    "F": (534.5, 396)
}

# --- Cấu hình sẵn cho từng chế độ, chọn theo các điểm trải rộng/không thẳng
# hàng tốt nhất trong kho điểm mốc ở trên ---
# 2 điểm: A-C nằm CHÉO NHAU trên board (~648.7mm, gần bằng đường chéo lớn
#         nhất có thể) => đạt tiêu chí tách biệt/trải rộng dù chỉ đo 2 điểm.
# 3 điểm: thêm D để tạo tam giác rộng, có residual để tự kiểm tra lỗi đo.
ANCHOR_SETS = {
    2: ["A", "C"],
    3: ["A", "C", "D"],
}


def get_anchor_points(mode):
    """Trả về [(tên, (x, y)), ...] tương ứng chế độ 2 hoặc 3 điểm đã chọn."""
    return [(f"Điểm {name}", ANCHOR_POINTS_POOL[name]) for name in ANCHOR_SETS[mode]]


def choose_anchor_mode():
    """Hỏi người vận hành muốn dùng 2 hay 3 điểm mốc ngay khi khởi động."""
    print("=" * 62)
    print(" CHỌN CHẾ ĐỘ BÙ LỆCH BOARD")
    print("=" * 62)
    print("  [2] Dùng 2 điểm mốc (A, C) - nhanh hơn, khớp tuyệt đối,")
    print("      KHÔNG kiểm tra chéo được lỗi đo.")
    print("  [3] Dùng 3 điểm mốc (A, C, D) - chậm hơn 1 lần đo, có residual")
    print("      để tự phát hiện đo lỗi/board bị méo.\n")
    while True:
        choice = input("Nhập lựa chọn (2 hoặc 3): ").strip()
        if choice in ("2", "3"):
            return int(choice)
        print("Lựa chọn không hợp lệ, vui lòng nhập 2 hoặc 3.")


def load_calibration_matrix(json_path):
    if not os.path.exists(json_path):
        raise FileNotFoundError(
            f"Không tìm thấy file '{json_path}'. Vui lòng chạy 'Calib_4diem.py' trước!"
        )
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)


def board_to_plc(x, y, coeffs):
    """Ma trận calib tĩnh (bilinear hoặc biquadratic nếu có c4/c5/d4/d5).

    Bilinear:    PLC = c0 + c1*x + c2*y + c3*x*y
    Biquadratic: PLC = c0 + c1*x + c2*y + c3*x*y + c4*x² + c5*y²
    """
    X_PLC = coeffs["c0"] + coeffs["c1"] * x + coeffs["c2"] * y + coeffs["c3"] * x * y
    Y_PLC = coeffs["d0"] + coeffs["d1"] * x + coeffs["d2"] * y + coeffs["d3"] * x * y
    # Biquadratic terms (tương thích ngược: không có → bỏ qua)
    if "c4" in coeffs:
        X_PLC += coeffs["c4"] * x * x + coeffs["c5"] * y * y
        Y_PLC += coeffs["d4"] * x * x + coeffs["d5"] * y * y
    return X_PLC, Y_PLC


def kabsch_2d(P_expected, Q_measured):
    """
    Tìm phép xoay R (2x2) + dịch chuyển t (2,) tối ưu (bình phương tối thiểu)
    sao cho R . p_i + t xấp xỉ q_i tốt nhất, với N >= 2 cặp điểm tương ứng.

    Trả về: R, t, theta (rad), rms_error (mm), max_error (mm), residuals (mm mỗi điểm)
    """
    P = np.array(P_expected, dtype=float)
    Q = np.array(Q_measured, dtype=float)
    if P.shape != Q.shape or P.shape[0] < 2:
        raise ValueError("Cần ít nhất 2 cặp điểm tương ứng, cùng số lượng.")

    centroid_P = P.mean(axis=0)
    centroid_Q = Q.mean(axis=0)

    P_c = P - centroid_P
    Q_c = Q - centroid_Q

    H = P_c.T @ Q_c
    U, _, Vt = np.linalg.svd(H)
    d = np.sign(np.linalg.det(Vt.T @ U.T))
    D = np.diag([1.0, d])
    R = Vt.T @ D @ U.T

    t = centroid_Q - R @ centroid_P

    P_transformed = (R @ P.T).T + t
    residuals = np.linalg.norm(P_transformed - Q, axis=1)
    rms_error = float(np.sqrt(np.mean(residuals**2)))
    max_error = float(np.max(residuals))
    theta = math.atan2(R[1, 0], R[0, 0])

    return R, t, theta, rms_error, max_error, residuals


def apply_rigid_offset(x_plc, y_plc, R, t):
    """Áp phép xoay + dịch lên 1 điểm PLC 'kỳ vọng' để ra điểm PLC 'thực tế'."""
    p = R @ np.array([x_plc, y_plc]) + t
    return float(p[0]), float(p[1])


def run():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    # File cua ma hang dang production (xem gateway/products_registry.yaml), khong con
    # nam thang trong gateway/ nua.
    PRODUCT_DIR = os.path.join(BASE_DIR, "products", "23691025-250616-0004-nvq-aoi")
    json_path = os.path.join(PRODUCT_DIR, "vrs_calib_side_b.json")
    coeffs = load_calibration_matrix(json_path)

    anchor_mode = choose_anchor_mode()
    anchor_points_board = get_anchor_points(anchor_mode)
    n_points = len(anchor_points_board)

    print("\n" + "=" * 62)
    print(f" BÙ LỆCH BOARD MỚI ({n_points} ĐIỂM MỐC) - MÁY VRS")
    print("=" * 62)

    expected_points = []
    for name, (bx, by) in anchor_points_board:
        plc_exp = board_to_plc(bx, by, coeffs)
        expected_points.append(plc_exp)
        print(f"PLC kỳ vọng tại {name} (Board {bx},{by}): ({plc_exp[0]:.3f}, {plc_exp[1]:.3f})")

    print("\n--> Di chuyển camera/PLC khớp lần lượt từng điểm mốc TRÊN BOARD MỚI,")
    print("    nhập tọa độ PLC thực tế đọc được tại mỗi điểm:\n")

    measured_points = []
    for name, _ in anchor_points_board:
        print(f"-- {name} --")
        mx = float(input(f"   PLC X thực tế: ").strip())
        my = float(input(f"   PLC Y thực tế: ").strip())
        measured_points.append((mx, my))
        print()

    R, t, theta, rms_error, max_error, residuals = kabsch_2d(expected_points, measured_points)

    print("-" * 62)
    print(f" Góc lệch board:      {math.degrees(theta):+.4f}°")
    print(f" Dịch chuyển bù (X):  {t[0]:+.4f} mm")
    print(f" Dịch chuyển bù (Y):  {t[1]:+.4f} mm")
    print(f" Sai số dư RMS:       {rms_error:.4f} mm   (max: {max_error:.4f} mm)")
    print("-" * 62)
    for (name, _), res in zip(anchor_points_board, residuals):
        flag = "  <-- lệch bất thường, kiểm tra lại phép đo!" if res > 2 * rms_error and res > 0.02 else ""
        print(f"   Sai số tại {name}: {res:.4f} mm{flag}")

    if n_points >= 3:
        if rms_error < 0.03:
            print("\n=> Board di chuyển đúng dạng rigid (chỉ xoay + tịnh tiến), độ tin cậy cao.")
        else:
            print("\n=> Sai số dư khá lớn: có thể do đo nhầm 1 điểm, hoặc board bị méo/")
            print("   sai số phi tuyến cục bộ. Nên đo lại hoặc cân nhắc calib lưới đa điểm.")
    else:
        print(
            "\n=> Chỉ dùng 2 điểm mốc: khớp tuyệt đối theo toán học, KHÔNG có residual dư "
            "để tự kiểm tra chéo lỗi đo. Nếu muốn phát hiện được đo nhầm, chạy lại và chọn "
            "chế độ 3 điểm."
        )

    offset_data = {
        "anchor_mode": n_points,
        "theta_deg": math.degrees(theta),
        "tx": float(t[0]),
        "ty": float(t[1]),
        "rms_error_mm": rms_error,
        "max_error_mm": max_error,
        "anchor_points_board": anchor_points_board,
        "anchor_points_expected_plc": expected_points,
        "anchor_points_measured_plc": measured_points,
        "residuals_mm": residuals.tolist(),
    }
    offset_path = os.path.join(PRODUCT_DIR, "offset_runtime_side_b.json")
    with open(offset_path, "w", encoding="utf-8") as f:
        json.dump(offset_data, f, indent=4, ensure_ascii=False)
    print(f"\nĐã lưu hệ số bù vào: {offset_path}\n")

    print("Nhập tọa độ Board (x, y) để lấy tọa độ PLC ĐÃ BÙ LỆCH. Gõ 'q' để thoát.\n")
    while True:
        raw_x = input("Board X (mm): ").strip()
        if raw_x.lower() in ("q", "exit"):
            break
        raw_y = input("Board Y (mm): ").strip()
        if raw_y.lower() in ("q", "exit"):
            break
        try:
            x, y = float(raw_x), float(raw_y)
        except ValueError:
            print("Giá trị không hợp lệ, nhập lại.\n")
            continue

        plc_nominal = board_to_plc(x, y, coeffs)
        plc_final = apply_rigid_offset(*plc_nominal, R, t)

        print(f"  PLC kỳ vọng (chưa bù): ({plc_nominal[0]:.3f}, {plc_nominal[1]:.3f})")
        print(f"  PLC ĐÃ BÙ LỆCH        : X = {plc_final[0]:.3f}  |  Y = {plc_final[1]:.3f}\n")


if __name__ == "__main__":
    run()
