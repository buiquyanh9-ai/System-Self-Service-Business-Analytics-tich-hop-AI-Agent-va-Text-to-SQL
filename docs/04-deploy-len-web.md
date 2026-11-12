# 04 · Deploy lên web (nâng cao)

> **Trạng thái: chưa chạy thử.** `Dockerfile` và `render.yaml` có sẵn trong repo, nhưng trong đợt kiểm tra ngày
> 13/11/2026 chưa build image và chưa deploy lên dịch vụ nào (máy kiểm tra không có Docker). Coi tài liệu này là kế
> hoạch làm việc; lần deploy đầu tiên có thể phải chỉnh. Giá và giới hạn của các dịch vụ thay đổi thường xuyên, hãy
> xem lại trang giá của họ trước khi chọn.

Chưa cần deploy để dùng hay để demo: chạy trên máy cá nhân theo [tài liệu 03](03-chay-local.md) là đủ.

## 1. Deploy gồm những gì

```
Người dùng ──HTTPS──► Dịch vụ chạy container ──► 1 container: FastAPI + giao diện + kho DuckDB (dữ liệu giả sinh lúc build)
                                                        │
                                                        └──► OpenRouter / Claude (nếu đặt khóa API)
```

Chỉ có một container, không cần cơ sở dữ liệu ngoài. Dữ liệu giả được sinh ngay lúc build image (`DATA_SCALE=0.5`,
nhỏ bằng nửa bản chạy trên máy).

## 2. Việc phải làm trước khi đưa lên mạng

Bản trong repo cấu hình cho việc dùng thử trên máy cá nhân. Đưa lên mạng công khai mà không sửa thì **ai cũng đăng
nhập được bằng quyền admin**.

| # | Việc | Vì sao | Sửa ở đâu |
|---|---|---|---|
| 1 | Đặt `APP_SECRET` là chuỗi ngẫu nhiên dài | Khóa ký phiên đăng nhập; giá trị mặc định đoán được | Biến môi trường (Render tự sinh qua `render.yaml`) |
| 2 | Đặt `demo_login: false` | Nếu để `true`, màn hình đăng nhập và `/api/public` liệt kê cả tên lẫn mật khẩu | `config/policies.yml` |
| 3 | Đổi mật khẩu các tài khoản | Mật khẩu mẫu (`admin123`…) nằm công khai trên GitHub | `config/policies.yml` |
| 4 | Đặt khóa API bằng biến môi trường bí mật của dịch vụ | Không đưa `.env` vào image (`.dockerignore` đã loại) hay vào git | Trang cấu hình của dịch vụ |
| 5 | Đặt hạn mức chi tiêu cho khóa API | Trang công khai có thể bị gọi nhiều | Trang của OpenRouter hoặc Anthropic |
| 6 | Cân nhắc ai được vào trang quản trị | Admin đổi được cài đặt, áp dụng quy tắc sửa dữ liệu, chạy lượt đo tốn tiền API | Chỉ phát tài khoản analyst cho người xem |

Lưu ý: mật khẩu hiện lưu dạng chữ thường trong `policies.yml`. Chấp nhận được cho bản demo với dữ liệu giả; với dữ
liệu thật nên dùng Supabase Auth (`SUPABASE_JWT_SECRET`) hoặc đặt hệ thống sau một lớp đăng nhập khác.

Nếu muốn giữ nút tài khoản dùng thử cho bản demo công khai, phương án an toàn hơn là chỉ để lại các tài khoản analyst
trong `policies.yml` của bản deploy và giữ `demo_login: true`.

## 3. Thử image trên máy trước (nên làm)

Cần Docker Desktop.

```powershell
docker build -t data01 .
docker run --rm -p 8000:8000 -e APP_SECRET=mot-chuoi-ngau-nhien-dai --env-file .env data01
```

Mở <http://127.0.0.1:8000> và kiểm tra `http://127.0.0.1:8000/api/health` trả `"ok": true`. Bước build mất vài phút
vì phải cài thư viện và sinh dữ liệu.

## 4. Deploy lên Render

`render.yaml` mô tả sẵn một dịch vụ web chạy bằng Docker, có kiểm tra sức khỏe ở `/api/health`.

