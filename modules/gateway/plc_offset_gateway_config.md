# Mô tả các key trong `plc_offset_gateway_config.json`

> File này **không tự động cập nhật** - nếu sau này thêm/đổi field trong
> `GatewayConfigModel` (class trong `plc_offset_gateway.py`) mà quên sửa ở đây thì file
> sẽ lạc hậu. Luôn coi comment ngay tại field trong `GatewayConfigModel` là nguồn đúng
> nhất; file này chỉ là bản tóm tắt dễ đọc hơn khi không muốn mở code.
>
> **Vì sao không viết mô tả thẳng vào file `.json`?** JSON không hỗ trợ comment - gateway
> đọc file bằng `json.loads()` thường, không dùng bộ đọc có hỗ trợ comment nào. Ngoài ra
> `load_gateway_config()` còn tự **ghi đè lại toàn bộ file** mỗi khi khởi động (chỉ giữ lại
> đúng những key đã biết trong `GatewayConfigModel`, key lạ - kể cả `"_comment"` - sẽ bị
> **âm thầm xoá** ở lần khởi động tiếp theo). Nên mọi giải thích phải để ở đây hoặc trong
> code, không để được trong chính file JSON.

## Camera / AI

| Key | Ý nghĩa |
|---|---|
| `camera_snapshot_url` | URL endpoint `/snapshot` của Stream Camera (Sony) dùng để chụp ảnh. |
| `camera_snapshot_timeout_ms` | Timeout (ms) cho 1 lần gọi HTTP GET snapshot. |
| `camera_snapshot_fresh_fetches` | Số lần gọi `/snapshot` liên tiếp mỗi lần cần chụp - chỉ giữ lại ảnh của **lần cuối cùng**. Dùng để tránh dính frame cache cũ (Stream Camera có thể trả về frame vừa lưu trước đó, chưa kịp cập nhật đúng lúc PLC vừa tới vị trí mới). |
| `camera_snapshot_fresh_delay_ms` | Khoảng cách (ms) giữa các lần gọi `/snapshot` liên tiếp ở trên. |
| `ai_api_url` | URL AI Detection API, dùng ở `/api/inspect-defect` (soi lỗi AOI). |

## PLC - chờ vị trí / feedback di chuyển

| Key | Ý nghĩa |
|---|---|
| `plc_axis_speed_mm_per_s` | Tốc độ trục PLC (mm/s) - **chỉ dùng để gateway tự ước tính timeout động** cho mỗi lệnh di chuyển (quãng đường / tốc độ này ⇒ thời gian chờ dự kiến). Không điều khiển tốc độ máy thật - đổi tốc độ PLC thật (HMI/servo) thì cũng nên cập nhật giá trị này cho khớp, để timeout ước tính không quá dư hoặc quá thiếu. |
| `use_plc_position_feedback` | Bật cơ chế chờ theo cặp thanh ghi done/busy (kiểu cũ). Nếu `false` và `use_plc_motion_status` cũng `false` thì gateway chỉ chờ cố định `plc_move_timeout_ms` rồi coi như xong, không kiểm tra PLC đã thật sự tới nơi chưa. |
| `plc_done_mem_area` / `plc_done_addr` / `plc_done_value` | Vùng nhớ + địa chỉ + giá trị báo "đã xong" (dùng khi `use_plc_position_feedback=true`). `addr=None` = không dùng. |
| `plc_busy_mem_area` / `plc_busy_addr` / `plc_busy_idle_value` | Vùng nhớ + địa chỉ + giá trị báo "đã hết bận/đứng yên" (cặp còn lại của done/busy). |
| `plc_poll_interval_ms` | Chu kỳ đọc lại thanh ghi PLC khi đang chờ máy tới vị trí. |
| `use_plc_motion_status` | Bật cơ chế chờ theo "motion status" (đọc cụm 4 thanh ghi D466-D469) - **mặc định `true`**, hiện đại hơn done/busy, không cần khai báo địa chỉ done/busy riêng. |
| `plc_motion_status_mem_area` / `plc_motion_status_addr` / `plc_motion_status_count` | Vùng nhớ + địa chỉ bắt đầu + số thanh ghi liên tiếp cần đọc để xác định máy đang di chuyển hay đứng yên (mặc định đọc 4 thanh ghi từ D466). |
| `plc_motion_start_timeout_ms` | Thời gian tối đa chờ PLC **bắt đầu** báo "đang di chuyển" sau khi gửi lệnh. Quá thời gian này mà chưa thấy motion-active thì coi là PLC "im lặng" (không báo trạng thái), rơi vào nhánh dự đoán theo `plc_axis_speed_mm_per_s` thay vì chờ vô hạn. |
| `plc_motion_idle_confirm_count` | Số lần đọc **liên tiếp** thấy trạng thái "idle" mới xác nhận là THẬT SỰ đã dừng hẳn (tránh nhầm do đọc trúng lúc thanh ghi đang chuyển đổi giá trị). |
| `plc_motion_settle_ms` | ⭐ **Thời gian chờ THÊM (ms) sau khi đã xác nhận PLC dừng hẳn, trước khi cho phép chụp ảnh** - để rung động cơ khí (do quán tính lúc dừng đột ngột) tắt hết, tránh ảnh mờ/nhoè. Đây chính là "thời gian đợi camera chụp" - có thể giảm nếu PLC tăng/giảm tốc êm hơn sau khi đẩy tốc độ lên cao. |
| `plc_motion_hard_timeout_ms` | Timeout **cứng** tối đa cho cả quá trình chờ 1 lệnh move hoàn tất (bất kể feedback thế nào) - chặn đứng việc treo vô hạn nếu PLC không bao giờ báo done/motion-idle. |
| `plc_move_timeout_ms` | Timeout "mềm" ban đầu truyền vào `wait_for_plc_position()` mỗi lệnh move - làm cơ sở tính các mốc thời gian ở trên. Có thể override riêng theo từng request gọi API (field `plc_move_timeout_ms` trong body). |
| `plc_io_timeout_ms` | Timeout tối đa (ms) cho **mỗi cuộc gọi PLC I/O riêng lẻ** (Connect/ReadInt/WriteFloat...) - chặn treo vĩnh viễn khi PLC mất kết nối/không phản hồi (xem hàm `plc_io()`). |

