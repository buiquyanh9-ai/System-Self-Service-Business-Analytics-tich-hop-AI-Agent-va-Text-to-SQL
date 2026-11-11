# DATA-01 · AI Agent Text-to-SQL Self-Service Analytics

Agent nhận câu hỏi **tiếng Việt**, tự lập kế hoạch truy vấn, sinh SQL đúng **semantic layer**, kiểm duyệt bằng code
(cú pháp → chỉ đọc → RBAC/RLS → cột PII → dry-run → lint AST → ước lượng bytes quét), **hỏi lại khi mơ hồ**, cho người
dùng **duyệt SQL trước khi chạy trên bảng lớn (HITL)**, chạy trên kho DuckDB, trả về **bảng + biểu đồ + diễn giải có
kiểm chứng số liệu 100%**, tự sửa SQL lỗi, phân tích **nguyên nhân biến động**, và **phát hiện – xử lý sai lệch dữ liệu**
bằng kiểm định thống kê.

Chạy được **ngay không cần API key** (bộ hiểu tiếng Việt offline trên semantic layer); đặt `ANTHROPIC_API_KEY` để bật
**Claude function calling** cho câu hỏi tự do.

![Chat](docs/screenshots/chat.png)

---

## 1. Chạy nhanh

Yêu cầu: Python ≥ 3.11.

```powershell
pip install -r requirements.txt
.\run.ps1                 # lần đầu tự sinh dữ liệu giả (~1 phút) rồi mở server
# hoặc: ./run.sh  (Linux/macOS/Git Bash)
```

Mở <http://127.0.0.1:8000> và đăng nhập bằng nút tài khoản demo:

| Tài khoản | Mật khẩu | Vai trò | Ghi chú |
|---|---|---|---|
| `admin` | `admin123` | Admin | Quản trị schema/catalog, chất lượng dữ liệu, lineage, eval, cài đặt; xem mọi log |
| `analyst` | `analyst123` | Analyst | Chỉ đọc tầng `dw`, bị chặn cột PII/lương |
| `analyst_bac` | `analyst123` | Analyst | + **RLS**: chỉ thấy dữ liệu Miền Bắc |
| `analyst_nam` | `analyst123` | Analyst | + **RLS**: chỉ thấy dữ liệu Miền Nam |

Bật Claude: `$env:ANTHROPIC_API_KEY="sk-ant-..."` rồi chạy lại (mặc định `claude-opus-5-5`, effort `medium`, có
refusal fallback). Đổi chế độ/model/effort trong trang **Cài đặt** (Admin).

Kiểm thử: `python -m pytest -q tests` (29 test: governance, chi phí, semantic, NLU, audit số liệu, so khớp eval).

---

## 2. Kịch bản demo (10 phút)

1. **analyst** → *“Doanh thu tháng trước theo miền”* → agent **hỏi lại** “doanh thu gộp hay thuần?” (2 phòng ban định
   nghĩa khác nhau) → chọn *thuần* → bảng + biểu đồ + diễn giải; badge `✓ 9/9 số liệu khớp kết quả`.
2. Hỏi tiếp *“còn Miền Nam thì sao?”*, *“so với cùng kỳ năm ngoái”* → agent **nhớ ngữ cảnh** và lựa chọn định nghĩa.
3. *“Doanh thu thuần theo cửa hàng”* (toàn bộ lịch sử, ~33 MB) → **chờ duyệt (HITL)**: xem/sửa SQL, bảng chi phí theo
   bảng/cột/phân vùng → Duyệt & chạy. Tab *Kiểm duyệt*, *Chi phí quét*, *Lineage*, *Pipeline agent*, *Kiểm chứng số liệu*.
4. Đăng nhập **analyst_bac**, hỏi *“Doanh thu thuần theo miền tháng 10/2026”* → chỉ ra Miền Bắc (RLS tiêm vào AST).
5. **SQL Console**: chạy câu mẫu có cột sai `thanh_tien` → agent **tự sửa** thành `tt` (theo mô tả catalog), cảnh báo
   `YEAR(ngay_dh)` làm mất cắt phân vùng. Thử `DROP TABLE`, `SELECT * FROM dw.dim_kh`, `read_csv(...)` → bị chặn.
6. **admin** → *“Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ?”* → phân rã đóng góp theo 6 chiều, drill-down,
   kiểm định z-score, **KS test phát hiện đơn giá bị ghi theo nghìn đồng** ở 10 cửa hàng TP.HCM → kết luận *lỗi dữ liệu*.
7. *“Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm?”* → chỉ ra ngành **Điện thoại** + **tồn kho kho Đà Nẵng ≈ 0**
   (hết hàng) → nguyên nhân *kinh doanh*.
8. **Chất lượng dữ liệu** → finding *Trôi lệch đơn vị đơn giá (POS)* → **Áp dụng quy tắc sửa** (được kiểm duyệt AST) →
   build lại raw → stg → dw → doanh thu tháng 9 được khôi phục (bảng trước/sau). Có nút **Hoàn tác**.
