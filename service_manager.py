"""
Service Manager - Bang dieu khien bat/tat cac module va theo doi log.
=====================================================================

Thay cho viec mo 5 cua so terminal chay tay tung lenh:

    python Stream_camera_Sony_20260508.py
    python run_gateway.py
    python run_fiducial_service.py
    python Do_khoang_cach_kem_camera_v6.py
    python run_auto_board_offset.py

Chuong trinh nay cho phep:
  * Bat / tat tung module bang 1 nut, hoac Start All / Stop All
  * Xem log truc tiep (stdout + stderr) cua tung module trong tab rieng
  * Loc log, tam dung cuon, luu log ra file (tu dong ghi vao thu muc logs/)
  * Theo doi trang thai: PID, thoi gian chay, health-check HTTP
  * Tu dong khoi dong lai module neu no chet ngoai y muon

Chay:
    python service_manager.py
hoac double-click Run_Service_Manager.bat

Cau hinh module: sua file services.yaml canh file nay (khong can sua code).

Code chia theo module trong thu muc app/:
    app/constants.py       - trang thai, mau, co Windows, fmt_uptime
    app/config.py          - duong dan services.yaml + load_config()
    app/service_process.py - ServiceProcess (subprocess, log, health-check)
    app/service_card.py    - ServiceCard (the dieu khien 1 module)
    app/log_pane.py        - LogPane (tab log 1 module)
    app/main_window.py     - ManagerWindow (cua so chinh)
    app/style.py           - stylesheet Qt
    app/main.py            - main()
"""

from __future__ import annotations

from app.main import main

if __name__ == "__main__":
    main()
