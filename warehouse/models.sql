-- =====================================================================================
-- Các model biến đổi dữ liệu kiểu dbt: raw/ref -> stg -> dw
-- Mỗi model bắt đầu bằng dòng "-- @model <schema.table>" và có mô tả "-- @desc ...".
-- {{ rule('slot', 'biểu_thức_mặc_định') }} là hook để cài quy tắc sửa dữ liệu (DQ fix) đã được Admin duyệt.
-- =====================================================================================

-- @model stg.stg_khach_hang
-- @desc Chuẩn hóa khách hàng CRM: giới tính, tỉnh/thành qua bảng alias, năm sinh
CREATE OR REPLACE TABLE stg.stg_khach_hang AS
SELECT c.ma_kh,
       TRIM(c.ho_ten) AS ho_ten,
       CASE WHEN LOWER(TRIM(c.gioi_tinh)) IN ('m', 'nam', 'male') THEN 'Nam'
            WHEN LOWER(TRIM(c.gioi_tinh)) IN ('f', 'nữ', 'female') THEN 'Nữ'
            ELSE 'Không rõ' END AS gt,
       YEAR(TRY_CAST(c.ngay_sinh AS DATE)) AS nam_sinh,
       COALESCE(a.tinh_tp, 'Không xác định') AS tinh_tp,
       a.ma_vung,
       c.hang_the AS hang_tv,
       c.ngay_dang_ky AS ngay_dk,
       c.sdt,
       c.email,
       c.dia_chi_tinh AS tinh_goc
FROM raw.crm_khach_hang c
LEFT JOIN ref.tinh_alias a ON a.alias = LOWER(TRIM(c.dia_chi_tinh));

-- @model stg.stg_don_hang
-- @desc Hợp nhất đơn POS + E-commerce, chuẩn hóa trạng thái qua ref.status_map, parse ngày dạng chuỗi DD/MM/YYYY
CREATE OR REPLACE TABLE stg.stg_don_hang AS
WITH pos AS (
    SELECT p.so_hd AS ma_dh,
           CAST(p.ngay_gio AS DATE) AS ngay_dh,
           p.ngay_gio AS tg_dh,
           NULLIF(TRIM(p.ma_kh), '') AS ma_kh,
           p.ma_ch AS ma_cn,
           'CH' AS ma_kenh,
           p.ma_nv,
           p.ma_ctkm AS ma_km,
           COALESCE(sm.trang_thai_chuan, 'KHAC') AS trang_thai,
           p.tt_don AS trang_thai_goc,
           p.hinh_thuc_tt AS pttt,
           CAST(0 AS DOUBLE) AS phi_vc,
           'POS' AS nguon
    FROM raw.pos_don_hang p
    LEFT JOIN ref.status_map sm ON sm.nguon = 'POS' AND sm.ma_goc = LOWER(TRIM(p.tt_don))
), ecom AS (
    SELECT e.order_id AS ma_dh,
           CAST(STRPTIME(e.order_date, '%d/%m/%Y %H:%M:%S') AS DATE) AS ngay_dh,
           STRPTIME(e.order_date, '%d/%m/%Y %H:%M:%S') AS tg_dh,
           e.customer_id AS ma_kh,
           e.fulfill_store AS ma_cn,
           CASE LOWER(e.channel) WHEN 'web' THEN 'WEB' WHEN 'app' THEN 'APP' ELSE 'SAN' END AS ma_kenh,
           CAST(NULL AS VARCHAR) AS ma_nv,
           e.promo_code AS ma_km,
           COALESCE(sm.trang_thai_chuan, 'KHAC') AS trang_thai,
           e.status AS trang_thai_goc,
           CASE e.payment_method WHEN 'cod' THEN 'COD' WHEN 'card' THEN 'Thẻ ngân hàng'
                WHEN 'bank_transfer' THEN 'Chuyển khoản QR' WHEN 'ewallet' THEN 'Ví điện tử'
                WHEN 'installment' THEN 'Trả góp' ELSE 'Khác' END AS pttt,
           e.shipping_fee AS phi_vc,
           'ECOM' AS nguon
    FROM raw.ecom_orders e
    LEFT JOIN ref.status_map sm ON sm.nguon = 'ECOM' AND sm.ma_goc = LOWER(TRIM(e.status))
)
SELECT * FROM pos
UNION ALL
SELECT * FROM ecom;