## Fiducial Detector (tìm marker)

| Key | Ý nghĩa |
|---|---|
| `fiducial_api_url` | URL API Fiducial Detector Service (tìm tâm điểm mốc/marker bằng YOLO). |
| `fiducial_confidence_threshold` | Ngưỡng confidence tối thiểu (0-1) để **chấp nhận** kết quả YOLO tìm marker - dưới ngưỡng coi như "không tìm thấy" (`status="not_found"` ở bước calib bù lệch). |

## Camera FOV / trục toạ độ

| Key | Ý nghĩa |
|---|---|
| `camera_fov_width_mm` / `camera_fov_height_mm` | Bề rộng/chiều cao vùng nhìn thấy của camera (mm) ở mức zoom quang học hiện tại - dùng để tính `camera_axis_matrix` **mặc định** (ước lượng thô, chưa hiệu chỉnh thật). |
| `camera_image_width_px` / `camera_image_height_px` | Độ phân giải ảnh camera trả về (px). |
| `camera_axis_matrix` | Ma trận trục T camera↔máy (`pixel_shift = T . plc_mm_shift`). Giá trị khởi tạo là ước lượng thô từ FOV (giả định trục thẳng hàng, không xoay/lật) - **phải hiệu chỉnh lại** bằng `/api/calib/camera-axis` trước khi tin dùng cho production. |
| `camera_axis_calibrated` | `false` = vẫn đang dùng giá trị ước lượng từ FOV, chưa hiệu chỉnh thật. |
| `camera_axis_invert_x` / `camera_axis_invert_y` | Đảo dấu độ lệch mm theo trục đó (áp SAU khi quy đổi pixel→mm) - dùng khi camera lắp lật trục so với PLC mà **chưa** hiệu chỉnh `camera_axis_matrix` đầy đủ. Nếu **đã** hiệu chỉnh bằng `/api/calib/camera-axis` thì để cả 2 = `false` (bật thêm sẽ đảo dấu 2 lần → sai hướng). |

## An toàn / ngưỡng cảnh báo (dùng ở `/api/calib/auto-board-offset`)

| Key | Ý nghĩa |
|---|---|
| `max_allowed_pixel_offset_px` | Lệch pixel tối đa (tâm marker phát hiện được so với tâm ảnh) cho 1 điểm mốc - vượt ngưỡng thì điểm đó bị loại (`status="outlier_pixel_offset"`), không đưa vào tính Kabsch (tránh 1 điểm đo sai làm lệch cả kết quả offset chung). |
| `max_allowed_rms_error_mm` | Sai số RMS (mm) tối đa chấp nhận được sau khi tính Kabsch từ các điểm mốc - vượt ngưỡng vẫn lưu offset nhưng trả về cảnh báo rõ ràng để kiểm tra lại (đo lại/kiểm tra marker-ánh sáng/board bị méo) trước khi dùng cho sản xuất thật.
