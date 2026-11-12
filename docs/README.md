# Tài liệu DATA-01

Bộ tài liệu này dành cho người lần đầu tiếp xúc với hệ thống. Đọc theo thứ tự dưới đây, hoặc nhảy thẳng tới phần cần.

| Bạn muốn | Đọc | Thời gian |
|---|---|---|
| Dùng thử: hỏi số liệu, đọc câu trả lời | [02 · Hướng dẫn sử dụng](02-huong-dan-su-dung.md) | 10 phút |
| Cài và chạy trên máy mình | [03 · Chạy trên máy cá nhân](03-chay-local.md) | 15 phút |
| Hiểu hệ thống làm việc thế nào, dùng kỹ thuật gì | [01 · Kỹ thuật và phương pháp](01-ky-thuat-va-phuong-phap.md) | 20 phút |
| Đưa lên mạng cho người khác dùng | [04 · Deploy lên web](04-deploy-len-web.md) | 15 phút |

## Hệ thống này là gì

Một trợ lý phân tích dữ liệu cho doanh nghiệp. Người dùng gõ câu hỏi bằng tiếng Việt, ví dụ *“Doanh thu thuần theo
miền tháng 10/2026”*. Hệ thống tự viết câu lệnh SQL, kiểm tra câu lệnh đó có an toàn và đúng quyền không, chạy trên
kho dữ liệu, rồi trả về bảng số, biểu đồ và một đoạn giải thích.

Ba điểm khác với việc hỏi thẳng một chatbot:

1. **Con số có định nghĩa chung.** “Doanh thu thuần” được định nghĩa một lần trong cấu hình; mọi người hỏi đều nhận
   cùng một cách tính.
2. **Mô hình ngôn ngữ không được tin ngay.** Mọi câu lệnh, dù do mô hình hay bộ luật viết, đều qua các bước kiểm tra
   bằng code trước khi chạy. Mọi con số trong lời giải thích được tính lại từ bảng kết quả.
3. **Chạy được không cần mô hình ngôn ngữ.** Không có khóa API thì hệ thống dùng bộ luật hiểu tiếng Việt có sẵn,
   trả lời được các câu hỏi về chỉ số đã định nghĩa.

## Từ ngữ dùng trong tài liệu

| Từ | Nghĩa |
|---|---|
| Chỉ số (metric) | Một con số có định nghĩa thống nhất, ví dụ doanh thu thuần, tỷ lệ giao đúng hạn |
| Chiều (dimension) | Tiêu chí để chia con số: theo tháng, theo miền, theo ngành hàng |
| Semantic layer | File cấu hình khai báo chỉ số, chiều và cách nối các bảng. Là “từ điển số liệu” của doanh nghiệp |
| Text-to-SQL | Chuyển câu hỏi bằng lời thành câu lệnh SQL |
| LLM | Mô hình ngôn ngữ lớn, gọi qua API (ở đây là OpenRouter hoặc Claude) |
| Bộ luật offline | Phần hiểu tiếng Việt viết bằng code, không gọi mạng, dùng khi không có LLM |
| Workspace | Một doanh nghiệp: bộ cấu hình, dữ liệu và bộ câu hỏi kiểm tra riêng |
| RBAC, RLS | Phân quyền theo vai trò (được xem bảng, cột nào) và theo dòng (chỉ thấy dữ liệu miền mình) |
| HITL | Người dùng phải xem và bấm duyệt trước khi một truy vấn nặng được chạy |
| Execution accuracy | Tỷ lệ câu hỏi mà kết quả truy vấn của hệ thống khớp kết quả của đáp án |

## Trạng thái kiểm tra (13/11/2026)

| Hạng mục | Kết quả |
|---|---|
| Kiểm thử tự động | 141/141 đạt, không test nào gọi mạng |
| Chạy thử server thật | Hai doanh nghiệp mẫu khởi động được; đăng nhập, hỏi đáp, phân tích nguyên nhân và 5 trang quản trị đều trả kết quả |
| Độ đúng trên bộ câu hỏi | Xem bảng ở mục 6 của [README gốc](../README.md) và phần giới hạn trong [tài liệu 01](01-ky-thuat-va-phuong-phap.md#7-giới-hạn-cần-biết) |
| Deploy lên web | **Chưa chạy thử.** File `Dockerfile` và `render.yaml` có sẵn nhưng chưa được build trên máy nào trong quá trình kiểm tra |