-- @model stg.stg_ct_don_hang
-- @desc Hợp nhất dòng đơn: loại dòng trùng (ingest replay), loại số lượng <= 0, NULL chiết khấu -> 0, hook sửa đơn giá
CREATE OR REPLACE TABLE stg.stg_ct_don_hang AS
WITH pos AS (
    SELECT l.so_hd AS ma_dh, l.stt AS dong, l.ma_hang AS ma_sp, l.so_luong AS sl,
           l.don_gia AS dg_goc, COALESCE(l.tien_ck, 0) AS ck_goc, 'POS' AS nguon
    FROM raw.pos_ct_don_hang l
    WHERE l.so_luong > 0
), ecom AS (
    SELECT d.order_id AS ma_dh, d.line_no AS dong, d.sku AS ma_sp, d.qty AS sl,
           d.unit_price AS dg_goc, COALESCE(d.discount_amt, 0) AS ck_goc, 'ECOM' AS nguon
    FROM (
        SELECT i.*, ROW_NUMBER() OVER (PARTITION BY i.order_id, i.line_no ORDER BY i.sku) AS rn
        FROM raw.ecom_order_items i
    ) d
    WHERE d.rn = 1 AND d.qty > 0
), u AS (
    SELECT * FROM pos
    UNION ALL
    SELECT * FROM ecom
)
SELECT u.ma_dh,
       u.dong,
       u.ma_sp,
       u.sl,
       {{ rule('dg', 'u.dg_goc') }} AS dg,
       {{ rule('ck', 'u.ck_goc') }} AS ck,
       u.dg_goc,
       u.nguon
FROM u
JOIN stg.stg_don_hang h ON h.ma_dh = u.ma_dh
JOIN raw.erp_san_pham sp ON sp.ma_hang = u.ma_sp;

-- @model stg.stg_tra_hang
-- @desc Phiếu trả hàng ERP gắn với đơn gốc để lấy cửa hàng/kênh
CREATE OR REPLACE TABLE stg.stg_tra_hang AS
SELECT r.so_phieu AS ma_th,
       r.ma_don AS ma_dh,
       r.ma_hang AS ma_sp,
       h.ma_cn,
       h.ma_kenh,
       r.ngay_tra,
       r.sl AS sl_tra,
       r.ly_do,
       r.tien_hoan
FROM raw.erp_tra_hang r
LEFT JOIN stg.stg_don_hang h ON h.ma_dh = r.ma_don;

-- @model dw.dim_ngay
-- @desc Lịch ngày: năm, quý, tháng, tuần, thứ, cuối tuần, ngày lễ
CREATE OR REPLACE TABLE dw.dim_ngay AS
SELECT CAST(g.d AS DATE) AS ngay,
       YEAR(g.d) AS nam,
       QUARTER(g.d) AS quy,
       MONTH(g.d) AS thang,
       WEEKOFYEAR(g.d) AS tuan,
       ISODOW(g.d) AS thu,
       CASE ISODOW(g.d) WHEN 7 THEN 'Chủ nhật' ELSE 'Thứ ' || CAST(ISODOW(g.d) + 1 AS VARCHAR) END AS ten_thu,
       ISODOW(g.d) >= 6 AS cuoi_tuan,
       l.ten_le IS NOT NULL AS ngay_le,
       l.ten_le,
       STRFTIME(g.d, '%Y-%m') AS nam_thang
FROM generate_series(TIMESTAMP '2025-01-01', TIMESTAMP '2026-12-31', INTERVAL 1 DAY) AS g(d)
LEFT JOIN ref.ngay_le l ON l.ngay = CAST(g.d AS DATE);

-- @model dw.dim_vung
-- @desc Vùng miền
CREATE OR REPLACE TABLE dw.dim_vung AS
SELECT v.ma_vung, v.ten_vung FROM ref.vung v;

-- @model dw.dim_kho
-- @desc Kho / trung tâm phân phối
CREATE OR REPLACE TABLE dw.dim_kho AS
SELECT k.ma_kho, k.ten_kho, k.ma_vung, k.tinh_tp FROM ref.kho k;

-- @model dw.dim_kenh
-- @desc Kênh bán hàng
CREATE OR REPLACE TABLE dw.dim_kenh AS
SELECT k.ma_kenh, k.ten_kenh, k.nhom_kenh FROM ref.kenh k;