9. **Lineage**: sơ đồ raw → staging → dw; bấm cột `dw.fct_ct_dh.tt` để truy ngược tới `raw.pos_ct_don_hang.don_gia`...
10. **Đánh giá (Eval)**: chạy bộ 41 câu → execution accuracy theo độ khó; **Tối ưu few-shot (train → dev)**.

![Variance](docs/screenshots/variance.png)

---

## 3. Kiến trúc

```
Trình duyệt (web/: HTML + JS thuần + Chart.js)
        │  JWT (HS256, tương thích Supabase Auth)
FastAPI (app/main.py) ──► Agent (app/agent.py) — đồ thị trạng thái kiểu LangGraph:
   load_context ─► planner ─┬─► clarify (hỏi lại, lưu lựa chọn vào bộ nhớ hội thoại)
                            ├─► responder (định nghĩa chỉ số / schema / DQ / trợ giúp)
                            ├─► variance_analyst (insights.py: phân rã, drill-down, kiểm định, nguyên nhân gốc)
                            └─► sql_generator ─► optimizer ─► validator ⇄ repairer (rule-based / Claude)
                                                                 │
                                                  cost_guard ── HITL (chờ duyệt) ── executor ─► analyst ─► explainer ─► finalize (log, bộ nhớ)
   planner = Claude function calling (search_schema, describe_table, get_column_values, dry_run_sql,
             compile_semantic_plan, submit_answer)  |  NLU tiếng Việt offline (nlu.py)
        │
DuckDB read-only (enable_external_access=false, lock_configuration, timeout) ◄─ warehouse/models.sql (raw → stg → dw)
```

| Thành phần | File | Ghi chú |
|---|---|---|
| Semantic layer (dbt semantic model) | `config/semantic_layer.yml`, `app/semantic.py` | 25 chỉ số có chủ sở hữu, 24 chiều, 7 model; derived & đa bảng sự kiện, so sánh kỳ, HAVING, semi-additive |
| Data catalog | `config/catalog.yml`, `app/catalog.py` | mô tả cột viết tắt, độ nhạy (PII/mật); Admin sửa trực tiếp |
| Governance | `config/policies.yml`, `app/governance.py` | RBAC theo schema & cột, RLS bằng AST rewrite, chặn DDL/DML/hàm file |
| Chi phí quét | `app/cost.py` | bytes = Σ dòng sau cắt phân vùng × độ rộng cột tham chiếu; ngưỡng cảnh báo / duyệt / chặn theo vai trò |
| Phân tích tĩnh & lineage | `app/sqlcheck.py`, `app/lineage.py` | lint L001–L010 (fan-out, semi-additive, thiếu lọc trạng thái, không sargable…), đối chiếu định nghĩa chỉ số, lineage cột tới raw |
| Diễn giải có kiểm chứng | `app/explain.py` | facts có công thức tái tính; văn bản chỉ chèn số qua placeholder; audit mọi token số |
| Sửa lỗi & tối ưu SQL | `app/repair.py` | sửa cột/bảng/GROUP BY/fan-out/chia 0; viết lại `YEAR()`/`STRFTIME()` thành khoảng ngày |
| Phân tích biến động | `app/insights.py` | z-score (MoM/YoY), phân rã theo “lift”, drill-down, số đơn × giá trị đơn, KS test unit drift, đóng cửa hàng, hết hàng, lịch KM |
| Chất lượng & drift | `app/dq.py` | schema drift (data contract), mapping, trùng, mồ côi, unit drift (change-point + KS), outlier ×10, volume, PSI + chi-square, freshness; quy tắc sửa + rebuild |
| RAG schema & few-shot | `app/retriever.py`, `config/fewshots.yml` | BM25 (thay được bằng Qdrant/pgvector), tái sử dụng SQL đã xác nhận |
| Eval & tự tối ưu | `eval/questions.json`, `app/evals.py` | Execution accuracy chuẩn Spider 2.0; học few-shot từ lỗi train, đo trên dev |
| Claude | `app/llm.py` | tool use, structured outputs, prompt caching, `fallbacks: "default"` |

---

## 4. Bộ dữ liệu giả

`warehouse/generate.py` sinh kho dữ liệu của chuỗi bán lẻ điện máy hư cấu (43 cửa hàng, 5 kho, ~300 SKU,
01/01/2025 → 09/11/2026, ~547k đơn, ~900k dòng) từ **7 hệ thống nguồn** với dữ liệu bẩn thực tế:

