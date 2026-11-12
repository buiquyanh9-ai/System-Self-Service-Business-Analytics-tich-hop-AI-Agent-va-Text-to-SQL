# 01 · Kỹ thuật và phương pháp

Tài liệu này giải thích hệ thống làm việc thế nào và vì sao chọn cách đó. Không cần biết trước về AI; cần biết SQL ở
mức đọc hiểu. Từ ngữ xem ở [trang mục lục](README.md#từ-ngữ-dùng-trong-tài-liệu).

## 1. Ý tưởng chính

Để một mô hình ngôn ngữ tự viết SQL rồi chạy thẳng có ba rủi ro: tính sai định nghĩa (doanh thu nào?), lộ dữ liệu
không được xem, và bịa con số trong lời giải thích. Hệ thống xử lý bằng một nguyên tắc: **mô hình chỉ đề xuất, code
quyết định.**

| Rủi ro | Cách chặn | Ở đâu |
|---|---|---|
| Tính sai định nghĩa | Chỉ số khai báo một lần trong semantic layer; SQL được biên dịch từ khai báo đó | `config/semantic_layer.yml`, `app/semantic.py` |
| Từ nhiều nghĩa | Hỏi lại người dùng bằng code, trước khi gọi mô hình | `ambiguities:` trong semantic layer |
| Lộ dữ liệu, phá dữ liệu | Chỉ cho lệnh đọc; kiểm tra quyền theo bảng, cột, dòng trên cây cú pháp SQL | `app/governance.py` |
| Truy vấn quá nặng | Ước lượng dung lượng quét; vượt ngưỡng thì cảnh báo, chờ duyệt hoặc chặn | `app/cost.py` |
| SQL sai logic | Phân tích tĩnh 9 quy tắc; tự sửa lỗi thường gặp | `app/sqlcheck.py`, `app/repair.py` |
| Bịa số khi giải thích | Lời giải thích chỉ chèn số qua chỗ giữ chỗ; mọi con số trong văn bản được đối chiếu lại với kết quả | `app/explain.py` |
| Mô hình lỗi hoặc hết tiền | Rơi về bộ luật offline và báo rõ trên giao diện | `app/agent.py` |

## 2. Đường đi của một câu hỏi

```
Câu hỏi tiếng Việt
   │
   ▼
1. Nạp ngữ cảnh ........ hội thoại trước, vai trò người hỏi, hồ sơ doanh nghiệp
   │
   ▼
2. Lập kế hoạch ........ theo thứ tự ưu tiên:
   │                     a) chốt chặn bằng code: câu giới thiệu, câu đã có SQL xác nhận, từ nhiều nghĩa
   │                     b) LLM gọi công cụ (nếu bật)
   │                     c) bộ luật offline (nếu không có LLM hoặc LLM lỗi)
   │
   ├─► Hỏi lại ......... khi câu hỏi mơ hồ
   ├─► Trả lời thông tin  định nghĩa chỉ số, mô tả dữ liệu, “bạn là ai”
   ├─► Phân tích nguyên nhân  cho câu hỏi “vì sao”
   │
   ▼
3. Sinh SQL ............ biên dịch từ kế hoạch qua semantic layer, hoặc SQL do LLM viết
   ▼
4. Tối ưu và kiểm duyệt  cú pháp → chỉ đọc → quyền bảng/cột → quyền theo dòng → chạy thử → phân tích tĩnh
   │        ▲
   │        └── tự sửa (tối đa 3 lần) nếu kiểm duyệt báo lỗi
   ▼
5. Kiểm tra chi phí .... nhỏ: chạy luôn · vừa: cảnh báo · lớn: chờ người dùng duyệt · rất lớn: chặn
   ▼
6. Chạy trên DuckDB .... kết nối chỉ đọc, có giới hạn thời gian
   ▼
7. Giải thích .......... tính các “sự kiện” từ kết quả, viết lời, đối chiếu lại từng con số
   ▼
8. Ghi nhật ký, lưu ngữ cảnh, gợi ý câu hỏi tiếp
```

Toàn bộ luồng là một đồ thị trạng thái tự viết trong `app/agent.py` (mỗi bước là một nút, kết quả của nút quyết định
nút tiếp theo). Giao diện hiện lại từng nút đã đi qua trong tab *Pipeline agent*.

## 3. Các kỹ thuật, từng cái một

### 3.1 Semantic layer

File `semantic_layer.yml` khai báo: các bảng và cách nối, các chiều, các chỉ số (công thức, bộ lọc bắt buộc, bộ phận
chịu trách nhiệm). Kế hoạch truy vấn chỉ cần nói “chỉ số X, chia theo Y, tháng Z”; `app/semantic.py` biên dịch thành
SQL. Nhờ vậy câu lệnh không phụ thuộc vào việc mô hình có nhớ đúng tên cột viết tắt hay không.

Doanh nghiệp mẫu ABC có 25 chỉ số, 24 chiều, 7 model. Hỗ trợ chỉ số suy diễn (tỷ lệ), chỉ số lấy từ nhiều bảng sự
kiện, so sánh kỳ, và chỉ số không cộng được qua thời gian (tồn kho).

### 3.2 Hai cách hiểu câu hỏi

| | LLM gọi công cụ | Bộ luật offline |
|---|---|---|
| Cần | Khóa API, có mạng | Không cần gì |
| Hiểu được | Câu tự do, kể cả ngoài semantic layer | Câu về chỉ số, chiều, thời gian đã khai báo |
| Tốc độ | Khoảng 6–7 giây mỗi câu | Dưới 1 giây |
| File | `app/llm.py` | `app/nlu.py` |

LLM không được trả lời trực tiếp. Nó được cấp 6 công cụ và phải nộp kết quả qua công cụ cuối:

| Công cụ | Việc |
|---|---|
| `search_schema` | Tìm bảng, cột, chỉ số liên quan tới một cụm từ |
| `describe_table` | Xem cột, kiểu, ý nghĩa, giá trị mẫu của một bảng |
| `get_column_values` | Xem các giá trị của một cột để viết bộ lọc đúng chính tả |
| `compile_semantic_plan` | Thử biên dịch một kế hoạch thành SQL |
| `dry_run_sql` | Kiểm tra một câu SQL mà không chạy: cú pháp, quyền, phân tích tĩnh, chi phí |
| `submit_answer` | Nộp kết quả cuối cùng |

Vòng gọi công cụ tối đa 8 lượt, dừng sớm khi mô hình lặp lại cùng một lời gọi lỗi, và buộc nộp ở lượt cuối.

### 3.3 Tìm kiếm ngữ cảnh (RAG) và ví dụ mẫu

Mô tả bảng, cột và chỉ số được đánh chỉ mục bằng BM25 (tìm theo từ khóa, chạy tại chỗ, không cần cơ sở dữ liệu
vector). Các cặp “câu hỏi → SQL đã xác nhận” trong `fewshots.yml` được dùng theo hai cách: câu hỏi gần giống thì dùng
lại SQL đã xác nhận; câu khác thì đưa vào lời nhắc làm ví dụ. File: `app/retriever.py`.

### 3.4 Kiểm duyệt SQL trên cây cú pháp

Câu SQL được phân tích thành cây cú pháp bằng thư viện `sqlglot`, rồi kiểm tra trên cây chứ không so chuỗi:

- **Chỉ đọc:** chặn `DROP`, `INSERT`, `UPDATE`… và các hàm đọc file (`read_csv`…).
- **Quyền theo vai trò:** vai trò Analyst chỉ đọc được tầng dữ liệu phục vụ, không đọc cột nhạy cảm (số điện thoại,
  lương).
- **Quyền theo dòng:** điều kiện lọc (ví dụ chỉ Miền Bắc) được **chèn thẳng vào cây cú pháp** của mọi bảng liên quan.
  Người dùng không bỏ qua được bằng cách viết lại câu lệnh.

### 3.5 Ước lượng chi phí và duyệt trước khi chạy

Chi phí ước lượng = số dòng còn lại sau khi cắt theo ngày × độ rộng các cột được dùng. Ngưỡng của vai trò Analyst:
trên 20 MB cảnh báo, trên 60 MB phải duyệt, trên 400 MB chặn. Truy vấn chạm bảng từ 100.000 dòng và quét trên 15 MB
cũng dừng lại để người dùng xem, sửa câu lệnh và bấm duyệt. Ngưỡng đặt trong `policies.yml`.

### 3.6 Phân tích tĩnh và tự sửa

Chín quy tắc (mã từ L001 đến L010, không có L009) bắt các lỗi logic mà câu lệnh vẫn chạy được nhưng cho số sai: cộng lặp do nối bảng cha với
bảng con, cộng tồn kho qua nhiều tuần, quên lọc trạng thái đơn hàng, điều kiện ngày làm mất khả năng cắt dữ liệu…
`app/repair.py` tự sửa các lỗi thường gặp: sai tên cột (dò theo mô tả trong catalog), thiếu `GROUP BY`, chia cho 0,
viết lại `YEAR(ngay) = 2026` thành khoảng ngày.

### 3.7 Lời giải thích có kiểm chứng

Hệ thống tính trước một tập “sự kiện” từ bảng kết quả (tổng, giá trị lớn nhất, tỷ trọng, mức thay đổi). Lời giải
thích chỉ được chèn số lấy từ tập này. Sau khi viết xong, hàm `audit` trong `app/explain.py` quét mọi con số trong
văn bản và đối chiếu lại. Dấu ✓ xanh trên giao diện nghĩa là mọi con số đều khớp.

### 3.8 Phân tích nguyên nhân biến động

Dùng cho câu hỏi “vì sao … tăng/giảm”. Các bước trong `app/insights.py`:

1. Kiểm tra mức biến động có bất thường không so với lịch sử của chính chỉ số (z-score).
2. Phân rã mức thay đổi theo từng chiều, tìm chiều giải thích được nhiều nhất, rồi đào sâu vào giá trị gây biến động.
3. Tách thành số đơn × giá trị mỗi đơn.
4. Kiểm định xem có phải lỗi dữ liệu không. Với gói bán lẻ: kiểm định Kolmogorov–Smirnov phát hiện đơn giá bị ghi
   sai đơn vị, đối chiếu tồn kho để phát hiện hết hàng, đối chiếu lịch đóng cửa hàng và lịch khuyến mãi.

Kết luận phân biệt *nguyên nhân kinh doanh* với *lỗi dữ liệu*.

### 3.9 Chất lượng dữ liệu

`app/dq.py` chạy các kiểm định: cấu trúc bảng đổi so với cam kết (data contract), dữ liệu chưa cập nhật, khóa ngoại
không khớp, số dòng theo ngày bất thường, phân phối bị trôi lệch (PSI, chi-square), dòng trùng, giá trị ngoại lai.
Với doanh nghiệp ABC, quản trị viên có thể áp dụng quy tắc sửa, dựng lại kho và hoàn tác.

### 3.10 Nhiều doanh nghiệp

Mã nguồn không nhắc tên bảng hay chỉ số của doanh nghiệp nào. Mỗi doanh nghiệp là một thư mục gồm sáu file cấu hình
(`profile`, `catalog`, `semantic_layer`, `policies`, `contracts`, `fewshots`), chọn bằng biến môi trường `WORKSPACE`.
Mỗi tiến trình phục vụ một doanh nghiệp nên dữ liệu không lẫn vào nhau. Công cụ `python -m app.onboard` đọc một kho
DuckDB bất kỳ và sinh bản nháp sáu file này.

## 4. Công nghệ và lý do chọn

| Phần | Dùng | Lý do |
|---|---|---|
| Máy chủ | FastAPI (Python) | Gọn, có tài liệu API tự sinh |
| Kho dữ liệu | DuckDB | Một file, không cần cài máy chủ, đủ nhanh cho vài trăm nghìn đến vài triệu dòng |
| Phân tích SQL | sqlglot | Phân tích và viết lại cây cú pháp; đổi phương ngữ SQL sau này ít tốn công |
| Mô hình ngôn ngữ | OpenRouter (mặc định `openai/gpt-6-luna`) hoặc Claude | Hai client cùng một giao diện, đổi trong trang Cài đặt |
| Tìm kiếm | BM25 tự viết | Không thêm phụ thuộc; đủ cho vài trăm mục mô tả |
| Thống kê | numpy, pandas, scipy | Kiểm định KS, chi-square, z-score |
| Giao diện | HTML + JavaScript thuần + Chart.js | Không cần bước build; API tách riêng nên thay giao diện được |
| Lưu hội thoại, nhật ký | SQLite | Một file, nằm trong `var/` |
| Đăng nhập | JWT ký HS256 | Chấp nhận thêm token của Supabase Auth nếu cấu hình |

## 5. Bản đồ mã nguồn

| Thư mục, file | Nội dung |
|---|---|
| `app/main.py` | Các đường dẫn API |
| `app/agent.py` | Đồ thị xử lý một câu hỏi |
| `app/llm.py`, `app/nlu.py` | Hai cách hiểu câu hỏi |
| `app/semantic.py`, `app/catalog.py`, `app/profile.py` | Đọc cấu hình doanh nghiệp, biên dịch SQL |
| `app/governance.py`, `app/cost.py`, `app/sqlcheck.py`, `app/repair.py` | Các chốt chặn quanh SQL |
| `app/explain.py`, `app/insights.py`, `app/dq.py`, `app/lineage.py` | Giải thích, nguyên nhân, chất lượng, nguồn gốc dữ liệu |
| `app/evals.py`, `eval/questions.json` | Đo độ đúng |
| `app/onboard.py` | Nhận kho dữ liệu mới |
| `config/` | Cấu hình của doanh nghiệp mẫu ABC |
| `workspaces/giaovan_xyz/` | Doanh nghiệp mẫu thứ hai |
| `warehouse/` | Sinh dữ liệu giả và dựng kho của ABC |
| `web/` | Giao diện |
| `tests/` | 141 kiểm thử tự động |
| `var/` | Dữ liệu sinh ra khi chạy (không đưa lên git) |

## 6. Cách đo độ đúng

Mỗi câu trong bộ câu hỏi có một câu SQL đáp án. Hệ thống trả lời câu hỏi, rồi **so kết quả chạy** của nó với kết quả
chạy của đáp án (không so chữ của câu lệnh). Tỷ lệ khớp là *execution accuracy*.

Kết quả đo ngày 12/11/2026, mỗi cấu hình một lượt chạy:

| Doanh nghiệp | Bộ luật offline | `gpt-4o-mini` | `gpt-6-luna` |
|---|---|---|---|
| ABC (41 câu) | 36/41 | 38/41 | 41/41 |
| XYZ (26 câu) | 22/26 | 25/26 | 26/26 |

Đo lại sau khi làm lại giao diện (cùng ngày) cho cùng số câu đúng ở bộ luật offline và `gpt-6-luna`.

## 7. Giới hạn cần biết

- **Các con số trên lạc quan.** Bộ câu hỏi do người xây hệ thống viết và đã được dùng để sửa lỗi. Chưa có bộ câu hỏi
  độc lập, chưa chạy lặp nhiều lần để biết dao động.
- Con số của chế độ LLM chỉ có nghĩa khi câu đó thật sự do LLM trả lời. Ở lượt đo lại trên XYZ có 1/26 câu LLM lỗi
  và bộ luật trả lời thay.
- Chế độ Claude chưa được đo vì chưa có khóa; mới kiểm bằng client giả.
- Giao diện mới được kiểm bằng ảnh chụp tự động, chưa có người dùng thật thử.
- Kho phải là DuckDB; chiều thời gian phải mang tên `ngay, tuan, thang, quy, nam`; đổi doanh nghiệp phải khởi động
  tiến trình khác.
- Phân tích nguyên nhân chưa chỉ ra thành phần gây biến động cho chỉ số dạng tỷ lệ.
- Dữ liệu của cả hai doanh nghiệp mẫu là dữ liệu giả lập.