-- @model dw.dim_nh
-- @desc Ngành hàng
CREATE OR REPLACE TABLE dw.dim_nh AS
SELECT n.ma_nh, n.ten_nh FROM ref.nganh_hang n;

-- @model dw.dim_ncc
-- @desc Nhà cung cấp
CREATE OR REPLACE TABLE dw.dim_ncc AS
SELECT n.ma_ncc, n.ten_ncc, n.quoc_gia FROM ref.nha_cung_cap n;

-- @model dw.dim_km
-- @desc Chương trình khuyến mãi
CREATE OR REPLACE TABLE dw.dim_km AS
SELECT k.ma_km, k.ten_km, k.loai_km, k.tu_ngay, k.den_ngay, k.pct_giam FROM ref.khuyen_mai k;

-- @model dw.dim_cn
-- @desc Chi nhánh / cửa hàng
CREATE OR REPLACE TABLE dw.dim_cn AS
SELECT c.ma_ch AS ma_cn,
       c.ten_ch AS ten_cn,
       c.ma_vung,
       c.tinh_tp,
       c.loai_ch,
       c.dien_tich_m2 AS dt_m2,
       CAST(c.ngay_khai_truong AS DATE) AS ngay_mo,
       CAST(c.ngay_dong_cua AS DATE) AS ngay_dong,
       c.ma_kho,
       CASE WHEN c.ngay_dong_cua IS NOT NULL THEN 'Đã đóng cửa' ELSE 'Đang hoạt động' END AS trang_thai
FROM raw.pos_cua_hang c;

-- @model dw.dim_sp
-- @desc Sản phẩm (SKU)
CREATE OR REPLACE TABLE dw.dim_sp AS
SELECT s.ma_hang AS ma_sp,
       s.ten_hang AS ten_sp,
       s.nganh_hang AS ma_nh,
       s.nhom_hang AS nhom_sp,
       s.thuong_hieu,
       s.gia_von,
       s.gia_ban_le AS gia_niem_yet,
       CAST(s.ngay_ra_mat AS DATE) AS ngay_ra_mat,
       s.ma_ncc,
       s.trang_thai_kd
FROM raw.erp_san_pham s;

-- @model dw.dim_kh
-- @desc Khách hàng (có PII: ho_ten, sdt, email)
CREATE OR REPLACE TABLE dw.dim_kh AS
SELECT k.ma_kh, k.ho_ten, k.gt, k.nam_sinh, k.tinh_tp, k.ma_vung, k.hang_tv, k.ngay_dk, k.sdt, k.email
FROM stg.stg_khach_hang k;

-- @model dw.dim_nv
-- @desc Nhân viên (luong_cb là dữ liệu nhạy cảm)
CREATE OR REPLACE TABLE dw.dim_nv AS
SELECT n.ma_nv,
       n.ho_ten,
       n.ma_ch AS ma_cn,
       n.chuc_danh AS chuc_vu,
       CAST(n.ngay_vao_lam AS DATE) AS ngay_vao,
       n.gioi_tinh AS gt,
       n.luong_co_ban AS luong_cb
FROM raw.hr_nhan_vien n;

-- @model dw.fct_ct_dh
-- @desc Sự kiện cấp DÒNG đơn hàng (grain ma_dh+dong). tt = sl*dg - ck. Hàng trả gán vào dòng đầu tiên của SP trong đơn.
CREATE OR REPLACE TABLE dw.fct_ct_dh AS
WITH tra AS (
    SELECT t.ma_dh, t.ma_sp, SUM(t.sl_tra) AS sl_tra, SUM(t.tien_hoan) AS tien_tra
    FROM stg.stg_tra_hang t
    GROUP BY t.ma_dh, t.ma_sp
), l AS (
    SELECT c.ma_dh, c.dong, c.ma_sp, c.sl, c.dg, c.ck,
           ROW_NUMBER() OVER (PARTITION BY c.ma_dh, c.ma_sp ORDER BY c.dong) AS rn
    FROM stg.stg_ct_don_hang c
)
SELECT l.ma_dh,
       l.dong,
       h.ngay_dh,
       h.ma_cn,
       h.ma_kenh,
       h.ma_kh,
       h.ma_km,
       h.pttt,
       l.ma_sp,
       h.trang_thai,
       l.sl,
       l.dg,
       l.ck,
       l.sl * l.dg - l.ck AS tt,
       sp.gia_von,
       CASE WHEN l.rn = 1 THEN COALESCE(tra.sl_tra, 0) ELSE 0 END AS sl_tra,
       CASE WHEN l.rn = 1 THEN COALESCE(tra.tien_tra, 0) ELSE 0 END AS tien_tra
