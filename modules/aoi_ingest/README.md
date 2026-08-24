# AOI Ingest - folder watcher

Module nhỏ, độc lập (không phụ thuộc Flutter/PLC Gateway lúc chạy) đọc dữ liệu
AOI ghi ra folder và nạp vào `autovrs.db` (cùng file DB Flutter app đang dùng).
Đây chính là phần "Nhánh A - API nạp dữ liệu AOI" còn thiếu trong
`KE_HOACH_TRIEN_KHAI_PIPELINE.md`, chỉ khác là AOI ghi **file** ra một folder
dùng chung thay vì gọi API - nên module này là 1 watcher polling folder, không
phải FastAPI service.

> Đối chiếu schema với thiết kế gốc (PDF `250714 - AOI Inspection - Phân tích
> thiết kế APP AUTOVRS - Version2.pdf`) và các delta/vấn đề phát sinh: xem
> `../THIET_KE_CSDL_CAP_NHAT.md`.

## Cấu trúc dữ liệu đầu vào (đã xác nhận trực tiếp với dữ liệu thật)

```
AOI_Output/<board_id>/<layer l1..l8>/<side A hoặc B>.vrs
                                      <side>.txt   (không dùng ở module này)
                                      <side>1.jpg .. <side>N.jpg
```

`.vrs` (text CRLF), 9 dòng header:

```
1: version
2: số dòng header (luôn là 9)
3: bbox board "xmin,ymin,xmax,ymax"
4: chưa rõ (quan sát luôn là 0)
5: số lượng lỗi khai báo
6: số điểm căn chỉnh (hiện tại = 2)
7: model/job name (dùng làm QCamber model name)
8: layer id (l1/l8/...)
9: chưa rõ (quan sát luôn là 0)
10..10+align_count-1: toạ độ điểm căn chỉnh "x,y,idx"
còn lại: toạ độ điểm lỗi "x,y,type_code", dòng đầu tiên <-> {side}1.jpg,
         dòng thứ 2 <-> {side}2.jpg, ...
```

Toạ độ là số nguyên, 3 chữ số cuối là phần thập phân theo mm
(vd `520234` → `520.234 mm`) - xác nhận trực tiếp với người yêu cầu, không suy đoán.

## Các quyết định đã chốt (từ AskUserQuestion trong hội thoại)

| Quyết định | Đã chọn |
|---|---|
| `plc_coor` tính thế nào | Lưu thẳng toạ độ board (mm) - **không** chạy qua `board_to_plc()`/calib. Khác với `KE_HOACH_TRIEN_KHAI_PIPELINE.md` mục 2 (bản đó dùng calib) - nếu sau này cần bù calib, sửa tại `format_coord_pair()` trong `aoi_ingest_service.py` hoặc chèn bước gọi `board_to_plc()` trước khi insert. |
| Dấu thập phân | Dấu **chấm** (`520.234;121.337`) - bắt buộc vì Flutter (`double.tryParse`) không đọc được dấu phẩy. |
| 1 board có nhiều layer (l1, l8...) | 1 dòng `tbBoard` riêng cho mỗi layer. Đã thêm cột `board_code`, `layer_id` vào `tbBoard` (migration tự chạy trong `db_writer.ensure_schema`). |
| Số lỗi khai báo > số ảnh đã lưu | Chỉ xử lý N lỗi đầu = số ảnh thực có, bỏ phần dư (không có ảnh). |

## Quy trình xử lý (mỗi board folder)

1. Chờ tới khi mỗi layer folder hiện có trong board folder đọc được `.vrs` VÀ
   số ảnh `.jpg` không đổi qua `stable_polls_required` lần poll liên tiếp
   (mặc định 2 lần, cách nhau `poll_interval_seconds` giây) - coi là "AOI đã
   ghi xong". Không dùng cứng số 2000 vì đó chỉ là quan sát thực tế, có thể
   khác theo máy/model.