| Nguồn | Bảng raw | “Bẩn” có chủ đích |
|---|---|---|
| POS cửa hàng | `pos_don_hang`, `pos_ct_don_hang`, `pos_cua_hang` | mã trạng thái lẫn lộn (`HT`, `ht`, `Hoàn thành`), số lượng âm, tiền CK NULL, giá gõ thừa số 0 |
| E-commerce | `ecom_orders`, `ecom_order_items` | cột tiếng Anh, ngày dạng chuỗi `DD/MM/YYYY`, `COMPLETED `/`canceled`/`pending_review`, dòng trùng, cột mới `voucher_code` |
| CRM | `crm_khach_hang` | tỉnh viết nhiều kiểu (`tp hcm`, `Sài Gòn`, `HN`…), giới tính `M/Nam/nam/Male`, trùng SĐT |
| ERP | `erp_san_pham`, `erp_ton_kho`, `erp_nhap_kho`, `erp_tra_hang` | tồn kho tuần (semi-additive), phiếu nhập, trả hàng |
| HR | `hr_nhan_vien` | lương (dữ liệu mật) |
| Tài chính | `fin_ke_hoach` | kế hoạch doanh thu tháng × cửa hàng |
| Marketing | `mkt_chi_phi` | chi phí theo tháng × kênh |

Model (`warehouse/models.sql`) làm sạch thành 4 bảng staging và **18 bảng dw** (`fct_ct_dh`, `fct_dh`, `fct_th`,
`fct_tk_tuan`, `fct_nk`, `fct_kh_dt`, `fct_cp_mkt`, 11 dimension) với tên cột viết tắt như kho thật.

**Sự cố cài sẵn để agent phát hiện:** (1) toàn bộ POS TP.HCM ghi đơn giá theo *nghìn đồng* từ 15/08/2026; (2) hết hàng
điện thoại ở kho Đà Nẵng tháng 6/2026; (3) cửa hàng Hải An đóng cửa 30/04/2026; (4) mã trạng thái chưa map; (5) dòng trùng,
số lượng âm, giá ×10; (6) khách trùng SĐT, đơn tham chiếu khách chưa có trong CRM; (7) schema drift `voucher_code`;
(8) ngày cuối nạp thiếu ~60%. Quy mô chỉnh bằng `python warehouse/generate.py --scale 0.5`.

---

## 5. Kết quả đo (chế độ offline, không LLM)

| Chỉ số | Giá trị |
|---|---|
| Execution accuracy (41 câu) | **87,8%** — easy 100% · medium 100% · hard 100% · extra 28,6% |
| Diễn giải qua audit số liệu | 100% |
| Độ trễ trung bình | ~0,2 s/câu |
| Tự tối ưu few-shot (dev) | **85% → 95%** sau khi học từ lỗi trên train (dev không bao giờ được đưa vào kho few-shot) |

Các câu “extra” là câu tự do nằm ngoài semantic layer (top khách hàng, sản phẩm ngừng KD còn tồn, trung bình theo thứ…):
offline chỉ giải được khi đã có SQL được xác nhận cho câu tương tự; với Claude, agent tự viết SQL qua `dry_run_sql`.
**Chưa đo được độ chính xác ở chế độ Claude** vì máy dựng chưa có API key — vòng lặp tool đã được kiểm thử bằng client giả lập.

---

## 6. API chính

`POST /api/login` · `POST /api/chat` · `POST /api/approve` · `POST /api/sql/validate` · `POST /api/sql/run` ·
`POST /api/feedback` · `GET /api/schema` · `PUT /api/admin/catalog` · `GET /api/logs` (+ `.csv`) · `GET|POST /api/dq…` ·
`GET /api/lineage` · `POST /api/eval/run` · `POST /api/eval/optimize` · `GET|PUT /api/admin/settings`. Tài liệu tự sinh:
<http://127.0.0.1:8000/docs>.

## 7. Triển khai

`Dockerfile` sinh dữ liệu lúc build (`DATA_SCALE=0.5`); `render.yaml` cho Render (Railway dùng chung Dockerfile).
Đặt `APP_SECRET`, tùy chọn `ANTHROPIC_API_KEY`, `SUPABASE_JWT_SECRET` (đăng nhập bằng Supabase Auth, vai trò lấy từ
`app_metadata.role`, RLS từ `app_metadata.rls`).

## 8. Khác biệt so với techstack gợi ý & hướng mở rộng

- **Frontend** là HTML/JS thuần + Chart.js (giao diện test, không cần build) thay cho Next.js + Recharts; API đã tách
  sẵn để thay frontend.
- **LangGraph** → đồ thị trạng thái tự viết cùng mô hình node/edge (không thêm phụ thuộc, trace từng node trên UI).
- **Vector DB** → BM25 local; interface `search_schema/search_fewshots` thay được bằng Qdrant/pgvector.
- **Warehouse** → DuckDB (mô phỏng phân vùng theo ngày & bytes-scanned kiểu BigQuery); SQL dialect qua sqlglot nên
  chuyển sang BigQuery/Snowflake/Trino chủ yếu là đổi `DIALECT` và connector.
- NLU offline chỉ bao phủ các câu hỏi biểu diễn được bằng semantic layer; câu tự do cần Claude.
