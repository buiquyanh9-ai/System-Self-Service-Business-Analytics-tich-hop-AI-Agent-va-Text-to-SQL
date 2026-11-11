"""
Sinh dữ liệu giả cho kho dữ liệu doanh nghiệp bán lẻ "Công ty CP Bán lẻ Điện máy ABC" (hư cấu).

Bảy hệ thống nguồn -> schema `raw` (dữ liệu bẩn đúng như khi ingest) + dữ liệu tham chiếu `ref` (seed):
  POS (cửa hàng)      raw.pos_don_hang, raw.pos_ct_don_hang
  E-commerce          raw.ecom_orders, raw.ecom_order_items       (cột tiếng Anh, ngày dạng chuỗi)
  CRM                 raw.crm_khach_hang                          (tỉnh/giới tính không chuẩn, trùng KH)
  ERP                 raw.erp_san_pham, raw.erp_ton_kho, raw.erp_nhap_kho, raw.erp_tra_hang
  HR                  raw.hr_nhan_vien                            (lương - dữ liệu nhạy cảm)
  Tài chính           raw.fin_ke_hoach                            (kế hoạch doanh thu)
  Marketing           raw.mkt_chi_phi

Các "sự cố" được cài sẵn để agent / bộ kiểm định phát hiện:
  1. Trôi lệch đơn vị (unit drift): các cửa hàng TP.HCM nâng cấp POS v5 từ 15/08/2026 -> don_gia ghi theo NGHÌN đồng.
  2. Hết hàng (stockout): Điện thoại tại kho Đà Nẵng (Miền Trung) tháng 06/2026 -> doanh số Miền Trung giảm.
  3. Đóng cửa hàng: ABC Hải An (Hải Phòng) đóng cửa 30/04/2026.
  4. Mã trạng thái bẩn ('COMPLETED ', 'canceled', 'Hoàn thành', 'pending_review' ...).
  5. Dòng trùng lặp (ingest replay) trong ecom_order_items, số lượng âm trong POS, giá nhập nhầm (x10).
  6. Khách hàng trùng SĐT, tỉnh/thành viết nhiều kiểu, đơn online tham chiếu KH chưa có trong CRM.
  7. Schema drift: ecom_orders có thêm cột voucher_code từ 05/2026 (không có trong data contract).
  8. Dữ liệu ngày cuối (09/11/2026) mới nạp một phần (partial load).
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import time

import duckdb
import numpy as np
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "var" / "warehouse.duckdb"

START = dt.date(2025, 1, 1)
END = dt.date(2026, 11, 9)          # ngày dữ liệu cuối cùng (nạp một phần)
DRIFT_START = dt.date(2026, 8, 15)  # POS v5 rollout
STOCKOUT_MONTH = (2026, 6)

# --------------------------------------------------------------------------------------
# Dữ liệu tham chiếu
# --------------------------------------------------------------------------------------
REGIONS = [("B", "Miền Bắc"), ("T", "Miền Trung"), ("N", "Miền Nam")]

WAREHOUSES = [
    ("KHO_HN", "Kho tổng Hà Nội", "B", "Hà Nội"),
    ("KHO_HP", "Kho Hải Phòng", "B", "Hải Phòng"),
    ("KHO_DN", "Kho Đà Nẵng", "T", "Đà Nẵng"),
    ("KHO_HCM", "Kho tổng TP.HCM", "N", "TP.HCM"),
    ("KHO_CT", "Kho Cần Thơ", "N", "Cần Thơ"),
]

# (tỉnh, vùng, kho, [(tên khu vực, loại cửa hàng, ngày mở, ngày đóng)])
STORE_LAYOUT = [
    ("Hà Nội", "B", "KHO_HN", [("Cầu Giấy", "Flagship"), ("Đống Đa", "Tiêu chuẩn"), ("Hai Bà Trưng", "Tiêu chuẩn"),
                                ("Long Biên", "Tiêu chuẩn"), ("Hà Đông", "Tiêu chuẩn"), ("Thanh Xuân", "Tiêu chuẩn"),
                                ("Hoàng Mai", "Tiêu chuẩn"), ("Tây Hồ", "Mini")]),
    ("Hải Phòng", "B", "KHO_HP", [("Lê Chân", "Tiêu chuẩn"), ("Ngô Quyền", "Tiêu chuẩn"),
                                   ("Hải An", "Mini", None, "2026-04-30")]),
    ("Quảng Ninh", "B", "KHO_HP", [("Hạ Long", "Tiêu chuẩn"), ("Cẩm Phả", "Mini")]),
    ("Bắc Ninh", "B", "KHO_HN", [("TP Bắc Ninh", "Tiêu chuẩn"), ("Từ Sơn", "Mini")]),
    ("Nam Định", "B", "KHO_HN", [("TP Nam Định", "Tiêu chuẩn")]),
    ("Đà Nẵng", "T", "KHO_DN", [("Hải Châu", "Flagship"), ("Thanh Khê", "Tiêu chuẩn"), ("Sơn Trà", "Mini")]),
    ("Thừa Thiên Huế", "T", "KHO_DN", [("TP Huế", "Tiêu chuẩn"), ("Hương Thủy", "Mini")]),
    ("Nghệ An", "T", "KHO_DN", [("TP Vinh", "Tiêu chuẩn"), ("Cửa Lò", "Mini")]),
    ("Khánh Hòa", "T", "KHO_DN", [("Nha Trang", "Tiêu chuẩn"), ("Cam Ranh", "Mini")]),
    ("Bình Định", "T", "KHO_DN", [("Quy Nhơn", "Tiêu chuẩn")]),
    ("TP.HCM", "N", "KHO_HCM", [("Quận 1", "Flagship"), ("Quận 3", "Tiêu chuẩn"), ("Quận 7", "Tiêu chuẩn"),
                                 ("Bình Thạnh", "Tiêu chuẩn"), ("Gò Vấp", "Tiêu chuẩn"), ("Tân Bình", "Tiêu chuẩn"),
                                 ("Thủ Đức", "Flagship"), ("Quận 10", "Tiêu chuẩn"), ("Bình Tân", "Tiêu chuẩn"),
                                 ("Phú Nhuận", "Tiêu chuẩn", "2025-07-01", None)]),
    ("Cần Thơ", "N", "KHO_CT", [("Ninh Kiều", "Tiêu chuẩn"), ("Cái Răng", "Mini")]),
    ("Đồng Nai", "N", "KHO_HCM", [("Biên Hòa", "Tiêu chuẩn"), ("Long Khánh", "Mini")]),
    ("Bình Dương", "N", "KHO_HCM", [("Thủ Dầu Một", "Tiêu chuẩn"), ("Dĩ An", "Tiêu chuẩn")]),
    ("Bà Rịa - Vũng Tàu", "N", "KHO_HCM", [("Vũng Tàu", "Tiêu chuẩn")]),
]
DRIFT_PROVINCE = "TP.HCM"   # toàn bộ cửa hàng TP.HCM nâng cấp POS v5

PROVINCE_ALIASES = {
    "Hà Nội": ["Hà Nội", "Ha Noi", "HN", "TP Hà Nội", "hà nội", "Hanoi"],
    "Hải Phòng": ["Hải Phòng", "Hai Phong", "HP", "TP. Hải Phòng"],
    "Quảng Ninh": ["Quảng Ninh", "Quang Ninh", "QN"],
    "Bắc Ninh": ["Bắc Ninh", "Bac Ninh"],
    "Nam Định": ["Nam Định", "Nam Dinh"],
    "Đà Nẵng": ["Đà Nẵng", "Da Nang", "ĐN", "TP Đà Nẵng", "đà nẵng"],
    "Thừa Thiên Huế": ["Thừa Thiên Huế", "Huế", "Hue", "TT Huế"],
    "Nghệ An": ["Nghệ An", "Nghe An"],
    "Khánh Hòa": ["Khánh Hòa", "Khanh Hoa", "Khánh Hoà"],
    "Bình Định": ["Bình Định", "Binh Dinh"],
    "TP.HCM": ["TP.HCM", "Hồ Chí Minh", "TP Hồ Chí Minh", "tp hcm", "HCM", "Sài Gòn", "Ho Chi Minh City"],
    "Cần Thơ": ["Cần Thơ", "Can Tho"],
    "Đồng Nai": ["Đồng Nai", "Dong Nai"],
    "Bình Dương": ["Bình Dương", "Binh Duong", "BD"],
    "Bà Rịa - Vũng Tàu": ["Bà Rịa - Vũng Tàu", "Vũng Tàu", "BR-VT", "Vung Tau"],
}

CHANNELS = [("CH", "Tại cửa hàng", "Offline"), ("WEB", "Website", "Online"),
            ("APP", "Ứng dụng di động", "Online"), ("SAN", "Sàn TMĐT", "Online")]

CATEGORIES = [  # ma_nh, ten_nh, tỷ trọng dòng cơ bản
    ("DT", "Điện thoại", 0.24), ("LT", "Laptop", 0.08), ("MTB", "Máy tính bảng", 0.05),
    ("PK", "Phụ kiện", 0.30), ("DHTM", "Đồng hồ thông minh", 0.05), ("TV", "Tivi", 0.05),
    ("AT", "Âm thanh", 0.06), ("GD", "Gia dụng", 0.11), ("DL", "Điện lạnh", 0.06),
]

# ma_nh -> list (thương hiệu, nhóm, mẫu tên, giá thấp, giá cao, số SKU)
PRODUCT_SPEC = {
    "DT": [("Apple", "iPhone", ["iPhone 15", "iPhone 15 Plus", "iPhone 16", "iPhone 16 Pro", "iPhone 16 Pro Max",
                                 "iPhone 17", "iPhone 17 Pro", "iPhone 17 Pro Max"], 17e6, 42e6),
           ("Samsung", "Galaxy", ["Galaxy S25", "Galaxy S25 Ultra", "Galaxy A56", "Galaxy A36", "Galaxy A16",
                                  "Galaxy Z Flip7", "Galaxy Z Fold7", "Galaxy S26"], 3.5e6, 45e6),
           ("Xiaomi", "Redmi", ["Redmi Note 14", "Redmi Note 14 Pro", "Redmi 15C", "Xiaomi 15T", "Xiaomi 15T Pro"],
            2.5e6, 18e6),
           ("OPPO", "OPPO", ["OPPO Reno14", "OPPO Reno14 F", "OPPO A5 Pro", "OPPO Find X9"], 4e6, 25e6),
           ("vivo", "vivo", ["vivo V60", "vivo Y39", "vivo X300"], 4e6, 22e6),
           ("realme", "realme", ["realme 15", "realme C75"], 3e6, 10e6)],
    "LT": [("Dell", "Laptop", ["Dell Inspiron 14", "Dell Vostro 15", "Dell XPS 13", "Dell Latitude 7450"], 13e6, 48e6),
           ("HP", "Laptop", ["HP Pavilion 15", "HP Victus 16", "HP EliteBook 840", "HP 240 G10"], 11e6, 40e6),
           ("Asus", "Laptop", ["Asus Vivobook 15", "Asus Zenbook 14", "Asus TUF Gaming F15", "Asus ROG Strix G16"],
            12e6, 52e6),
           ("Lenovo", "Laptop", ["Lenovo IdeaPad Slim 5", "Lenovo ThinkPad E14", "Lenovo LOQ 15", "Lenovo Yoga 7"],
            12e6, 38e6),
           ("Apple", "MacBook", ["MacBook Air M4 13", "MacBook Air M4 15", "MacBook Pro M5 14"], 25e6, 55e6),
           ("Acer", "Laptop", ["Acer Aspire 7", "Acer Nitro V 15"], 13e6, 25e6)],
    "MTB": [("Apple", "iPad", ["iPad Gen 11", "iPad Air M3", "iPad Pro M5", "iPad mini 7"], 9e6, 35e6),
            ("Samsung", "Galaxy Tab", ["Galaxy Tab S10 FE", "Galaxy Tab A9+", "Galaxy Tab S11"], 4.5e6, 25e6),
            ("Xiaomi", "Xiaomi Pad", ["Xiaomi Pad 7", "Redmi Pad 2"], 4e6, 11e6),
            ("Lenovo", "Lenovo Tab", ["Lenovo Tab M11", "Lenovo Tab P12"], 4e6, 9e6)],
    "PK": [("Anker", "Sạc - Pin", ["Sạc nhanh Anker 30W", "Pin dự phòng Anker 10000mAh", "Sạc Anker Nano 65W",
                                   "Cáp Anker USB-C 1m"], 0.15e6, 1.8e6),
           ("Baseus", "Sạc - Pin", ["Pin dự phòng Baseus 20000mAh", "Cáp Baseus Lightning", "Sạc xe hơi Baseus"],
            0.12e6, 1.2e6),
           ("Ugreen", "Cáp - Hub", ["Hub Ugreen USB-C 6in1", "Cáp Ugreen HDMI 2m", "Đế tản nhiệt Ugreen"],
            0.1e6, 1.0e6),
           ("Apple", "Tai nghe", ["AirPods 4", "AirPods Pro 3", "Sạc Apple 20W", "Apple Pencil Pro"], 0.55e6, 6.5e6),
           ("Samsung", "Tai nghe", ["Galaxy Buds3", "Galaxy Buds3 Pro", "Sạc Samsung 25W"], 0.4e6, 4.5e6),
           ("Xiaomi", "Tai nghe", ["Redmi Buds 6", "Xiaomi Buds 5"], 0.4e6, 2.5e6),
           ("ABC Care", "Ốp lưng - Dán", ["Ốp lưng iPhone", "Ốp lưng Galaxy", "Dán cường lực", "Túi chống sốc laptop"],
            0.08e6, 0.6e6)],
    "DHTM": [("Apple", "Apple Watch", ["Apple Watch SE 3", "Apple Watch Series 11", "Apple Watch Ultra 3"], 6e6, 22e6),
             ("Samsung", "Galaxy Watch", ["Galaxy Watch8", "Galaxy Watch8 Classic", "Galaxy Fit3"], 1.2e6, 10e6),
             ("Garmin", "Garmin", ["Garmin Forerunner 265", "Garmin Venu 3"], 8e6, 12e6),
             ("Xiaomi", "Mi Band", ["Xiaomi Smart Band 10", "Redmi Watch 5"], 0.8e6, 2.5e6),
             ("Huawei", "Huawei Watch", ["Huawei Watch Fit 4", "Huawei Watch GT 5"], 2.5e6, 6e6)],
    "TV": [("Samsung", "Tivi", ["Smart TV Samsung 43 inch", "Smart TV Samsung 55 inch QLED", "Neo QLED Samsung 65 inch"],
            7e6, 45e6),
           ("LG", "Tivi", ["Smart TV LG 50 inch", "OLED LG 55 inch", "OLED LG 65 inch evo"], 9e6, 55e6),
           ("Sony", "Tivi", ["Google TV Sony 55 inch", "Google TV Sony 65 inch Bravia 7"], 15e6, 50e6),
           ("TCL", "Tivi", ["Google TV TCL 43 inch", "QD-Mini LED TCL 65 inch"], 5e6, 25e6),
           ("Xiaomi", "Tivi", ["Xiaomi TV A 43 inch", "Xiaomi TV A Pro 55 inch"], 5e6, 12e6),
           ("Casper", "Tivi", ["Casper 32 inch HD", "Casper 43 inch FHD"], 3.5e6, 6e6)],
    "AT": [("JBL", "Loa", ["Loa JBL Flip 7", "Loa JBL Charge 6", "Loa JBL PartyBox 120"], 2.5e6, 11e6),
           ("Sony", "Tai nghe", ["Tai nghe Sony WH-1000XM6", "Tai nghe Sony WF-C710N", "Loa Sony ULT Field 1"],
            1.8e6, 10e6),
           ("Marshall", "Loa", ["Loa Marshall Emberton III", "Loa Marshall Stanmore III"], 4.5e6, 12e6),
           ("Harman Kardon", "Loa", ["Loa Harman Kardon Onyx Studio 9", "Loa Harman Kardon Aura Studio 4"], 5e6, 9e6),
           ("LG", "Loa thanh", ["Loa thanh LG S40T", "Loa thanh LG S70TY"], 3.5e6, 9e6)],
    "GD": [("Philips", "Gia dụng", ["Nồi chiên không dầu Philips 6.2L", "Máy xay sinh tố Philips", "Bàn ủi hơi nước Philips",
                                     "Máy lọc không khí Philips 2000i"], 0.6e6, 8e6),
           ("Sunhouse", "Gia dụng", ["Nồi cơm điện Sunhouse 1.8L", "Bếp từ đôi Sunhouse", "Ấm siêu tốc Sunhouse"],
            0.3e6, 4e6),
           ("Panasonic", "Gia dụng", ["Nồi cơm điện tử Panasonic", "Máy sấy tóc Panasonic", "Lò vi sóng Panasonic"],
            0.5e6, 5e6),
           ("Xiaomi", "Gia dụng", ["Robot hút bụi Xiaomi S20", "Máy lọc không khí Xiaomi 4", "Quạt thông minh Xiaomi"],
            1e6, 9e6),
           ("Kangaroo", "Gia dụng", ["Máy lọc nước Kangaroo", "Cây nước nóng lạnh Kangaroo"], 3e6, 8e6),
           ("Sharp", "Gia dụng", ["Lò vi sóng Sharp", "Máy lọc không khí Sharp"], 1.5e6, 6e6)],
    "DL": [("Daikin", "Máy lạnh", ["Máy lạnh Daikin Inverter 1HP", "Máy lạnh Daikin Inverter 1.5HP",
                                   "Máy lạnh Daikin Inverter 2HP"], 9e6, 22e6),
           ("Panasonic", "Máy lạnh", ["Máy lạnh Panasonic Inverter 1HP", "Máy lạnh Panasonic Inverter 1.5HP"], 9e6, 16e6),
           ("LG", "Tủ lạnh", ["Tủ lạnh LG Inverter 335L", "Tủ lạnh LG Side by Side 635L", "Máy giặt LG 10kg"], 8e6, 32e6),
           ("Samsung", "Tủ lạnh", ["Tủ lạnh Samsung Inverter 380L", "Máy giặt Samsung AI 11kg"], 9e6, 18e6),
           ("Toshiba", "Tủ lạnh", ["Tủ lạnh Toshiba 253L", "Máy giặt Toshiba 9kg"], 6e6, 12e6),
           ("Electrolux", "Máy giặt", ["Máy giặt Electrolux 10kg", "Máy sấy Electrolux 8kg"], 8e6, 15e6),
           ("Aqua", "Tủ lạnh", ["Tủ lạnh Aqua 189L", "Máy giặt Aqua 8kg"], 4.5e6, 7e6)],
}
SUPPLIERS = [
    ("NCC01", "Apple Việt Nam (qua FPT Trading)", "Mỹ"), ("NCC02", "Samsung Vina Electronics", "Hàn Quốc"),
    ("NCC03", "Xiaomi Việt Nam (Digiworld)", "Trung Quốc"), ("NCC04", "OPPO Việt Nam", "Trung Quốc"),
    ("NCC05", "Dell - HP - Lenovo (Synnex FPT)", "Mỹ"), ("NCC06", "Asus - Acer Việt Nam", "Đài Loan"),
    ("NCC07", "Sony Electronics Việt Nam", "Nhật Bản"), ("NCC08", "LG Electronics Việt Nam", "Hàn Quốc"),
    ("NCC09", "Phụ kiện Anker - Baseus - Ugreen (Petrosetco)", "Trung Quốc"),
    ("NCC10", "Daikin - Panasonic Việt Nam", "Nhật Bản"), ("NCC11", "Gia dụng Sunhouse - Kangaroo", "Việt Nam"),
    ("NCC12", "Harman (JBL - Harman Kardon) / Marshall", "Mỹ"), ("NCC13", "Điện máy Toshiba - Electrolux - Aqua", "Nhật Bản"),
]
BRAND_SUPPLIER = {"Apple": "NCC01", "Samsung": "NCC02", "Xiaomi": "NCC03", "OPPO": "NCC04", "vivo": "NCC04",
                  "realme": "NCC04", "Dell": "NCC05", "HP": "NCC05", "Lenovo": "NCC05", "Asus": "NCC06",
                  "Acer": "NCC06", "Sony": "NCC07", "LG": "NCC08", "Anker": "NCC09", "Baseus": "NCC09",
                  "Ugreen": "NCC09", "ABC Care": "NCC09", "Daikin": "NCC10", "Panasonic": "NCC10",
                  "Sunhouse": "NCC11", "Kangaroo": "NCC11", "Philips": "NCC11", "Sharp": "NCC13",
                  "JBL": "NCC12", "Harman Kardon": "NCC12", "Marshall": "NCC12", "Garmin": "NCC12",
                  "Huawei": "NCC03", "TCL": "NCC03", "Casper": "NCC11", "Toshiba": "NCC13",
                  "Electrolux": "NCC13", "Aqua": "NCC13"}

PROMOS = [  # ma_km, tên, loại, từ, đến, % giảm, hệ số lưu lượng
    ("KM2501", "Tết Ất Tỵ 2025", "Lễ Tết", "2025-01-10", "2025-01-27", 0.10, 1.55),
    ("KM2502", "Quốc tế Phụ nữ 8/3", "Ngày lễ", "2025-03-01", "2025-03-08", 0.08, 1.25),
    ("KM2503", "Đại lễ 30/4 - 1/5", "Ngày lễ", "2025-04-25", "2025-05-04", 0.10, 1.35),
    ("KM2504", "Back to School 2025", "Mùa vụ", "2025-08-01", "2025-08-31", 0.07, 1.20),
    ("KM2505", "Siêu sale 9/9", "Flash sale", "2025-09-08", "2025-09-10", 0.12, 1.80),
    ("KM2506", "Siêu sale 10/10", "Flash sale", "2025-10-09", "2025-10-11", 0.12, 1.70),
    ("KM2507", "Siêu sale 11/11", "Flash sale", "2025-11-10", "2025-11-12", 0.15, 2.10),
    ("KM2508", "Black Friday 2025", "Flash sale", "2025-11-28", "2025-11-30", 0.15, 1.90),
    ("KM2509", "Giáng sinh & 12/12", "Mùa vụ", "2025-12-12", "2025-12-25", 0.08, 1.25),
    ("KM2601", "Tết Bính Ngọ 2026", "Lễ Tết", "2026-01-26", "2026-02-14", 0.10, 1.60),
    ("KM2602", "Quốc tế Phụ nữ 8/3/2026", "Ngày lễ", "2026-03-01", "2026-03-08", 0.08, 1.25),
    ("KM2603", "Đại lễ 30/4 - 1/5/2026", "Ngày lễ", "2026-04-25", "2026-05-03", 0.10, 1.35),
    ("KM2604", "Back to School 2026", "Mùa vụ", "2026-08-01", "2026-08-31", 0.07, 1.20),
    ("KM2605", "Siêu sale 9/9/2026", "Flash sale", "2026-09-08", "2026-09-10", 0.12, 1.85),
    ("KM2606", "Siêu sale 10/10/2026", "Flash sale", "2026-10-09", "2026-10-11", 0.12, 1.75),
    ("KM2607", "Siêu sale 11/11/2026", "Flash sale", "2026-11-10", "2026-11-12", 0.15, 2.10),
]
TET_HOLIDAYS = [("2025-01-28", "2025-02-02"), ("2026-02-15", "2026-02-21")]
HOLIDAYS = {"2025-01-01": "Tết Dương lịch", "2026-01-01": "Tết Dương lịch", "2025-04-30": "Giải phóng miền Nam",
            "2025-05-01": "Quốc tế Lao động", "2026-04-30": "Giải phóng miền Nam", "2026-05-01": "Quốc tế Lao động",
            "2025-09-02": "Quốc khánh", "2026-09-02": "Quốc khánh", "2025-04-07": "Giỗ Tổ Hùng Vương",
            "2026-04-26": "Giỗ Tổ Hùng Vương"}

HO = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Huỳnh", "Phan", "Vũ", "Võ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương", "Lý"]
DEM_NAM = ["Văn", "Hữu", "Minh", "Quốc", "Đức", "Hoàng", "Gia", "Thanh", "Công", "Anh"]
DEM_NU = ["Thị", "Ngọc", "Thu", "Thanh", "Minh", "Bảo", "Khánh", "Phương", "Hồng", "Mai"]
TEN_NAM = ["An", "Bình", "Dũng", "Hải", "Hiếu", "Hùng", "Huy", "Khánh", "Long", "Minh", "Nam", "Phong", "Phúc",
           "Quân", "Quang", "Sơn", "Trung", "Tú", "Tuấn", "Việt", "Đạt", "Khoa", "Thắng", "Lộc"]
TEN_NU = ["Anh", "Châu", "Giang", "Hà", "Hạnh", "Hoa", "Lan", "Linh", "Mai", "Ngân", "Nhung", "Tâm", "Thảo",
          "Trang", "Vy", "Yến", "Hương", "Ly", "Quỳnh", "Thư", "Uyên", "Nhi"]
RETURN_REASONS = ["Lỗi kỹ thuật", "Không đúng mô tả", "Khách đổi ý", "Giao sai hàng", "Hư hỏng khi vận chuyển"]
PAY_POS = ["Tiền mặt", "Thẻ ngân hàng", "Chuyển khoản QR", "Ví điện tử", "Trả góp"]
PAY_ECOM = ["cod", "card", "bank_transfer", "ewallet", "installment"]


def _names(rng, n):
    male = rng.random(n) < 0.5
    ho = rng.choice(HO, n, p=_zipf_p(len(HO), 0.9))
    dem = np.where(male, rng.choice(DEM_NAM, n), rng.choice(DEM_NU, n))
    ten = np.where(male, rng.choice(TEN_NAM, n), rng.choice(TEN_NU, n))
    return np.char.add(np.char.add(np.char.add(np.char.add(ho.astype(str), " "), dem.astype(str)), " "),
                       ten.astype(str)), male


def _zipf_p(n, a=1.0):
    w = 1.0 / np.arange(1, n + 1) ** a
    return w / w.sum()


def build_ref(rng):
    stores = []
    i = 1
    for prov, vung, kho, areas in STORE_LAYOUT:
        for a in areas:
            area, loai = a[0], a[1]
            mo = a[2] if len(a) > 2 and a[2] else f"20{rng.integers(17, 24)}-{rng.integers(1, 13):02d}-01"
            dong = a[3] if len(a) > 3 else None
            dt_m2 = {"Flagship": 900, "Tiêu chuẩn": 350, "Mini": 150}[loai] + int(rng.integers(-40, 60))
            stores.append(dict(ma_ch=f"CH{i:03d}", ten_ch=f"ABC {area}", tinh_tp=prov, ma_vung=vung, loai_ch=loai,
                               dien_tich_m2=dt_m2, ngay_khai_truong=mo, ngay_dong_cua=dong, ma_kho=kho))
            i += 1
    stores = pd.DataFrame(stores)

    products = []
    j = 1
    for ma_nh, specs in PRODUCT_SPEC.items():
        for brand, nhom, models, lo, hi in specs:
            for m in models:
                # 1-3 biến thể dung lượng/màu cho nhóm có giá cao
                n_var = 1 if hi < 2e6 else int(rng.integers(1, 4))
                base = rng.uniform(lo, hi)
                for v in range(n_var):
                    suffix = ["", " 256GB", " 512GB"][v] if ma_nh in ("DT", "MTB") else ["", " (bản cao cấp)", " (màu mới)"][v]
                    price = round(base * (1 + 0.18 * v) / 10000) * 10000 - 10000
                    price = max(price, 79000)
                    launch = "2024-06-01"
                    if any(k in m for k in ("17", "S26", "M5", "Fold7", "Flip7", "Series 11", "Ultra 3", "SE 3", "Watch8",
                                            "AirPods Pro 3", "Band 10", "X300", "Find X9", "15T", "Tab S11")):
                        launch = "2025-09-20" if "S26" not in m else "2026-02-25"
                    products.append(dict(ma_hang=f"SP{j:04d}", ten_hang=m + suffix, nganh_hang=ma_nh, nhom_hang=nhom,
                                         thuong_hieu=brand, gia_ban_le=float(price),
                                         gia_von=float(round(price * rng.uniform(0.72, 0.9), -3)),
                                         ngay_ra_mat=launch, ma_ncc=BRAND_SUPPLIER.get(brand, "NCC09")))
                    j += 1
    products = pd.DataFrame(products)
    products["trang_thai_kd"] = np.where(rng.random(len(products)) < 0.06, "Ngừng KD", "Đang KD")
    products["popularity"] = np.clip(rng.pareto(1.6, len(products)), 0, 4.0) + 0.3
    return stores, products


def day_factors(days: pd.DatetimeIndex, rng):
    n = len(days)
    f = np.ones(n)
    dow = days.day_of_week.values
    f *= np.where(dow >= 5, 1.35, np.where(dow == 4, 1.1, 1.0))
    # xu hướng tăng trưởng ~8%/năm
    t = (days - pd.Timestamp(START)).days.values / 365.0
    f *= 1 + 0.08 * t
    # mùa vụ theo tháng (tổng thể)
    month_f = {1: 1.25, 2: 0.95, 3: 0.92, 4: 1.0, 5: 1.05, 6: 1.02, 7: 0.95, 8: 1.05, 9: 1.08, 10: 1.0, 11: 1.15, 12: 1.2}
    f *= np.array([month_f[m] for m in days.month])
    promo_idx = np.full(n, -1)
    for k, (_, _, _, a, b, _, mult) in enumerate(PROMOS):
        mask = (days >= pd.Timestamp(a)) & (days <= pd.Timestamp(b))
        f[mask] *= mult
        promo_idx[mask] = k
    for a, b in TET_HOLIDAYS:
        mask = (days >= pd.Timestamp(a)) & (days <= pd.Timestamp(b))
        f[mask] *= 0.3
    f *= rng.normal(1.0, 0.06, n).clip(0.75, 1.3)
    return f, promo_idx


def category_probs(months: np.ndarray):
    """Ma trận xác suất ngành hàng theo tháng (1..12) -> shape (len(months), n_cat)."""
    base = np.array([c[2] for c in CATEGORIES])
    P = np.tile(base, (len(months), 1))
    cat_idx = {c[0]: i for i, c in enumerate(CATEGORIES)}
    for r, m in enumerate(months):
        if m in (4, 5, 6):
            P[r, cat_idx["DL"]] *= 2.3
        if m in (11, 12, 1):
            P[r, cat_idx["DL"]] *= 0.6
            P[r, cat_idx["TV"]] *= 1.4
        if m in (8, 9):
            P[r, cat_idx["LT"]] *= 1.7
            P[r, cat_idx["MTB"]] *= 1.3
        if m in (9, 10):
            P[r, cat_idx["DT"]] *= 1.3
        if m in (1, 2):
            P[r, cat_idx["GD"]] *= 1.3
    return P / P.sum(1, keepdims=True)


def generate(db_path: pathlib.Path, scale: float = 1.0, seed: int = 42, verbose: bool = True):
    t0 = time.time()
    rng = np.random.default_rng(seed)
    log = print if verbose else (lambda *a, **k: None)
    stores, products = build_ref(rng)
    days = pd.date_range(START, END, freq="D")
    D = len(days)
    fday, promo_idx = day_factors(days, rng)

    # ------------------------------------------------------------------ khách hàng
    n_cust = int(60000 * scale) + 2000
    cust_names, cust_male = _names(rng, n_cust)
    provs = list(PROVINCE_ALIASES)
    prov_region = {p: v for p, v, _, _ in STORE_LAYOUT}
    prov_w = np.array([sum(len(a) for p2, _, _, a in STORE_LAYOUT if p2 == p) for p in provs], float)
    cust_prov = rng.choice(provs, n_cust, p=prov_w / prov_w.sum())
    cust_region = np.array([prov_region[p] for p in cust_prov])
    reg_dates = pd.Timestamp("2019-01-01") + pd.to_timedelta(rng.integers(0, (END - dt.date(2019, 1, 1)).days, n_cust), "D")
    birth_year = rng.integers(1960, 2008, n_cust)
    phones = np.char.add("09", rng.integers(10_000_000, 99_999_999, n_cust).astype(str))
    tiers = rng.choice(["Thường", "Bạc", "Vàng", "Kim cương"], n_cust, p=[0.62, 0.24, 0.11, 0.03])
    cust_ids = np.array([f"KH{i:06d}" for i in range(1, n_cust + 1)])

    # ------------------------------------------------------------------ đơn POS
    open_mask = np.ones((len(stores), D), bool)
    for s, row in stores.iterrows():
        open_mask[s] &= days >= pd.Timestamp(row.ngay_khai_truong)
        if pd.notna(row.ngay_dong_cua):
            open_mask[s] &= days <= pd.Timestamp(row.ngay_dong_cua)
    base_rate = stores.loai_ch.map({"Flagship": 24.0, "Tiêu chuẩn": 12.0, "Mini": 6.0}).values * scale
    base_rate = base_rate * rng.uniform(0.8, 1.2, len(stores))
    lam = base_rate[:, None] * fday[None, :] * open_mask
    # cửa hàng mới mở: ramp-up 3 tháng
    for s, row in stores.iterrows():
        opened = pd.Timestamp(row.ngay_khai_truong)
        if opened > pd.Timestamp(START):
            age = (days - opened).days.values
            lam[s] *= np.clip(0.4 + age / 150, 0, 1)
    counts = rng.poisson(lam)
    s_idx, d_idx = np.nonzero(counts)
    reps = counts[s_idx, d_idx]
    o_store = np.repeat(s_idx, reps)
    o_day = np.repeat(d_idx, reps)
    n_pos = len(o_store)
    log(f"  POS orders: {n_pos:,}")

    # ------------------------------------------------------------------ đơn online
    region_codes = np.array(["B", "T", "N"])
    online_base = np.array([38.0, 14.0, 44.0]) * scale
    t_year = (days - pd.Timestamp(START)).days.values / 365.0
    online_trend = 1 + 0.35 * t_year
    lam_on = online_base[:, None] * fday[None, :] * online_trend[None, :]
    c_on = rng.poisson(lam_on)
    r_idx, d2 = np.nonzero(c_on)
    reps2 = c_on[r_idx, d2]
    e_region = np.repeat(r_idx, reps2)
    e_day = np.repeat(d2, reps2)
    n_ecom = len(e_day)
    log(f"  E-commerce orders: {n_ecom:,}")
    # chọn cửa hàng xử lý đơn trong vùng (cửa hàng đang mở, ưu tiên lớn)
    e_store = np.empty(n_ecom, int)
    store_w = stores.loai_ch.map({"Flagship": 4.0, "Tiêu chuẩn": 1.5, "Mini": 0.3}).values
    for r in range(3):
        sel = np.nonzero(e_region == r)[0]
        cand = np.nonzero(stores.ma_vung.values == region_codes[r])[0]
        dsel = e_day[sel]
        # xác suất phụ thuộc trạng thái mở cửa của ngày
        om = open_mask[cand][:, dsel].T * store_w[cand][None, :]
        om = om / om.sum(1, keepdims=True)
        u = rng.random(len(sel))[:, None]
        e_store[sel] = cand[(u > om.cumsum(1)).sum(1).clip(0, len(cand) - 1)]
    channel_on = rng.choice(["web", "app", "shopee", "lazada", "tiki"], n_ecom, p=[0.28, 0.34, 0.2, 0.12, 0.06])

    # ------------------------------------------------------------------ hợp nhất đơn để sinh dòng
    all_store = np.concatenate([o_store, e_store])
    all_day = np.concatenate([o_day, e_day])
    is_pos = np.concatenate([np.ones(n_pos, bool), np.zeros(n_ecom, bool)])
    N = len(all_store)
    region_of_store = stores.ma_vung.values
    all_region = region_of_store[all_store]
    # khách hàng
    cust_by_region = {r: np.nonzero(cust_region == r)[0] for r in "BTN"}
    cust_idx = np.full(N, -1)
    for r in "BTN":
        sel = np.nonzero(all_region == r)[0]
        same = rng.random(len(sel)) < 0.9
        pool = cust_by_region[r]
        pick = np.where(same, pool[rng.integers(0, len(pool), len(sel))], rng.integers(0, n_cust, len(sel)))
        cust_idx[sel] = pick
    walk_in = is_pos & (rng.random(N) < 0.35)
    cust_idx[walk_in] = -1
    # thời gian đặt
    hour = np.clip(rng.normal(15.5, 3.2, N), 8, 21.99)
    ts = (pd.Timestamp(START) + pd.to_timedelta(all_day, "D") + pd.to_timedelta((hour * 3600).astype(int), "s"))
    promo_k = promo_idx[all_day]
    has_promo = (promo_k >= 0) & (rng.random(N) < 0.6)
    month_abs = (days.year.values[all_day] - 2025) * 12 + days.month.values[all_day] - 1  # 0..22

    # dòng đơn hàng
    n_lines = 1 + np.clip(rng.poisson(0.65, N), 0, 4)
    L = int(n_lines.sum())
    l_order = np.repeat(np.arange(N), n_lines)
    l_no = np.concatenate([np.arange(1, k + 1) for k in n_lines]) if N < 50 else (
        np.arange(L) - np.repeat(np.cumsum(n_lines) - n_lines, n_lines) + 1)
    months_cal = (np.arange(23) % 12) + 1
    P = category_probs(months_cal)
    cum = P.cumsum(1)
    l_month = month_abs[l_order]
    u = rng.random(L)
    l_cat = (u[:, None] > cum[l_month]).sum(1).clip(0, len(CATEGORIES) - 1)
    # stockout: Điện thoại, Miền Trung, 06/2026 -> mất 78% dòng
    cat_codes = np.array([c[0] for c in CATEGORIES])
    stock_m = (STOCKOUT_MONTH[0] - 2025) * 12 + STOCKOUT_MONTH[1] - 1
    so_mask = (cat_codes[l_cat] == "DT") & (all_region[l_order] == "T") & (l_month == stock_m)
    so_mask |= (cat_codes[l_cat] == "DT") & (all_region[l_order] == "T") & (l_month == stock_m - 1) & \
               (days.day.values[all_day[l_order]] >= 25)
    keep = ~(so_mask & (rng.random(L) < 0.78))
    l_order, l_no, l_month, l_cat = l_order[keep], l_no[keep], l_month[keep], l_cat[keep]
    # đánh lại số dòng
    L = len(l_order)
    first = np.r_[True, l_order[1:] != l_order[:-1]]
    grp_start = np.maximum.accumulate(np.where(first, np.arange(L), 0))
    l_no = np.arange(L) - grp_start + 1
    log(f"  Order lines: {L:,}")

    # chọn sản phẩm theo (tháng, ngành)
    prod_cat = products.nganh_hang.map({c[0]: i for i, c in enumerate(CATEGORIES)}).values
    launch = pd.to_datetime(products.ngay_ra_mat).values
    pop = products.popularity.values
    month_start = np.array([np.datetime64(f"{2025 + m // 12}-{m % 12 + 1:02d}-01") for m in range(23)])
    l_prod = np.empty(L, int)
    for m in range(23):
        for c in range(len(CATEGORIES)):
            sel = np.nonzero((l_month == m) & (l_cat == c))[0]
            if len(sel) == 0:
                continue
            cand = np.nonzero(prod_cat == c)[0]
            w = pop[cand] * (launch[cand] <= month_start[m] + np.timedelta64(20, "D"))
            # sản phẩm mới ra mắt: hút cầu mạnh
            fresh = (launch[cand] > np.datetime64("2025-01-01")) & (launch[cand] <= month_start[m] + np.timedelta64(20, "D"))
            w = w * np.where(fresh, 1.8, 1.0)
            w = w / w.sum()
            l_prod[sel] = cand[rng.choice(len(cand), len(sel), p=w)]
    list_price = products.gia_ban_le.values[l_prod]
    l_cat_code = cat_codes[l_cat]
    qty = np.ones(L, int)
    qty += np.where(l_cat_code == "PK", rng.poisson(0.45, L), 0)
    qty += np.where((l_cat_code != "PK") & (rng.random(L) < 0.03), 1, 0)
    unit_price = np.round(list_price * rng.uniform(0.96, 1.0, L), -3)
    o_has_promo = has_promo[l_order]
    pct = np.array([p[5] for p in PROMOS])[np.clip(promo_k[l_order], 0, None)]
    disc = np.where(o_has_promo, np.round(unit_price * qty * pct, -3), 0.0)
    manual = (~o_has_promo) & (rng.random(L) < 0.04)
    disc = np.where(manual, np.round(unit_price * qty * 0.03, -3), disc)

    # ------------------------------------------------------------------ trạng thái
    end_ts = pd.Timestamp(END)
    recent = (end_ts - pd.Timestamp(START)).days - all_day <= 4
    u = rng.random(N)
    status_std = np.where(is_pos,
                          np.where(u < 0.02, "HUY", np.where(u < 0.035, "TRA_HANG", "HOAN_THANH")),
                          np.where(u < 0.095, "HUY", np.where(u < 0.115, "TRA_HANG", "HOAN_THANH")))
    shipping = (~is_pos) & recent & (rng.random(N) < 0.6) & (status_std == "HOAN_THANH")
    status_std = np.where(shipping, "DANG_GIAO", status_std)

    # ------------------------------------------------------------------ partial load ngày cuối
    last_day = (end_ts - pd.Timestamp(START)).days
    keep_o = ~((all_day == last_day) & (rng.random(N) < 0.62))

    # ------------------------------------------------------------------ nhân viên
    staff = []
    k = 1
    for _, st in stores.iterrows():
        n_staff = {"Flagship": 14, "Tiêu chuẩn": 7, "Mini": 4}[st.loai_ch]
        names, male = _names(rng, n_staff)
        for i2 in range(n_staff):
            role = "Cửa hàng trưởng" if i2 == 0 else ("Thu ngân" if i2 in (1, 2) else
                                                     ("Kỹ thuật viên" if i2 == n_staff - 1 else "Tư vấn bán hàng"))
            luong = {"Cửa hàng trưởng": 22e6, "Thu ngân": 8.5e6, "Kỹ thuật viên": 10e6, "Tư vấn bán hàng": 9e6}[role]
            staff.append(dict(ma_nv=f"NV{k:04d}", ho_ten=names[i2], ma_ch=st.ma_ch, chuc_danh=role,
                              ngay_vao_lam=str(pd.Timestamp(st.ngay_khai_truong) + pd.Timedelta(days=int(rng.integers(0, 600)))),
                              luong_co_ban=float(round(luong * rng.uniform(0.9, 1.25), -5)),
                              gioi_tinh="Nam" if male[i2] else "Nữ"))
            k += 1
    staff = pd.DataFrame(staff)
    staff_by_store = staff.groupby("ma_ch").ma_nv.apply(lambda s: s.values[1:] if len(s) > 1 else s.values).to_dict()
    o_staff = np.array([None] * N, dtype=object)
    store_codes = stores.ma_ch.values
    for s in range(len(stores)):
        sel = np.nonzero(is_pos & (all_store == s))[0]
        pool = staff_by_store[store_codes[s]]
        o_staff[sel] = pool[rng.integers(0, len(pool), len(sel))]

    # ------------------------------------------------------------------ mã đơn
    pos_ids = np.array([f"HD{d:%y%m}{i:07d}" for i, d in zip(range(1, n_pos + 1), ts[:n_pos])])
    ecom_ids = np.array([f"ECO-{1000000 + i}" for i in range(1, n_ecom + 1)])
    order_ids = np.concatenate([pos_ids, ecom_ids])
    promo_codes = np.array([p[0] for p in PROMOS])
    o_promo = np.where(has_promo, promo_codes[np.clip(promo_k, 0, None)], None)
    cust_code = np.where(cust_idx >= 0, cust_ids[np.clip(cust_idx, 0, None)], None)
    # đơn online tham chiếu KH chưa có trong CRM (late-arriving)
    orphan = (~is_pos) & (rng.random(N) < 0.003)
    cust_code = np.where(orphan, np.char.add("KH9", rng.integers(10000, 99999, N).astype(str)).astype(object), cust_code)

    # ======================================================== RAW: POS
    pos_mask_o = is_pos & keep_o
    pos_status_map = {"HOAN_THANH": "HT", "HUY": "HUY", "TRA_HANG": "TH", "DANG_GIAO": "DG"}
    pos_status = np.array([pos_status_map[s] for s in status_std])
    dirty = is_pos & (rng.random(N) < 0.004)
    pos_status = np.where(dirty & (status_std == "HOAN_THANH"), "Hoàn thành", pos_status)
    dirty2 = is_pos & (rng.random(N) < 0.003)
    pos_status = np.where(dirty2 & (status_std == "HOAN_THANH"), "ht", pos_status)
    line_amount = unit_price * qty - disc
    order_total = np.bincount(l_order, weights=line_amount, minlength=N)
    pay_pos = rng.choice(PAY_POS, N, p=[0.32, 0.28, 0.25, 0.08, 0.07])
    raw_pos = pd.DataFrame({
        "so_hd": order_ids[pos_mask_o], "ngay_gio": ts[pos_mask_o], "ma_ch": store_codes[all_store[pos_mask_o]],
        "ma_kh": cust_code[pos_mask_o], "ma_nv": o_staff[pos_mask_o], "ma_ctkm": o_promo[pos_mask_o],
        "tt_don": pos_status[pos_mask_o], "hinh_thuc_tt": pay_pos[pos_mask_o],
        "tong_tien_hd": order_total[pos_mask_o].round(0),
    })
    lmask_pos = pos_mask_o[l_order]
    drift_store_codes = set(stores.loc[stores.tinh_tp == DRIFT_PROVINCE, "ma_ch"])
    l_store_code = store_codes[all_store[l_order]]
    l_date = (pd.Timestamp(START) + pd.to_timedelta(all_day[l_order], "D")).values
    don_gia_raw = unit_price.copy()
    tien_ck_raw = disc.copy()
    drift = np.isin(l_store_code, list(drift_store_codes)) & (l_date >= np.datetime64(DRIFT_START)) & is_pos[l_order]
    don_gia_raw[drift] = np.round(don_gia_raw[drift] / 1000.0, 1)
    tien_ck_raw[drift] = np.round(tien_ck_raw[drift] / 1000.0, 1)
    typo = lmask_pos & (rng.random(L) < 0.0002)
    don_gia_raw[typo] = don_gia_raw[typo] * 10
    tien_ck_obj = tien_ck_raw.astype(object)
    tien_ck_obj[(tien_ck_raw == 0) & (rng.random(L) < 0.5)] = None   # NULL thay vì 0
    raw_pos_ct = pd.DataFrame({
        "so_hd": order_ids[l_order[lmask_pos]], "stt": l_no[lmask_pos], "ma_hang": products.ma_hang.values[l_prod[lmask_pos]],
        "so_luong": qty[lmask_pos], "don_gia": don_gia_raw[lmask_pos], "tien_ck": tien_ck_obj[lmask_pos],
    })
    # số lượng âm (ghi nhầm hàng trả vào hóa đơn bán)
    neg = raw_pos_ct.sample(frac=0.0015, random_state=seed).copy()
    neg["so_luong"] = -1
    neg["stt"] = neg["stt"] + 10
    neg["tien_ck"] = None
    raw_pos_ct = pd.concat([raw_pos_ct, neg], ignore_index=True)
    raw_pos_ct["tien_ck"] = pd.to_numeric(raw_pos_ct["tien_ck"], errors="coerce")
    log(f"  raw.pos_don_hang={len(raw_pos):,} raw.pos_ct_don_hang={len(raw_pos_ct):,}")

    # ======================================================== RAW: E-commerce
    e_mask_o = (~is_pos) & keep_o
    ecom_status_map = {"HOAN_THANH": "completed", "HUY": "cancelled", "TRA_HANG": "returned", "DANG_GIAO": "shipping"}
    e_status = np.array([ecom_status_map[s] for s in status_std], dtype=object)
    rr = rng.random(N)
    e_status = np.where((status_std == "HOAN_THANH") & (rr < 0.05), "Completed", e_status)
    e_status = np.where((status_std == "HOAN_THANH") & (rr > 0.97), "COMPLETED ", e_status)
    e_status = np.where((status_std == "HUY") & (rr < 0.3), "canceled", e_status)
    e_status = np.where((status_std == "HOAN_THANH") & (rr > 0.9985), "pending_review", e_status)
    ts_e = pd.Series(ts[e_mask_o])
    order_date_str = ts_e.dt.strftime("%d/%m/%Y %H:%M:%S").values
    ship_fee = np.where(rng.random(N) < 0.55, 0.0, rng.choice([15000.0, 22000.0, 30000.0, 45000.0], N))
    voucher = np.where((ts >= pd.Timestamp("2026-05-01")) & (rng.random(N) < 0.15),
                       np.char.add("VC", rng.integers(100000, 999999, N).astype(str)).astype(object), None)
    raw_ecom = pd.DataFrame({
        "order_id": order_ids[e_mask_o], "order_date": order_date_str, "customer_id": cust_code[e_mask_o],
        "channel": channel_on[e_mask_o[n_pos:]],
        "fulfill_store": store_codes[all_store[e_mask_o]], "status": e_status[e_mask_o],
        "payment_method": rng.choice(PAY_ECOM, int(e_mask_o.sum()), p=[0.3, 0.2, 0.2, 0.22, 0.08]),
        "shipping_fee": ship_fee[e_mask_o], "promo_code": o_promo[e_mask_o], "voucher_code": voucher[e_mask_o],
    })
    lmask_e = e_mask_o[l_order]
    disc_e = disc.astype(object)
    disc_e[(disc == 0) & (rng.random(L) < 0.3)] = None
    raw_ecom_items = pd.DataFrame({
        "order_id": order_ids[l_order[lmask_e]], "line_no": l_no[lmask_e],
        "sku": products.ma_hang.values[l_prod[lmask_e]], "qty": qty[lmask_e],
        "unit_price": unit_price[lmask_e], "discount_amt": disc_e[lmask_e],
    })
    dups = raw_ecom_items.sample(frac=0.004, random_state=seed + 1)
    raw_ecom_items = pd.concat([raw_ecom_items, dups], ignore_index=True)
    raw_ecom_items["discount_amt"] = pd.to_numeric(raw_ecom_items["discount_amt"], errors="coerce")
    log(f"  raw.ecom_orders={len(raw_ecom):,} raw.ecom_order_items={len(raw_ecom_items):,}")

    # ======================================================== RAW: CRM
    gender_raw = np.where(cust_male, rng.choice(["M", "Nam", "nam", "Male"], n_cust, p=[0.5, 0.35, 0.1, 0.05]),
                          rng.choice(["F", "Nữ", "nữ", "Female"], n_cust, p=[0.5, 0.35, 0.1, 0.05])).astype(object)
    gender_raw[rng.random(n_cust) < 0.05] = None
    prov_raw = np.array([rng.choice(PROVINCE_ALIASES[p]) for p in cust_prov], dtype=object)
    prov_raw[rng.random(n_cust) < 0.01] = None
    emails = np.char.add(np.char.add("kh", np.arange(1, n_cust + 1).astype(str)),
                         rng.choice(["@gmail.com", "@yahoo.com", "@outlook.com", "@abc.vn"], n_cust)).astype(object)
    emails[rng.random(n_cust) < 0.2] = None
    raw_crm = pd.DataFrame({
        "ma_kh": cust_ids, "ho_ten": cust_names, "gioi_tinh": gender_raw,
        "ngay_sinh": [f"{y}-{m:02d}-{d:02d}" for y, m, d in zip(birth_year, rng.integers(1, 13, n_cust),
                                                                rng.integers(1, 29, n_cust))],
        "sdt": phones, "email": emails, "dia_chi_tinh": prov_raw, "hang_the": tiers,
        "ngay_dang_ky": reg_dates.date,
    })
    dup = raw_crm.sample(frac=0.02, random_state=seed + 2).copy()
    dup["ma_kh"] = [f"KH{n_cust + i:06d}" for i in range(1, len(dup) + 1)]
    dup["hang_the"] = "Thường"
    raw_crm = pd.concat([raw_crm, dup], ignore_index=True)
    log(f"  raw.crm_khach_hang={len(raw_crm):,}")

    # ======================================================== RAW: ERP trả hàng
    l_status = status_std[l_order]
    l_keep = keep_o[l_order]
    ret_partial = (l_status == "HOAN_THANH") & l_keep & (rng.random(L) < 0.025)
    ret_full = (l_status == "TRA_HANG") & l_keep
    ret = ret_partial | ret_full
    ri = np.nonzero(ret)[0]
    ret_qty = np.where(ret_full[ri], qty[ri], np.maximum(1, (qty[ri] * rng.uniform(0.3, 1.0, len(ri))).astype(int)))
    ret_day = all_day[l_order[ri]] + rng.integers(1, 15, len(ri))
    ok = ret_day <= last_day
    ri, ret_qty, ret_day = ri[ok], ret_qty[ok], ret_day[ok]
    refund = np.round((unit_price[ri] * qty[ri] - disc[ri]) * ret_qty / qty[ri], -2)
    raw_ret = pd.DataFrame({
        "so_phieu": [f"PTH{i:07d}" for i in range(1, len(ri) + 1)], "ma_don": order_ids[l_order[ri]],
        "ma_hang": products.ma_hang.values[l_prod[ri]], "sl": ret_qty,
        "ly_do": rng.choice(RETURN_REASONS, len(ri), p=[0.35, 0.2, 0.25, 0.1, 0.1]),
        "ngay_tra": (pd.Timestamp(START) + pd.to_timedelta(ret_day, "D")).date,
        "tien_hoan": refund,
    })
    log(f"  raw.erp_tra_hang={len(raw_ret):,}")

    # ======================================================== RAW: ERP tồn kho + nhập kho (mô phỏng tuần)
    kho_of_store = dict(zip(stores.ma_ch, stores.ma_kho))
    kho_codes = [w[0] for w in WAREHOUSES]
    sold_mask = np.isin(l_status, ["HOAN_THANH", "TRA_HANG", "DANG_GIAO"]) & l_keep
    week_idx = (all_day[l_order] + START.weekday()) // 7      # tuần bắt đầu Thứ 2
    n_weeks = int(week_idx.max()) + 1
    l_kho = np.array([kho_codes.index(kho_of_store[c]) for c in store_codes])[all_store[l_order]]
    n_prod = len(products)
    demand = np.zeros((len(kho_codes), n_prod, n_weeks))
    np.add.at(demand, (l_kho[sold_mask], l_prod[sold_mask], week_idx[sold_mask]), qty[sold_mask])
    week_start0 = pd.Timestamp(START) - pd.Timedelta(days=START.weekday())
    snap_rows, inb_rows = [], []
    pn = 1
    so_w0 = (pd.Timestamp("2026-05-18") - week_start0).days // 7
    so_w1 = (pd.Timestamp("2026-07-05") - week_start0).days // 7
    dt_idx = set(np.nonzero(products.nganh_hang.values == "DT")[0])
    gia_von = products.gia_von.values
    supplier = products.ma_ncc.values
    for k_i, kho in enumerate(kho_codes):
        for p_i in range(n_prod):
            dmd = demand[k_i, p_i]
            avg = max(dmd.mean(), 0.2)
            stock = avg * 4 + rng.integers(0, 5)
            pipeline = []  # (tuần về, sl)
            for w in range(n_weeks):
                arrived = sum(q for ww, q in pipeline if ww == w)
                pipeline = [(ww, q) for ww, q in pipeline if ww != w]
                stockout = (kho == "KHO_DN" and p_i in dt_idx and so_w0 <= w <= so_w1)
                if arrived and not stockout:
                    stock += arrived
                    d_nhap = week_start0 + pd.Timedelta(days=7 * w + int(rng.integers(0, 3)))
                    if d_nhap.date() <= END:
                        inb_rows.append((f"PN{pn:07d}", d_nhap.date(), kho, supplier[p_i], products.ma_hang.values[p_i],
                                         int(arrived), float(round(gia_von[p_i] * rng.uniform(0.97, 1.03), -3))))
                        pn += 1
                elif arrived and stockout:
                    pipeline.append((so_w1 + 1, arrived))
                stock = max(stock - dmd[w], 0)
                on_order = sum(q for _, q in pipeline)
                if stock + on_order < avg * 2.5 and not stockout:
                    q = int(np.ceil(avg * 4)) + int(rng.integers(0, 4))
                    pipeline.append((w + int(rng.integers(1, 3)), q))
                if stockout:
                    stock = min(stock, rng.integers(0, 2))
                snap_day = week_start0 + pd.Timedelta(days=7 * w + 6)   # Chủ nhật
                if snap_day.date() <= END and (dmd.sum() > 0 or w % 4 == 0):
                    snap_rows.append((snap_day.date(), kho, products.ma_hang.values[p_i], int(round(stock)),
                                      int(sum(q for _, q in pipeline))))
    raw_inv = pd.DataFrame(snap_rows, columns=["ngay_chot", "ma_kho", "ma_hang", "ton_cuoi", "dang_ve"])
    raw_inb = pd.DataFrame(inb_rows, columns=["so_pn", "ngay_nhap", "ma_kho", "ma_ncc", "ma_hang", "sl", "dg_nhap"])
    log(f"  raw.erp_ton_kho={len(raw_inv):,} raw.erp_nhap_kho={len(raw_inb):,}")

    # ======================================================== RAW: tài chính kế hoạch & marketing
    net_true = np.where((l_status == "HOAN_THANH") & l_keep, unit_price * qty - disc, 0.0)
    l_m = l_month
    rev = np.zeros((len(stores), 23))
    np.add.at(rev, (all_store[l_order], l_m), net_true)
    plan_rows = []
    for s in range(len(stores)):
        for m in range(23):
            y, mo = 2025 + m // 12, m % 12 + 1
            if m >= 12:
                base_v = rev[s, m - 12] * 1.12 if rev[s, m - 12] > 0 else rev[s, m] * 1.05
            else:
                base_v = rev[s, m] * rng.uniform(0.95, 1.12)
            if base_v <= 0:
                continue
            plan_rows.append((y, mo, store_codes[s], float(round(base_v * rng.uniform(0.97, 1.05), -6))))
    # tháng 11, 12/2026 (chưa có dữ liệu thực tế) vẫn có kế hoạch
    for s in range(len(stores)):
        if pd.notna(stores.ngay_dong_cua.values[s]):
            continue
        for m in (22, 23):
            y, mo = 2026, m - 11
            plan_rows = [r for r in plan_rows if not (r[0] == y and r[1] == mo and r[2] == store_codes[s])]
            plan_rows.append((y, mo, store_codes[s], float(round(rev[s, m - 12] * 1.12, -6))))
    raw_plan = pd.DataFrame(plan_rows, columns=["nam", "thang", "ma_ch", "dt_ke_hoach"])
    mk_rows = []
    for m in range(23):
        y, mo = 2025 + m // 12, m % 12 + 1
        boost = 1.6 if mo in (1, 11) else (1.3 if mo in (8, 9, 10, 12) else 1.0)
        for ch, base_mk in (("CH", 1.2e9), ("WEB", 0.9e9), ("APP", 1.1e9), ("SAN", 1.4e9)):
            mk_rows.append((f"{y}-{mo:02d}-01", ch, float(round(base_mk * boost * scale * rng.uniform(0.85, 1.15), -6))))
    raw_mkt = pd.DataFrame(mk_rows, columns=["thang", "kenh", "chi_phi"])

    # ======================================================== REF (seed)
    ref_vung = pd.DataFrame(REGIONS, columns=["ma_vung", "ten_vung"])
    ref_kho = pd.DataFrame(WAREHOUSES, columns=["ma_kho", "ten_kho", "ma_vung", "tinh_tp"])
    ref_kenh = pd.DataFrame(CHANNELS, columns=["ma_kenh", "ten_kenh", "nhom_kenh"])
    ref_nh = pd.DataFrame([(c[0], c[1]) for c in CATEGORIES], columns=["ma_nh", "ten_nh"])
    ref_ncc = pd.DataFrame(SUPPLIERS, columns=["ma_ncc", "ten_ncc", "quoc_gia"])
    ref_km = pd.DataFrame([p[:6] for p in PROMOS], columns=["ma_km", "ten_km", "loai_km", "tu_ngay", "den_ngay", "pct_giam"])
    alias_rows = [(a.lower().strip(), p) for p, al in PROVINCE_ALIASES.items() for a in al]
    ref_alias = pd.DataFrame(sorted(set(alias_rows)), columns=["alias", "tinh_tp"])
    ref_alias["ma_vung"] = ref_alias.tinh_tp.map(prov_region)
    status_rows = [("POS", "ht", "HOAN_THANH"), ("POS", "huy", "HUY"), ("POS", "th", "TRA_HANG"),
                   ("POS", "dg", "DANG_GIAO"), ("ECOM", "completed", "HOAN_THANH"), ("ECOM", "cancelled", "HUY"),
                   ("ECOM", "canceled", "HUY"), ("ECOM", "returned", "TRA_HANG"), ("ECOM", "shipping", "DANG_GIAO")]
    ref_status = pd.DataFrame(status_rows, columns=["nguon", "ma_goc", "trang_thai_chuan"])
    ref_holiday = pd.DataFrame(list(HOLIDAYS.items()) + [(str(d.date()), "Nghỉ Tết Nguyên đán")
                                                         for a, b in TET_HOLIDAYS for d in pd.date_range(a, b)],
                               columns=["ngay", "ten_le"])
    erp_sp = products.drop(columns=["popularity"]).copy()

    # ======================================================== GHI VÀO DUCKDB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    wal = db_path.with_suffix(db_path.suffix + ".wal")
    if wal.exists():
        wal.unlink()
    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA raw; CREATE SCHEMA ref;")
    tables = {
        "raw.pos_don_hang": raw_pos, "raw.pos_ct_don_hang": raw_pos_ct, "raw.ecom_orders": raw_ecom,
        "raw.ecom_order_items": raw_ecom_items, "raw.crm_khach_hang": raw_crm, "raw.erp_san_pham": erp_sp,
        "raw.erp_ton_kho": raw_inv, "raw.erp_nhap_kho": raw_inb, "raw.erp_tra_hang": raw_ret,
        "raw.hr_nhan_vien": staff, "raw.fin_ke_hoach": raw_plan, "raw.mkt_chi_phi": raw_mkt,
        "raw.pos_cua_hang": stores,
        "ref.vung": ref_vung, "ref.kho": ref_kho, "ref.kenh": ref_kenh, "ref.nganh_hang": ref_nh,
        "ref.nha_cung_cap": ref_ncc, "ref.khuyen_mai": ref_km, "ref.tinh_alias": ref_alias,
        "ref.status_map": ref_status, "ref.ngay_le": ref_holiday,
    }
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
        con.unregister("_df")
    # kiểu dữ liệu chuẩn hơn cho một số cột
    con.execute("ALTER TABLE raw.pos_don_hang ALTER ngay_gio TYPE TIMESTAMP")
    con.execute("ALTER TABLE raw.erp_tra_hang ALTER ngay_tra TYPE DATE")
    con.execute("ALTER TABLE raw.erp_ton_kho ALTER ngay_chot TYPE DATE")
    con.execute("ALTER TABLE raw.erp_nhap_kho ALTER ngay_nhap TYPE DATE")
    con.execute("ALTER TABLE raw.crm_khach_hang ALTER ngay_dang_ky TYPE DATE")
    con.execute("ALTER TABLE raw.mkt_chi_phi ALTER thang TYPE DATE")
    con.execute("ALTER TABLE ref.khuyen_mai ALTER tu_ngay TYPE DATE")
    con.execute("ALTER TABLE ref.khuyen_mai ALTER den_ngay TYPE DATE")
    con.execute("ALTER TABLE ref.ngay_le ALTER ngay TYPE DATE")
    con.execute("CREATE TABLE ref.meta AS SELECT DATE '%s' AS ngay_bat_dau, DATE '%s' AS ngay_ket_thuc, %f AS scale, %d AS seed"
                % (START, END, scale, seed))
    con.close()
    log(f"Đã sinh dữ liệu raw/ref vào {db_path} trong {time.time() - t0:.1f}s")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--scale", type=float, default=1.0, help="hệ số quy mô dữ liệu (1.0 ~ 400k đơn, 700k dòng)")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    generate(pathlib.Path(a.db), a.scale, a.seed)


if __name__ == "__main__":
    main()
