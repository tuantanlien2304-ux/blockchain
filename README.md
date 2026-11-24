# Demo điểm danh sinh viên bằng Blockchain

Đây là nguyên mẫu chạy cục bộ để trình diễn quản lý lớp/buổi, điểm danh QR, phân quyền và nhật ký nối chuỗi SHA-256. Đây **không** phải blockchain phân tán: SQLite và khóa ký QR chỉ phục vụ học tập, không nên dùng để ghi nhận điểm danh chính thức.

Người dùng phải đăng nhập mới vào dashboard. Giảng viên quản lý lớp, sinh viên và buổi học; sinh viên chỉ được điểm danh, xem thông tin cá nhân, số buổi và trạng thái chuyên cần. Các chức năng quản lý, Blockchain Explorer và Smart Contract chỉ hiển thị cho giảng viên.

## Chạy trên Windows

Yêu cầu Python 3.10 trở lên.

```powershell
cd attendance-blockchain-demo
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Mở http://localhost:8000. Dữ liệu demo được tạo tự động trong `attendance.db`.

> **Chế độ demo (tuỳ chọn):** hai endpoint `/api/blockchain/tamper` và `/api/blockchain/repair` (nút "Giả lập sửa dữ liệu" / "Khôi phục" trên tab Blockchain) mặc định **bị tắt** (trả 404). Muốn dùng chúng để minh hoạ phát hiện can thiệp, đặt biến môi trường rồi chạy lại:
> ```powershell
> $env:ATTENDANCE_DEMO = "1"
> python app.py
> ```
> Cả hai thao tác còn yêu cầu đăng nhập bằng tài khoản **giảng viên**.

| Vai trò | Tên đăng nhập | Mật khẩu |
|---|---|---|
| Giảng viên | `gv01` | `GVdemo123!` |
| Sinh viên | `sv01` | `SVdemo123!` |
| Sinh viên | `sv02` | `SVdemo123!` |

Lớp `CNTT101` và buổi `Buổi demo điểm danh` đã được khởi tạo sẵn. Giảng viên có thể tạo môn học/lớp bằng mã môn, mã lớp và tên môn; chọn lớp, ghi danh `sv01`/`sv02`, rồi tạo buổi học. Mỗi buổi có QR và mã điểm danh 6 số dùng được khi buổi còn mở. Bảng môn học hiển thị số buổi và tổng lượt có mặt/vắng; bảng chi tiết cho biết trạng thái từng buổi. Sinh viên thấy môn học, tổng số buổi, số buổi đã điểm danh/vắng/chưa điểm danh; buổi chỉ tính vắng sau khi giảng viên đóng buổi. Giao diện sinh viên có nút **Quét QR bằng camera** và **Chọn ảnh QR**; đọc ảnh được xử lý trên máy chủ cục bộ và không cần cấp quyền camera. Quét trực tiếp bằng camera cần trình duyệt hỗ trợ `BarcodeDetector` và trang HTTPS hoặc localhost. Khi mở app qua HTTP trong mạng LAN, có thể dùng ứng dụng Camera của điện thoại để quét QR và mở link điểm danh. Để quét QR từ điện thoại trong cùng mạng LAN, đặt `ATTENDANCE_HOST=0.0.0.0` và `ATTENDANCE_BASE_URL` thành địa chỉ máy chạy server, ví dụ `http://192.168.1.20:8000`, rồi chạy `python app.py`. Cần cho phép cổng 8000 trong mạng nội bộ; không mở demo này ra Internet.

## Gửi link cho mọi người vào

Có 2 cách, tuỳ người nhận ở đâu.

### A. Cùng mạng Wi-Fi/LAN (nhanh nhất, không cần deploy)

1. Bấm đúp **`Chay-chia-se-LAN.bat`** (hoặc mở PowerShell trong thư mục và chạy):
   ```powershell
   $env:ATTENDANCE_HOST="0.0.0.0"; $env:ATTENDANCE_BASE_URL="http://<IP-LAN>:8000"; python app.py
   ```
   File `.bat` tự tìm IP của máy và in ra dòng `Link gui cho moi nguoi: http://192.168.x.x:8000`.
