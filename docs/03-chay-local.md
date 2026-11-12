# 03 · Chạy trên máy cá nhân

Các lệnh dưới đây viết cho Windows PowerShell. Linux, macOS hoặc Git Bash dùng `./run.sh` thay cho `.\run.ps1`.

## 1. Cần có

| Thứ | Yêu cầu | Kiểm tra |
|---|---|---|
| Python | 3.11 trở lên (đã kiểm thử với 3.14) | `python --version` |
| Dung lượng đĩa | Khoảng 500 MB (thư viện + khoảng 120 MB dữ liệu giả) | |
| Git | Để tải mã nguồn | `git --version` |
| Khóa API | **Không bắt buộc.** Chỉ cần khi muốn bật mô hình ngôn ngữ | |

Không cần card đồ họa, không cần cài cơ sở dữ liệu.

## 2. Cài và chạy lần đầu

```powershell
git clone https://github.com/buiquyanh9-ai/System-Self-Service-Business-Analytics-tich-hop-AI-Agent-va-Text-to-SQL.git
cd System-Self-Service-Business-Analytics-tich-hop-AI-Agent-va-Text-to-SQL

python -m venv .venv                 # môi trường riêng cho dự án
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

.\run.ps1                            # lần đầu tự sinh dữ liệu giả (khoảng 1 phút) rồi mở server
```

Khi thấy dòng `Uvicorn running on http://127.0.0.1:8000`, mở địa chỉ đó trên trình duyệt và bấm một nút tài khoản
dùng thử. Dừng server bằng `Ctrl + C`.

Các lần sau chỉ cần kích hoạt môi trường và chạy `.\run.ps1`.

## 3. Bật mô hình ngôn ngữ (tùy chọn)

Không có khóa, hệ thống chạy bằng bộ luật offline. Để hỏi được câu tự do:

1. Chép `.env.example` thành `.env` ở gốc dự án.
2. Điền **một** trong hai khóa:

   | Biến | Lấy ở đâu | Mô hình mặc định |
   |---|---|---|
   | `OPENROUTER_API_KEY` | <https://openrouter.ai> → Keys | `openai/gpt-6-luna` (đổi bằng `OPENROUTER_MODEL`) |
   | `ANTHROPIC_API_KEY` | <https://console.anthropic.com> | `claude-opus-5-5` (đổi bằng `LLM_MODEL`) |

3. Khởi động lại server. Thanh trên của giao diện hiện tên mô hình đang dùng.

File `.env` đã nằm trong `.gitignore`. **Không đưa khóa lên git, không dán khóa vào chỗ công khai.** Nên đặt hạn mức
chi tiêu cho khóa ngay trên trang của nhà cung cấp.

Chi phí tham khảo: lượt đo 67 câu ngày 12/11/2026 tốn khoảng 0,02 USD với `gpt-6-luna`.

Đổi chế độ mà không cần khởi động lại: đăng nhập `admin` → trang **Cài đặt**.

## 4. Các biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WORKSPACE` | trống (doanh nghiệp ABC) | Tên thư mục trong `workspaces/` |
| `LLM_MODE` | `auto` | `auto`, `claude`, `openrouter`, `offline`. `auto`: Claude nếu có khóa Anthropic, kế đến OpenRouter, cuối cùng offline |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | trống, `openai/gpt-6-luna` | Gọi mô hình qua OpenRouter |
| `ANTHROPIC_API_KEY`, `LLM_MODEL`, `LLM_EFFORT` | trống, `claude-opus-5-5`, `medium` | Gọi Claude |
| `APP_SECRET` | giá trị dùng cho phát triển | Khóa ký phiên đăng nhập. **Phải đổi khi đưa lên mạng** |
| `SUPABASE_JWT_SECRET` | trống | Chấp nhận đăng nhập bằng Supabase Auth |

## 5. Chạy doanh nghiệp thứ hai

```powershell
.\run.ps1 -Workspace giaovan_xyz -Port 8001
```

Lần đầu lệnh này tự sinh dữ liệu của công ty giao vận. Mở <http://127.0.0.1:8001>. Có thể chạy song song với ABC ở
cổng 8000 trong một cửa sổ khác.

## 6. Tùy chọn của `run.ps1`