2. Khi TẤT CẢ layer folder đã sẵn sàng, move **toàn bộ** board folder sang
   `archive_dir` (dùng `shutil.move`, đổi tên nếu trùng bằng cách thêm
   timestamp).
3. Với mỗi layer: get-or-create `tbModel` (theo tên ở dòng 7), get-or-create
   `tbLot` (xem giả định bên dưới), insert 1 dòng `tbBoard`
   (`board_code`, `layer_id`, `defect_quantity`), rồi insert N dòng `tbDefect`
   (`type`, `coordinates`, `plc_coor`, `url_image` trỏ tới ảnh ở vị trí ĐÃ
   MOVE).

## Giả định cần bạn xác nhận lại (chưa có trong yêu cầu gốc)

- **Lot**: chưa có khái niệm "AOI đang chạy cho lot nào" ở tầng dữ liệu AOI.
  Mặc định: dùng lại lot mới nhất của đúng model đó, hoặc tạo lot rỗng mới nếu
  model chưa có lot nào. **Cập nhật**: đối chiếu với mục 1.1.2 của tài liệu
  thiết kế gốc cho thấy quy trình thật yêu cầu kỹ sư xác nhận thủ công 1 lot
  (qua `ConfirmView`) trước khi chạy dây chuyền — auto-create ở đây có thể
  không đúng quy trình. Xem mục 2 của `../THIET_KE_CSDL_CAP_NHAT.md` để quyết
  định có sửa `get_or_create_lot` thành "bắt buộc lot đã tồn tại" hay không.
- **erro_quantity** của `tbBoard`: để `0` lúc nạp (chưa có phán định NG/OK -
  việc đó xảy ra sau, lúc vận hành viên duyệt từng defect).
- **Thứ tự move rồi mới ghi DB** (đúng như yêu cầu gốc): nếu tiến trình crash
  đúng lúc giữa 2 bước này, folder đã nằm trong `archive_dir` nhưng DB chưa có
  dữ liệu, không tự retry. Có thể đảo ngược thứ tự (ghi DB trong transaction
  trước, move sau) nếu muốn an toàn hơn trước rủi ro crash - nói nếu muốn đổi.
- ~~Cột `board_code`/`layer_id` mới chỉ thêm ở phía Python~~ — **đã sửa**:
  `local_database_service.dart` giờ cũng có migration này, cộng thêm
  `status`/`completed_at` cho cơ chế "board tiếp theo" (xem
  `../THIET_KE_CSDL_CAP_NHAT.md` mục 6).

## Chạy thử

```bash
cd AOI_Ingest
python aoi_ingest_service.py --config aoi_ingest_config.json --once   # 1 lần quét, để test
python aoi_ingest_service.py --config aoi_ingest_config.json          # chạy liên tục (poll mỗi 5s)
```

Sửa `aoi_ingest_config.json`:

- `watch_dir`: folder AOI ghi ảnh lỗi ra (vd `AOI_Output`)
- `archive_dir`: folder đích sau khi xử lý xong
- `db_path`: đường dẫn `autovrs.db` thật (mặc định trỏ đúng chỗ Flutter dùng
  trên Windows: `%USERPROFILE%\Documents\AutoVRS\autovrs.db`)
- `poll_interval_seconds`, `stable_polls_required`: tinh chỉnh tốc độ/độ chắc
  chắn của bước "đủ ảnh chưa"

## File

- `aoi_ingest_service.py` - toàn bộ service trong 1 file: parse `.vrs`, thao
  tác SQLite (tạo bảng/migration/insert), vòng lặp poll + move folder, và
  `main()` để chạy trực tiếp. Trước đây tách 3 file (`vrs_parser.py`,
  `db_writer.py`, `aoi_folder_watcher.py`) - đã gộp lại theo yêu cầu, các hàm
  giữ nguyên tên/logic, chỉ khác namespace chung 1 file.
- `aoi_ingest_config.json` - cấu hình đường dẫn/thời gian