2. Gửi link đó (kèm `?code=...` lấy trên tab buổi học/QR) cho mọi người. Điện thoại cùng Wi-Fi mở được, có thể quét QR ngay bằng ứng dụng Camera.
3. Nếu máy khác báo không vào được: cho phép `python.exe` qua **Windows Defender Firewall** (bảng điều khiển → Allow an app), hoặc chạy `.bat` bằng *Run as administrator*.

Link/QR bây giờ tự sinh theo địa chỉ đang truy cập: mở bằng `http://192.168.1.20:8000` thì QR chứa đúng địa chỉ đó; mở bằng tên miền (có HTTPS) thì QR chứa `https://...`. Có thể khoá cứng bằng biến `ATTENDANCE_BASE_URL`.

### B. Ai cũng vào được từ Internet

Máy nhà thường không mở cổng ra ngoài. Chọn một trong hai:

- **Deploy lên dịch vụ cloud** (khuyến nghị) — xem mục tiếp theo. Link dạng `https://diem-danh-xxxx.onrender.com`, gửi ai cũng mở được.
- **Tunnel tạm thời** khi cần demo nhanh từ máy đang chạy:
  ```powershell
  $env:ATTENDANCE_HOST="0.0.0.0"
  python app.py
  # Terminal khác: cài cloudflared, rồi
  cloudflared tunnel --url http://localhost:8000
  ```
  Tool sẽ in ra link `https://....trycloudflare.com` — gửi link này. Lưu ý điểm danh trên serverless/tunnel dùng chung 1 worker nên chỉ phù hợp lớp nhỏ, và không dùng làm hệ thống chính thức.

## Đưa lên web (deploy)

App vẫn là HTTP server thuần Python nên có thể chạy trên mọi máy chủ/VPS hoặc nền tảng container. Trên web, server phải lắng nghe `0.0.0.0` và cổng do nền tảng cấp (`PORT`).

### 1. VPS / máy chủ Linux (cách phổ biến nhất)

```bash
pip install -r requirements.txt
ATTENDANCE_HOST=0.0.0.0 ATTENDANCE_BASE_URL=https://ten-mien-cua-ban \
  gunicorn --workers 1 --threads 8 --bind 0.0.0.0:8000 wsgi:application
```

Đặt gunicorn sau Nginx/Caddy để có HTTPS và tên miền. **Chỉ dùng 1 worker** vì SQLite và phiên đăng nhập (lưu trong RAM) cần dùng chung một tiến trình; số luồng `--threads 8` phục vụ đủ tester.

### 2. Docker (Render, Railway, Fly.io, VPS...)

```bash
docker build -t diem-danh .
docker run -p 8000:8000 -e ATTENDANCE_BASE_URL=http://localhost:8000 \
  -v diem-danh-data:/data diem-danh
```

Có sẵn `Dockerfile`, `Procfile` (Heroku/Railway) và `render.yaml`. Container đọc ghi dữ liệu tại `/data` (mount volume để giữ dữ liệu giữa các lần deploy).

### 3. Serverless (Vercel / Netlify / AWS Lambda)

Các nền tảng này **không giữ được SQLite** nên dùng chế độ dữ liệu tạm: mỗi lần khởi động lại, database và tài khoản demo
được tạo mới (đủ để demo, không lưu lịch sử điểm danh lâu dài).

```bash
ATTENDANCE_EPHEMERAL=1 ATTENDANCE_HOST=0.0.0.0 \
  gunicorn --workers 1 --threads 8 --bind 0.0.0.0:$PORT wsgi:application
```

> Lưu ý riêng với **Vercel**: chưa có adapter Python WSGI chính thức cho định dạng `@vercel/python` mới, và SQLite không chạy được trên filesystem serverless (dữ liệu sẽ mất ngay khi request kết thúc). Với nhu cầu "gửi link cho mọi người vào", **Render / Railway / Fly.io / VPS + Docker** là lựa chọn đúng; `render.yaml` đã cấu hình sẵn.

### Biến môi trường dùng khi deploy