| Tùy chọn | Tác dụng |
|---|---|
| `-Port 8001` | Đổi cổng |
| `-Workspace <tên>` | Chạy doanh nghiệp khác |
| `-Rebuild` | Xóa và sinh lại dữ liệu. Dùng khi dữ liệu hỏng hoặc sau khi thử chức năng sửa dữ liệu |
| `-Scale 0.5` | Sinh dữ liệu nhỏ hơn (nhanh hơn, nhẹ hơn), dùng kèm `-Rebuild` |

Với `run.sh`: `PORT=8001 WORKSPACE=giaovan_xyz REBUILD=1 SCALE=0.5 ./run.sh`.

## 7. Kiểm thử

```powershell
python -m pytest -q
```

Kết quả mong đợi: `141 passed`, chạy khoảng nửa phút, không gọi mạng và không tốn tiền API. Cần đã có dữ liệu của cả
hai doanh nghiệp (chạy `.\run.ps1` và `.\run.ps1 -Workspace giaovan_xyz` ít nhất một lần); thiếu dữ liệu của XYZ thì
các test về doanh nghiệp thứ hai bị bỏ qua và con số `passed` sẽ nhỏ hơn.

Đo độ đúng trên bộ câu hỏi: đăng nhập `admin` → trang **Đo độ chính xác** → chạy. Ở chế độ LLM, lượt đo này có gọi
API.

## 8. Đưa dữ liệu của một doanh nghiệp khác vào

Cần một file kho dữ liệu DuckDB.

```powershell
python -m app.onboard cong_ty_moi --db duong\dan\kho.duckdb --name "Công ty Mới"
$env:WORKSPACE = "cong_ty_moi"; python -m uvicorn app.main:app
```

Công cụ sinh **bản nháp** sáu file cấu hình trong `workspaces/cong_ty_moi/config/`. Hệ thống chạy được ngay, nhưng
bản nháp không biết nghiệp vụ. Người hiểu doanh nghiệp cần rà lại theo thứ tự:

1. `profile.yml`: tên, ngành, lời giới thiệu, câu hỏi mẫu.
2. `semantic_layer.yml`: nhãn tiếng Việt, từ đồng nghĩa, bộ lọc bắt buộc của từng chỉ số, từ nhiều nghĩa.
3. `policies.yml`: tài khoản, vai trò, phân quyền theo dòng. **Đổi mật khẩu và tắt `demo_login`.**
4. `catalog.yml`: mô tả cột, đánh dấu cột nhạy cảm.

Chạy lại công cụ không ghi đè file đã rà. Khi dữ liệu thay đổi: `python -m app.onboard cong_ty_moi --stats`.

## 9. Lỗi thường gặp

| Hiện tượng | Nguyên nhân | Cách xử lý |
|---|---|---|
| `run.ps1 cannot be loaded because running scripts is disabled` | PowerShell chặn chạy script | `Set-ExecutionPolicy -Scope Process Bypass` rồi chạy lại |
| `python` không được nhận | Chưa cài Python hoặc chưa thêm vào PATH | Cài lại từ python.org, tích “Add to PATH” |
| Cổng 8000 đang bị dùng | Một server khác đang chạy | `.\run.ps1 -Port 8010` |
| `Không tìm thấy workspace '…'` | Gõ sai tên hoặc chưa tạo | Xem tên thư mục trong `workspaces/`; tạo mới bằng `app.onboard` |
| Thanh trên ghi chế độ offline dù đã có khóa | File `.env` sai chỗ, sai tên biến, hoặc chưa khởi động lại | `.env` phải ở gốc dự án; khởi động lại server |
| Câu trả lời có cảnh báo vàng | Mô hình lỗi (hết hạn mức, mất mạng, sai tên mô hình) | Kiểm tra số dư và tên mô hình ở trang Cài đặt |
| Chữ tiếng Việt lỗi trong cửa sổ lệnh | Bảng mã của console | Dùng `run.ps1` (đã đặt UTF-8) thay vì gọi `uvicorn` trực tiếp |
| Số liệu lạ sau khi thử “Áp dụng quy tắc sửa” | Kho đã được dựng lại với quy tắc sửa | Bấm Hoàn tác ở trang Chất lượng dữ liệu, hoặc `.\run.ps1 -Rebuild` |

Dữ liệu sinh ra khi chạy (kho, hội thoại, nhật ký, cài đặt) nằm trong `var/` và `workspaces/<tên>/var/`. Xóa thư mục
này là về trạng thái ban đầu.