FROM l
JOIN stg.stg_don_hang h ON h.ma_dh = l.ma_dh
LEFT JOIN raw.erp_san_pham sp ON sp.ma_hang = l.ma_sp
LEFT JOIN tra ON tra.ma_dh = l.ma_dh AND tra.ma_sp = l.ma_sp
ORDER BY h.ngay_dh;

-- @model dw.fct_dh
-- @desc Sự kiện cấp ĐƠN hàng (grain ma_dh). tong_tt = tổng thành tiền sau chiết khấu (chưa gồm phí vận chuyển)
CREATE OR REPLACE TABLE dw.fct_dh AS
WITH a AS (
    SELECT c.ma_dh, COUNT(*) AS so_dong, SUM(c.sl * c.dg) AS tien_hang, SUM(c.ck) AS tien_ck
    FROM stg.stg_ct_don_hang c
    GROUP BY c.ma_dh
)
SELECT h.ma_dh,
       h.ngay_dh,
       h.tg_dh,
       h.ma_kh,
       h.ma_cn,
       h.ma_kenh,
       h.ma_nv,
       h.ma_km,
       h.trang_thai,
       h.pttt,
       h.phi_vc,
       COALESCE(a.so_dong, 0) AS so_dong,
       COALESCE(a.tien_hang, 0) AS tien_hang,
       COALESCE(a.tien_ck, 0) AS tien_ck,
       COALESCE(a.tien_hang, 0) - COALESCE(a.tien_ck, 0) AS tong_tt,
       h.nguon
FROM stg.stg_don_hang h
LEFT JOIN a ON a.ma_dh = h.ma_dh
ORDER BY h.ngay_dh;

-- @model dw.fct_th
-- @desc Sự kiện trả hàng
CREATE OR REPLACE TABLE dw.fct_th AS
SELECT t.ma_th, t.ma_dh, t.ma_sp, t.ma_cn, t.ma_kenh, t.ngay_tra, t.sl_tra, t.ly_do, t.tien_hoan
FROM stg.stg_tra_hang t
ORDER BY t.ngay_tra;

-- @model dw.fct_tk_tuan
-- @desc Ảnh chụp tồn kho cuối tuần (Chủ nhật) theo kho x SP. SEMI-ADDITIVE: không cộng dồn qua thời gian
CREATE OR REPLACE TABLE dw.fct_tk_tuan AS
SELECT t.ngay_chot,
       t.ma_kho,
       t.ma_hang AS ma_sp,
       t.ton_cuoi AS sl_ton,
       t.dang_ve AS sl_dang_ve,
       t.ton_cuoi * sp.gia_von AS gia_tri_ton
FROM raw.erp_ton_kho t
LEFT JOIN raw.erp_san_pham sp ON sp.ma_hang = t.ma_hang
ORDER BY t.ngay_chot;

-- @model dw.fct_nk
-- @desc Phiếu nhập kho từ nhà cung cấp
CREATE OR REPLACE TABLE dw.fct_nk AS
SELECT n.so_pn AS ma_pn, n.ngay_nhap AS ngay_nk, n.ma_kho, n.ma_ncc, n.ma_hang AS ma_sp,
       n.sl AS sl_nhap, n.dg_nhap, n.sl * n.dg_nhap AS tt_nhap
FROM raw.erp_nhap_kho n
ORDER BY n.ngay_nhap;

-- @model dw.fct_kh_dt
-- @desc Kế hoạch doanh thu thuần theo tháng x cửa hàng (Phòng Tài chính)
CREATE OR REPLACE TABLE dw.fct_kh_dt AS
SELECT MAKE_DATE(k.nam, k.thang, 1) AS thang, k.ma_ch AS ma_cn, k.dt_ke_hoach
FROM raw.fin_ke_hoach k;

-- @model dw.fct_cp_mkt
-- @desc Chi phí marketing theo tháng x kênh
CREATE OR REPLACE TABLE dw.fct_cp_mkt AS
SELECT m.thang, m.kenh AS ma_kenh, m.chi_phi FROM raw.mkt_chi_phi m;
