# Service Manager

Bang dieu khien bat/tat cac module va xem log truc tiep, thay cho viec mo 5 cua so
terminal chay tay tung lenh.

## Chay

Double-click `Run_Service_Manager.bat`, hoac:

```
D:\Camera\Dev\Camera_venv\Scripts\python.exe D:\Camera\Dev\Service_Manager\service_manager.py
```

## Giao dien

- **Cot trai** - moi module 1 the: den trang thai, nut Bat/Dung, PID, thoi gian chay,
  ket qua health-check, o tick "Tu bat lai khi module chet".
- **Cot phai** - tab log rieng cho tung module, cap nhat theo tung dong ngay khi
  module in ra. Co o loc log, tick "Tu cuon", nut xoa man hinh va mo file log.
- **Bat tat ca** - bat lan luot cac service theo dung thu tu trong `services.yaml`,
  cach nhau 3 giay (chinh bang `start_all_gap_sec`). Module loai `tool` khong nam
  trong nhom nay - phai bam "Chay" thu cong.
- **Dung tat ca** - dung theo thu tu nguoc lai.
- Double-click vao the module de nhay sang tab log cua no.

## Log ghi ra file

Log duoc chia thu muc theo module roi den nam / thang / ngay:

```
logs/
  gateway/
    2026/
      08/
        10/ gateway_2026_08_10.log
        11/ gateway_2026_08_11.log
  stream_camera/
    2026/08/10/ stream_camera_2026_08_10.log
```

- **Ten file co san ngay thang** nen khi copy file di noi khac van biet no cua module
  nao, ngay nao.
- **Bat lai nhieu lan trong cung ngay thi ghi noi vao cung 1 file**, khong de len
  file cu. Moi lan bat co 1 moc phan tach de de tim:

  ```
  ==============================================================================
  [08:51:09] ### PHIEN MOI luc 2026-08-10 08:51:09
  [08:51:09] ### KHOI DONG: ...
  ```

- Module chay xuyen dem se **tu dong chuyen sang file cua ngay moi** luc 00:00,
  va ghi lai 1 dong bao da chuyen o cuoi file ngay cu.
- Nut **"Mo file log hom nay"** mo dung file cua ngay hien tai; nut **"Mo thu muc"**
  mo thu muc rieng cua module de xem log cac ngay truoc.

## Cac module dang cau hinh

| Module | Loai | Cong | Ghi chu |
|---|---|---|---|
| Stream Camera Sony (RTSP) | service | 8001 | RTSP `rtsp://localhost:8554/mystream` |
| PLC Offset Gateway | service | 8083 | http://localhost:8083/docs |
| Fiducial Detector (YOLO) | service | 8191 | Khoi dong lau vi phai nap model |
| Do khoang cach + Camera v6 | gui | - | Mo cua so PySide6 rieng |
| Auto Board Offset | tool | - | CLI chay 1 lan, sua tham so ngay tren the |

## Them / sua module

Sua `services.yaml`, khong can dong vao code:

```yaml
  - name: ten_ky_thuat           # duy nhat
    label: Ten hien thi
    script: D:\duong\dan\den\script.py
    cwd: D:\duong\dan            # QUAN TRONG - xem ghi chu ben duoi
    type: service                # service | gui | tool
    health_url: http://127.0.0.1:8083/docs   # tuy chon
    args: --board-id BOARD-001   # tuy chon
    env:                         # tuy chon
      MY_VAR: gia_tri
    note: Mo ta ngan hien tren the
```

## Vi sao can `cwd` va cac xu ly dac biet

- `run_gateway.py` nap `"plc_offset_gateway:app"` va `run_fiducial_service.py` nap
  `"fiducial_service:app"` theo **ten tran**, con `Stream_camera_Sony_20260508.py`
  import `Config.ParamsBase`. Neu chay sai thu muc lam viec thi ca ba deu
  `ModuleNotFoundError`, nen `cwd` phai tro dung thu muc chua script.
- Khi dung module, Manager gui `CTRL_BREAK` truoc; sau 6 giay khong thoat thi
  `taskkill /PID <pid> /T /F`. Kill **ca cay process** la bat buoc vi Stream camera
  sinh process con `ffmpeg` - kill moi python se de lai ffmpeg treo va giu camera.
- Manager dat `PYTHONIOENCODING=utf-8` cho process con. Cac script co `print()` emoji
  (🚀, ✅, ❌); khi stdout bi chuyen huong vao pipe ma khong ep UTF-8 thi Python se
  nem `UnicodeEncodeError` ngay dong print dau tien.
- `PYTHONUNBUFFERED=1` + `python -u` de log hien ra ngay, khong bi dem lai.

## Thu tu khoi dong khuyen nghi

Stream camera → Gateway → Fiducial → GUI Do khoang cach → (khi can) Auto Board Offset.
Day dung la thu tu trong `services.yaml`, nen chi can bam **Bat tat ca**.