1. Đẩy mã lên GitHub (sau khi làm mục 2).
2. Trên Render: **New → Blueprint**, chọn repo. Render đọc `render.yaml`.
3. Điền các biến được hỏi:

   | Biến | Giá trị |
   |---|---|
   | `APP_SECRET` | Render tự sinh |
   | `OPENROUTER_API_KEY` | Khóa của bạn; để trống thì chạy bộ luật offline |
   | `OPENROUTER_MODEL` | `openai/gpt-6-luna` (đã đặt sẵn) |
   | `ANTHROPIC_API_KEY` | Để trống nếu dùng OpenRouter |
   | `LLM_MODE` | `auto` (đã đặt sẵn) |
   | `SUPABASE_JWT_SECRET` | Để trống nếu không dùng Supabase |

4. Chờ build xong, mở địa chỉ Render cấp, kiểm tra `/api/health`.

`render.yaml` đang ghi `plan: starter`, là gói **trả tiền**. Muốn dùng gói miễn phí thì đổi thành `plan: free`. Gói
miễn phí có ít bộ nhớ và tự ngủ khi không ai truy cập; chưa kiểm tra hệ thống có đủ bộ nhớ trên gói này không. Nếu
thiếu, giảm `DATA_SCALE` trong `Dockerfile`.

## 5. Lựa chọn khác

Mọi dịch vụ chạy được Dockerfile đều dùng được. Container lắng nghe ở cổng lấy từ biến `PORT` (mặc định 8000).

| Dịch vụ | Cách làm | Lưu ý |
|---|---|---|
| Railway, Fly.io | Trỏ vào repo, dùng chung `Dockerfile`, đặt các biến ở mục 4 | Chưa thử |
| Hugging Face Spaces (loại Docker) | Tạo Space kiểu Docker, đặt biến `PORT=7860` hoặc khai `app_port` | Chưa thử; cần thêm phần khai báo ở đầu README của Space |
| Máy chủ riêng (VPS) | `docker run` như mục 3, đặt sau Nginx hoặc Caddy để có HTTPS | Tự lo cập nhật và sao lưu |
| Chỉ demo tạm thời | Chạy trên máy cá nhân rồi mở đường hầm (Cloudflare Tunnel, ngrok) | Máy phải bật suốt buổi demo; vẫn phải làm mục 2 |

## 6. Giới hạn của bản deploy hiện tại

| Giới hạn | Hệ quả | Hướng xử lý |
|---|---|---|
| Dữ liệu chạy nằm trong container (`var/`) | Hội thoại, nhật ký, cài đặt và ví dụ đã học **mất** mỗi lần deploy lại hoặc container khởi động lại | Gắn ổ đĩa bền vào `/app/var`, hoặc chấp nhận với bản demo |
| `Dockerfile` chỉ dựng doanh nghiệp ABC | Đặt `WORKSPACE=giaovan_xyz` sẽ không có dữ liệu | Thêm bước sinh dữ liệu XYZ vào `Dockerfile` (xem dưới) và chạy thành dịch vụ thứ hai |
| Mỗi tiến trình một doanh nghiệp | Hai doanh nghiệp là hai dịch vụ, hai địa chỉ | Chưa có chuyển doanh nghiệp trên giao diện |
| Chạy một tiến trình, kho là một file | Phù hợp demo và nhóm nhỏ, không phù hợp nhiều người dùng cùng lúc | Ngoài phạm vi hiện tại |
| Trang quản trị cho phép dựng lại kho | Thao tác nặng, chiếm máy trong lúc chạy | Hạn chế tài khoản admin |

Phác thảo bước thêm cho doanh nghiệp XYZ (chưa thử), đặt sau dòng `RUN python warehouse/generate.py…`:

```dockerfile
RUN python workspaces/giaovan_xyz/warehouse/generate.py --scale ${DATA_SCALE} \
 && python -m app.onboard giaovan_xyz --stats
```

rồi đặt biến `WORKSPACE=giaovan_xyz` cho dịch vụ thứ hai.

## 7. Kiểm tra sau khi deploy

1. `/api/health` trả `"ok": true` và đúng tên doanh nghiệp.
2. Màn hình đăng nhập **không** hiện mật khẩu admin.
3. Đăng nhập analyst, hỏi một câu mẫu trên màn hình chào, có bảng và dấu ✓ xanh.
4. Thanh trên hiện đúng chế độ (tên mô hình, hoặc bộ luật có sẵn nếu không đặt khóa).
5. Hỏi một câu tự do; không có cảnh báo vàng. Có cảnh báo nghĩa là khóa API hoặc tên mô hình sai.
6. Xem số dư khóa API sau vài câu hỏi để biết mức tiêu thực tế.
