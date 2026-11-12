# 04 · Deploy lên web (nâng cao)

Chưa cần deploy để dùng hay để demo: chạy trên máy cá nhân theo [tài liệu 03](03-chay-local.md) là đủ. Tài liệu này
dành cho lúc cần một địa chỉ web để người khác vào dùng thử.

Giá và giới hạn của các dịch vụ thay đổi thường xuyên; xem lại trang giá của họ trước khi chọn.

## 1. Deploy gồm những gì

```
Người dùng ──HTTPS──► Dịch vụ chạy container ──► 1 container: FastAPI + giao diện + kho DuckDB (dữ liệu giả sinh lúc build)
                                                        │
                                                        └──► OpenRouter / Claude (nếu đặt khóa API)
```

Chỉ có một container, không cần cơ sở dữ liệu ngoài. Image chứa sẵn dữ liệu giả của **cả hai** doanh nghiệp mẫu
(`DATA_SCALE=0.5`, nhỏ bằng nửa bản chạy trên máy). Mỗi container phục vụ một doanh nghiệp, chọn bằng biến `WORKSPACE`.

## 2. Bảo mật cho bản công khai

Repo công khai chứa mật khẩu mẫu (`admin123`…), nên bản đưa lên mạng phải thay bằng biến môi trường:

| Biến | Tác dụng | Bắt buộc |
|---|---|---|
| `APP_SECRET` | Khóa ký phiên đăng nhập. Chuỗi ngẫu nhiên dài | Có |
| `PASSWORD_ADMIN` | Mật khẩu thật của tài khoản `admin`. Khi đặt, mật khẩu mẫu **không dùng được nữa** và tài khoản admin **không hiện** trên màn hình đăng nhập | Có |
| `PASSWORD_<TÊN>` | Tương tự cho tài khoản bất kỳ, ví dụ `PASSWORD_ANALYST` | Tùy |
| `DEMO_LOGIN` | `0`: không hiện nút tài khoản dùng thử nào. Không đặt: hiện các tài khoản còn dùng mật khẩu mẫu | Tùy |
| `OPENROUTER_API_KEY` | Bật mô hình ngôn ngữ. Đặt ở phần biến bí mật của dịch vụ, không đưa vào git | Tùy |

`render.yaml` trong repo đã khai báo sẵn: `APP_SECRET` và `PASSWORD_ADMIN` do Render tự sinh. Các tài khoản analyst
vẫn hiện làm nút dùng thử; chúng chỉ đọc dữ liệu giả lập.

Việc cần tự làm:

1. **Đặt hạn mức chi tiêu cho khóa API** trên trang OpenRouter. Ai vào trang cũng hỏi được, mỗi câu hỏi tốn tiền.
   Không muốn tốn thì không đặt khóa: hệ thống chạy bằng bộ luật có sẵn.
2. Không phát mật khẩu admin rộng rãi. Admin đổi được cài đặt, áp dụng quy tắc sửa dữ liệu và chạy lượt đo tốn tiền API.
3. Với dữ liệu thật, không dùng tài khoản trong `policies.yml` (mật khẩu lưu dạng chữ thường); dùng Supabase Auth
   (`SUPABASE_JWT_SECRET`) hoặc đặt hệ thống sau một lớp đăng nhập khác.

## 3. Image đã được kiểm chứng thế nào

Mỗi lần đẩy mã lên GitHub, workflow [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) tự chạy:

1. Bộ kiểm thử tự động.
2. `docker build`, rồi khởi động hai container (ABC và XYZ) với `PASSWORD_ADMIN` đặt riêng, và kiểm tra: `/api/health`
   trả lời; tài khoản admin không lộ trên màn hình đăng nhập; mật khẩu admin mẫu bị từ chối; đăng nhập analyst hỏi
   một câu nhận được bảng kết quả qua kiểm chứng số liệu.

Xem kết quả ở tab **Actions** của repo. Dấu xanh nghĩa là image build được và chạy đúng trên máy của GitHub.

Thử trên máy mình (cần Docker Desktop):

```powershell
docker build -t data01 .
docker run --rm -p 8000:8000 -e APP_SECRET=mot-chuoi-ngau-nhien-dai -e PASSWORD_ADMIN=mat-khau-cua-ban --env-file .env data01
```

## 4. Deploy lên Render

1. Vào <https://dashboard.render.com>, đăng nhập bằng tài khoản GitHub và cho Render quyền đọc repo (repo để private
   vẫn được).