| Biến | Ý nghĩa |
|---|---|
| `PORT` | Cổng server (mặc định `8000`; nhiều nền tảng tự đặt) |
| `ATTENDANCE_HOST` | Địa chỉ lắng nghe, đặt `0.0.0.0` khi chạy trên web |
| `ATTENDANCE_BASE_URL` | URL công khai, dùng để sinh link trong QR, ví dụ `https://diemdanh.example.com`. Nếu bỏ trống, app tự suy ra từ địa chỉ người dùng đang truy cập |
| `ATTENDANCE_DATA_DIR` | Thư mục ghi `attendance.db` và khóa ký QR (mặc định thư mục dự án, hoặc `/tmp/attendance-data`) |
| `ATTENDANCE_SECRET_KEY` | Khóa HMAC cho QR (chuỗi ngẫu nhiên dài). Nếu bỏ trống, app tự sinh và lưu ra đĩa |
| `ATTENDANCE_EPHEMERAL` | `1` = dùng dữ liệu tạm trên đĩa không bền vững |
| `ATTENDANCE_DEMO` | `1` = bật endpoint minh hoạ can thiệp dữ liệu |

> Lưu ý: phiên đăng nhập lưu trong RAM nên khi server khởi động lại, người dùng phải đăng nhập lại. Trên máy chủ nhiều CPU bật thêm `WEB_CONCURRENCY=1` để tránh tạo nhiều worker.

## Kiểm thử và minh chứng

Danh sách buổi học, thống kê và danh sách sinh viên trong cửa sổ buổi học tự làm mới mỗi 5 giây khi người dùng đang đăng nhập; buổi mới và lượt điểm danh sẽ xuất hiện trên các dashboard đang mở.

```powershell
python -m unittest discover -s tests -v | Out-File evidence/test-results.txt -Encoding utf8
```

Bộ kiểm thử Python hiện có 14 kịch bản (phân quyền, tạo nhiều buổi, giải mã QR từ ảnh, điểm danh một lần, trigger chỉ-ghi, phát hiện can thiệp, khôi phục chuỗi, giới hạn chế độ demo). Kiểm thử Smart Contract (8 kịch bản Mocha/Chai) nằm ở `tests/StudentAttendance.test.js` và cần cài Hardhat mới chạy được.

Kết quả chạy kiểm thử được lưu ở [evidence/test-results.txt](evidence/test-results.txt); mã QR SVG mẫu ở [evidence/sample-qr.svg](evidence/sample-qr.svg).

Các kiểm thử bao gồm: vai trò sinh viên không được tạo lớp/đọc audit toàn cục; giảng viên tạo lớp, ghi danh sinh viên, tạo/đóng buổi và SVG QR; buổi đã đóng và điểm danh lần hai bị từ chối; trigger SQLite chặn sửa lịch sử; nếu cố ý gỡ trigger rồi sửa dữ liệu trực tiếp thì hàm kiểm chứng chuỗi phát hiện sai lệch; QR giả/chỉnh sửa bị từ chối.

## Giới hạn và hướng phát triển

- Hash chain giúp phát hiện sửa bản ghi và xóa ở giữa chuỗi, nhưng không tự khôi phục dữ liệu; xóa một đoạn ở cuối chuỗi cũng không thể phát hiện nếu không có hash neo độc lập. Quản trị viên toàn quyền cơ sở dữ liệu có thể viết lại cả chuỗi. Muốn tăng khả năng chống chối bỏ cần neo hash định kỳ ra nhiều nút độc lập hoặc dịch vụ lưu trữ chỉ-ghi.
- QR demo ký HMAC, hết hạn sau 30 phút và mỗi sinh viên chỉ ghi nhận một lần/buổi. QR tĩnh vẫn có thể bị chụp/chia sẻ trong thời hạn; triển khai thực tế nên dùng QR luân phiên thời gian ngắn, xác thực danh tính và kiểm soát vị trí/quy trình giám sát phù hợp.
- Tài khoản mẫu và mật khẩu nằm trong mã, phiên đăng nhập lưu trong RAM, khóa QR được lưu cạnh chương trình; không dùng dữ liệu/mật khẩu này trong môi trường thật.
- Phiên bản sản phẩm cần TLS, quản lý mật khẩu/khóa an toàn, cơ sở dữ liệu và sao lưu phù hợp, giao diện quản trị đầy đủ, chính sách lưu trữ dữ liệu cá nhân, ghi log và đánh giá tải/khả năng sẵn sàng.