2. **New → Blueprint**, chọn repo. Render đọc `render.yaml` và hiện hai dịch vụ: `data01-dienmay-abc` và
   `data01-giaovan-xyz`.
3. Render hỏi giá trị các biến đánh dấu bí mật:

   | Biến | Điền gì |
   |---|---|
   | `OPENROUTER_API_KEY` | Khóa của bạn, hoặc để trống để chạy bộ luật có sẵn |
   | `ANTHROPIC_API_KEY`, `SUPABASE_JWT_SECRET` | Để trống |

4. Bấm **Apply**. Lần build đầu mất khoảng 5–10 phút.
5. Mở địa chỉ `https://<tên-dịch-vụ>.onrender.com`. Mật khẩu admin: Dashboard → dịch vụ → **Environment** →
   `PASSWORD_ADMIN`.

Sau này mỗi lần đẩy mã lên nhánh `main`, Render tự build và deploy lại.

`render.yaml` dùng `plan: free`. Gói miễn phí tự ngủ khi không ai truy cập (lần mở đầu tiên sau đó phải chờ khoảng
một phút) và có ít bộ nhớ. Nếu dịch vụ bị khởi động lại vì thiếu bộ nhớ: giảm `DATA_SCALE` trong `Dockerfile` xuống
`0.25`, hoặc đổi sang gói trả tiền. Bước CI ở mục 3 in ra bộ nhớ container đã dùng để tham khảo.

## 5. Lựa chọn khác

Mọi dịch vụ chạy được Dockerfile đều dùng được. Container lắng nghe ở cổng lấy từ biến `PORT` (mặc định 8000).

| Dịch vụ | Cách làm | Lưu ý |
|---|---|---|
| Railway, Fly.io | Trỏ vào repo, dùng chung `Dockerfile`, đặt các biến ở mục 2 | Chưa thử |
| Hugging Face Spaces (loại Docker) | Tạo Space kiểu Docker, đặt biến `PORT=7860` | Chưa thử; cần thêm phần khai báo ở đầu README của Space |
| Máy chủ riêng (VPS) | `docker run` như mục 3, đặt sau Nginx hoặc Caddy để có HTTPS | Tự lo cập nhật và sao lưu |
| Chỉ demo tạm thời | Chạy trên máy cá nhân rồi mở đường hầm (Cloudflare Tunnel, ngrok) | Máy phải bật suốt buổi demo; vẫn phải đặt các biến ở mục 2 |

## 6. Giới hạn của bản deploy hiện tại

| Giới hạn | Hệ quả | Hướng xử lý |
|---|---|---|
| Dữ liệu chạy nằm trong container (`var/`) | Hội thoại, nhật ký, cài đặt và ví dụ đã học **mất** mỗi lần deploy lại hoặc container khởi động lại | Gắn ổ đĩa bền vào `/app/var` (gói trả tiền), hoặc chấp nhận với bản demo |
| Mỗi tiến trình một doanh nghiệp | Hai doanh nghiệp là hai dịch vụ, hai địa chỉ | Chưa có chuyển doanh nghiệp trên giao diện |
| Chạy một tiến trình, kho là một file | Phù hợp demo và nhóm nhỏ, không phù hợp nhiều người dùng cùng lúc | Ngoài phạm vi hiện tại |
| Khóa API của tài khoản mới bị giới hạn số lượt gọi mỗi phút | Nhiều người hỏi cùng lúc thì câu trả lời chậm lại (hệ thống tự chờ rồi gọi lại) | Nâng hạng tài khoản ở nhà cung cấp |
| Trang quản trị cho phép dựng lại kho | Thao tác nặng, chiếm máy trong lúc chạy | Hạn chế tài khoản admin |

## 7. Kiểm tra sau khi deploy

1. `/api/health` trả `"ok": true` và đúng tên doanh nghiệp.
2. Màn hình đăng nhập **không** có nút tài khoản admin; đăng nhập `admin` / `admin123` bị từ chối.
3. Đăng nhập analyst, hỏi một câu mẫu trên màn hình chào, có bảng và dấu ✓ xanh.
4. Thanh trên hiện đúng chế độ (tên mô hình, hoặc bộ luật có sẵn nếu không đặt khóa).
5. Hỏi một câu tự do; không có cảnh báo vàng. Có cảnh báo nghĩa là khóa API hoặc tên mô hình sai.
6. Xem số dư khóa API sau vài câu hỏi để biết mức tiêu thực tế.
