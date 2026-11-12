"""
Sinh dữ liệu GIẢ LẬP cho doanh nghiệp mẫu thứ hai: Công ty CP Giao vận XYZ (chuyển phát hàng hóa).

Mục đích: kiểm chứng hệ thống chạy được với một doanh nghiệp khác hẳn công ty điện máy ABC
(ngành khác, schema khác, quy ước đặt tên khác, bộ chỉ số khác) chỉ bằng cách đổi bộ file cấu hình.
Toàn bộ số liệu là giả lập, sinh từ seed cố định nên chạy lại cho đúng cùng một kho.

    python workspaces/giaovan_xyz/warehouse/generate.py            # ghi workspaces/giaovan_xyz/var/warehouse.duckdb

Ba sự kiện được cài sẵn để kiểm thử câu hỏi "vì sao":
  1. 10–25/06/2026: mưa bão Miền Trung, thời gian giao tới các bưu cục Miền Trung tăng mạnh (tỷ lệ đúng hạn giảm).
  2. Từ 01/09/2026: khách hàng doanh nghiệp lớn nhất ở Miền Nam ngừng gửi hàng (số vận đơn Miền Nam giảm).
  3. 01/04/2026: "Bưu cục Vinh 1" đóng cửa, vận đơn chuyển về trung tâm chia chọn cùng tỉnh.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib

import duckdb
import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_DB = HERE.parent / "var" / "warehouse.duckdb"
START, END = dt.date(2025, 1, 1), dt.date(2026, 10, 31)      # END = ngày dữ liệu mới nhất
SEED = 20261111

PROVINCES = [  # (tỉnh/thành, miền, trọng số sản lượng, số bưu cục)
    ("Hà Nội", "Miền Bắc", 10, 4), ("Hải Phòng", "Miền Bắc", 4, 2), ("Quảng Ninh", "Miền Bắc", 2, 1),
    ("Bắc Ninh", "Miền Bắc", 3, 1), ("Thái Nguyên", "Miền Bắc", 2, 1), ("Nam Định", "Miền Bắc", 1.5, 1),
    ("Thanh Hóa", "Miền Trung", 2, 1), ("Nghệ An", "Miền Trung", 2.5, 2), ("Huế", "Miền Trung", 1.5, 1),
    ("Đà Nẵng", "Miền Trung", 4, 2), ("Quảng Nam", "Miền Trung", 1.2, 1), ("Khánh Hòa", "Miền Trung", 1.8, 1),
    ("Đắk Lắk", "Miền Trung", 1.2, 1),
    ("TP. Hồ Chí Minh", "Miền Nam", 14, 5), ("Bình Dương", "Miền Nam", 4, 2), ("Đồng Nai", "Miền Nam", 3.5, 2),
    ("Cần Thơ", "Miền Nam", 2.5, 1), ("Long An", "Miền Nam", 1.5, 1), ("An Giang", "Miền Nam", 1.2, 1),
]
CITY_SHORT = {"Hà Nội": "Hà Nội", "TP. Hồ Chí Minh": "Sài Gòn", "Nghệ An": "Vinh", "Khánh Hòa": "Nha Trang",
              "Đắk Lắk": "Buôn Ma Thuột", "Quảng Ninh": "Hạ Long", "Đồng Nai": "Biên Hòa", "Bình Dương": "Thủ Dầu Một",
              "Quảng Nam": "Tam Kỳ", "Long An": "Tân An", "An Giang": "Long Xuyên"}
SERVICES = [  # (mã, tên, cam kết giờ, tỷ trọng, cước cơ bản, cước mỗi kg)
    ("STD", "Tiêu chuẩn", 72, 0.58, 18000, 3500), ("EXP", "Nhanh", 48, 0.27, 26000, 5000),
    ("SDD", "Hỏa tốc", 24, 0.09, 45000, 8000), ("BLK", "Hàng nặng", 96, 0.06, 60000, 2200),
]
SEGMENTS = [("Cá nhân", 0.60, 1.0), ("Shop online", 0.34, 9.0), ("Doanh nghiệp", 0.06, 40.0)]   # (tên, tỷ lệ KH, hệ số sản lượng)
INDUSTRIES = ["Thời trang", "Mỹ phẩm", "Điện tử", "Thực phẩm khô", "Sách & văn phòng phẩm", "Đồ gia dụng", "Mẹ & bé", "Khác"]
COMPLAINT_TYPES = ["Giao trễ", "Hư hỏng hàng", "Thất lạc", "Thái độ nhân viên", "Sai tiền thu hộ"]
COST_TYPES = [("Nhân công", 0.52), ("Thuê mặt bằng", 0.2), ("Nhiên liệu & xe", 0.2), ("Khác", 0.08)]
HO = ["Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Hồ", "Ngô", "Dương"]
DEM = ["Văn", "Thị", "Minh", "Thu", "Đức", "Ngọc", "Hữu", "Thanh", "Quang", "Hồng"]
TEN = ["An", "Bình", "Chi", "Dũng", "Giang", "Hà", "Hải", "Hạnh", "Hùng", "Lan", "Linh", "Long", "Mai", "Nam", "Nga",
       "Phúc", "Quân", "Sơn", "Tâm", "Trang", "Tuấn", "Vy", "Yến"]


def build_dims(rng):
    hubs = []
    for prov, region, _w, n in PROVINCES:
        city = CITY_SHORT.get(prov, prov)
        for i in range(n):
            kind = "Trung tâm chia chọn" if i == 0 and n >= 2 else "Bưu cục"
            name = (f"Trung tâm chia chọn {city}" if kind.startswith("Trung") else f"Bưu cục {city} {i if n >= 2 else 1}")
            hubs.append({"hub_id": f"H{len(hubs) + 1:03d}", "hub_name": name, "hub_type": kind, "province": prov,
                         "region": region, "opened_date": START - dt.timedelta(days=int(rng.integers(200, 2500))),
                         "closed_date": None})
    hubs = pd.DataFrame(hubs)
    hubs.loc[hubs.hub_name == "Bưu cục Vinh 1", "closed_date"] = dt.date(2026, 4, 1)      # sự kiện 3

    n_cust = 4000
    seg_idx = rng.choice(len(SEGMENTS), size=n_cust, p=[s[1] for s in SEGMENTS])
    prov_w = np.array([p[2] for p in PROVINCES], dtype=float)
    prov_idx = rng.choice(len(PROVINCES), size=n_cust, p=prov_w / prov_w.sum())
    names = []
    for i in range(n_cust):
        seg = SEGMENTS[seg_idx[i]][0]
        person = f"{HO[rng.integers(len(HO))]} {DEM[rng.integers(len(DEM))]} {TEN[rng.integers(len(TEN))]}"
        names.append(person if seg == "Cá nhân" else (f"Shop {TEN[rng.integers(len(TEN))]} {rng.integers(100, 999)}"
                                                     if seg == "Shop online" else f"Công ty TNHH {TEN[rng.integers(len(TEN))]} {TEN[rng.integers(len(TEN))]} {rng.integers(10, 99)}"))
    cust = pd.DataFrame({
        "customer_id": [f"C{i + 1:05d}" for i in range(n_cust)], "customer_name": names,
        "segment": [SEGMENTS[i][0] for i in seg_idx],
        "industry": [None if SEGMENTS[s][0] == "Cá nhân" else INDUSTRIES[rng.integers(len(INDUSTRIES))] for s in seg_idx],
        "province": [PROVINCES[i][0] for i in prov_idx], "region": [PROVINCES[i][1] for i in prov_idx],
        "phone": [f"09{rng.integers(10_000_000, 99_999_999)}" for _ in range(n_cust)],
        "joined_date": [START - dt.timedelta(days=int(rng.integers(0, 1500))) for _ in range(n_cust)],
    })
    cust["_w"] = [SEGMENTS[s][2] for s in seg_idx] * rng.lognormal(0, 0.9, n_cust)
    # sự kiện 2: một khách doanh nghiệp rất lớn ở Miền Nam
    key = cust[(cust.segment == "Doanh nghiệp") & (cust.region == "Miền Nam")].index[0]
    cust.loc[key, ["customer_name", "_w"]] = ["Công ty CP Thương mại Phương Nam", cust["_w"].sum() * 0.045]
    services = pd.DataFrame([{"service_code": c, "service_name": n, "sla_hours": h} for c, n, h, *_ in SERVICES])
    return hubs, cust, services, cust.loc[key, "customer_id"]


def build_shipments(rng, hubs, cust, key_customer, scale):
    days = pd.date_range(START, END, freq="D")
    t = np.arange(len(days))
    base = 330 * scale * (1 + 0.35 * t / len(days))                                  # tăng trưởng đều
    wd = np.array([1.05, 1.08, 1.06, 1.04, 1.02, 0.85, 0.62])[days.day_of_week]        # cuối tuần ít hàng
    month = np.array([0.92, 0.72, 0.95, 0.97, 1.0, 1.0, 0.98, 1.0, 1.03, 1.08, 1.22, 1.3])[days.month - 1]
    n_day = rng.poisson(base * wd * month)
    created = np.repeat(days.values, n_day)
    n = len(created)
    created_d = pd.to_datetime(created)

    hub_w = hubs.merge(pd.DataFrame(PROVINCES, columns=["province", "region", "w", "n"])[["province", "w", "n"]], on="province")
    w = (hub_w.w / hub_w.n).to_numpy() * np.where(hubs.hub_type == "Trung tâm chia chọn", 1.6, 1.0)
    origin = rng.choice(len(hubs), size=n, p=w / w.sum())
    dest = rng.choice(len(hubs), size=n, p=w / w.sum())
    cw = cust["_w"].to_numpy()
    cidx = rng.choice(len(cust), size=n, p=cw / cw.sum())
    svc = rng.choice(len(SERVICES), size=n, p=[s[3] for s in SERVICES])

    # sự kiện 3: bưu cục đóng cửa -> chuyển vận đơn về bưu cục còn lại cùng tỉnh
    closed = hubs.index[hubs.closed_date.notna()][0]
    sibling = hubs.index[(hubs.province == hubs.province[closed]) & hubs.closed_date.isna()][0]
    after = created_d >= pd.Timestamp(hubs.closed_date[closed])
    origin = np.where(after & (origin == closed), sibling, origin)
    dest = np.where(after & (dest == closed), sibling, dest)
    # sự kiện 2: khách lớn ngừng gửi từ 01/09/2026
    key_idx = cust.index[cust.customer_id == key_customer][0]
    keep = ~((cidx == key_idx) & (created_d >= pd.Timestamp("2026-09-01")))
    # khách lớn gửi từ trung tâm chia chọn Sài Gòn
    hcm = hubs.index[hubs.hub_name == "Trung tâm chia chọn Sài Gòn"][0]
    origin = np.where(cidx == key_idx, hcm, origin)
    created, created_d, origin, dest, cidx, svc = created[keep], created_d[keep], origin[keep], dest[keep], cidx[keep], svc[keep]
    n = len(created)

    o_reg, d_reg = hubs.region.to_numpy()[origin], hubs.region.to_numpy()[dest]
    inter = o_reg != d_reg
    sla = np.array([s[2] for s in SERVICES])[svc]
    weight = np.round(np.where(svc == 3, rng.lognormal(3.0, 0.5, n), rng.lognormal(0.1, 0.8, n)).clip(0.1, 300), 2)
    fee = np.array([s[4] for s in SERVICES])[svc] + np.array([s[5] for s in SERVICES])[svc] * np.maximum(weight - 1, 0)
    fee = np.round(fee * np.where(inter, 1.45, 1.0) / 500) * 500
    seg = cust.segment.to_numpy()[cidx]
    has_cod = rng.random(n) < np.where(seg == "Shop online", 0.72, np.where(seg == "Doanh nghiệp", 0.25, 0.1))
    cod = np.where(has_cod, np.round(rng.lognormal(12.6, 0.7, n) / 1000) * 1000, 0.0)

    hours = sla * rng.lognormal(-0.42, 0.38, n) * np.where(inter, 1.12, 1.0)
    storm = (d_reg == "Miền Trung") & (created_d >= pd.Timestamp("2026-06-10")) & (created_d <= pd.Timestamp("2026-06-25"))
    hours = np.where(storm, hours * rng.uniform(1.6, 2.4, n), hours)                 # sự kiện 1
    hours = np.round(hours, 1)

    u = rng.random(n)
    p_ret = np.where(has_cod, 0.085, 0.02) + np.where(storm, 0.03, 0)
    status = np.where(u < 0.022, "CANCELLED", np.where(u < 0.022 + p_ret, "RETURNED", np.where(u < 0.025 + p_ret, "LOST", "DELIVERED")))
    in_transit = (pd.Timestamp(END) - created_d).days * 24 < hours
    status = np.where(in_transit & (status != "CANCELLED"), "IN_TRANSIT", status)
    done = np.isin(status, ["DELIVERED", "RETURNED"])
    delivered_date = (created_d + pd.to_timedelta(hours, unit="h")).normalize().where(done, pd.NaT)
    attempts = np.where(status == "DELIVERED", 1 + (rng.random(n) < 0.14) + (rng.random(n) < 0.03),
                        np.where(status == "RETURNED", 3, np.where(status == "CANCELLED", 0, 1)))
    df = pd.DataFrame({
        "shipment_id": [f"VD{i + 1:08d}" for i in range(n)],
        "created_date": pd.to_datetime(created).date, "delivered_date": delivered_date,
        "origin_hub_id": hubs.hub_id.to_numpy()[origin], "dest_hub_id": hubs.hub_id.to_numpy()[dest],
        "customer_id": cust.customer_id.to_numpy()[cidx], "service_code": np.array([s[0] for s in SERVICES])[svc],
        "status": status, "route_type": np.where(inter, "Liên miền", "Nội miền"),
        "weight_kg": weight, "shipping_fee": np.where(status == "CANCELLED", 0.0, fee), "cod_amount": cod,
        "delivery_hours": np.where(done, hours, np.nan), "on_time": np.where(status == "DELIVERED", hours <= sla, None),
        "attempts": attempts.astype(int), "payer": np.where(rng.random(n) < 0.7, "Người gửi", "Người nhận"),
    })
    df["delivered_date"] = df["delivered_date"].dt.date
    return df, sla, storm


def build_complaints(rng, ship, sla, storm):
    late = ship.delivery_hours.to_numpy() > sla
    p = np.where(ship.status == "LOST", 0.9, np.where(ship.status == "RETURNED", 0.035, np.where(late, 0.11, 0.006)))
    p = np.where(ship.status.isin(["CANCELLED", "IN_TRANSIT"]), 0.0, p)
    pick = rng.random(len(ship)) < p
    s = ship[pick].reset_index(drop=True)
    late_s, n = late[pick], int(pick.sum())
    ctype = np.where(s.status == "LOST", "Thất lạc", np.where(late_s & (rng.random(n) < 0.8), "Giao trễ",
                     rng.choice(["Hư hỏng hàng", "Thái độ nhân viên", "Sai tiền thu hộ"], size=n, p=[0.5, 0.3, 0.2])))
    lag = rng.integers(0, 6, n)
    base = pd.to_datetime(s.delivered_date.fillna(s.created_date))
    created = (base + pd.to_timedelta(lag, unit="D")).clip(upper=pd.Timestamp(END))
    comp = np.where(ctype == "Thất lạc", np.maximum(s.cod_amount, 300000) * 0.8,
                    np.where(ctype == "Hư hỏng hàng", np.round(rng.lognormal(11.8, 0.6, n) / 1000) * 1000, 0.0))
    return pd.DataFrame({
        "complaint_id": [f"KN{i + 1:07d}" for i in range(n)], "shipment_id": s.shipment_id, "created_date": created.dt.date,
        "hub_id": s.origin_hub_id, "complaint_type": ctype,       # bưu cục gửi tiếp nhận và chịu trách nhiệm khiếu nại
        "resolved_hours": np.round(rng.lognormal(3.2, 0.7, n), 1), "compensation_amount": np.round(comp, 0),
    })


def build_costs(rng, hubs, ship):
    m = pd.to_datetime(ship.created_date).dt.to_period("M").dt.to_timestamp()
    vol = ship.assign(month=m).groupby(["month", "origin_hub_id"]).size().rename("n").reset_index()
    rows = []
    for r in vol.itertuples():
        total = 4_000_000 + r.n * 18_000 * rng.uniform(0.92, 1.08)       # định phí + biến phí theo số vận đơn
        for ct, share in COST_TYPES:
            rows.append({"month": r.month.date(), "hub_id": r.origin_hub_id, "cost_type": ct,
                         "amount": round(total * share * rng.uniform(0.95, 1.05), -3)})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args()
    rng = np.random.default_rng(SEED)
    hubs, cust, services, key_customer = build_dims(rng)
    ship, sla, storm = build_shipments(rng, hubs, cust, key_customer, a.scale)
    complaints = build_complaints(rng, ship, sla, storm)
    costs = build_costs(rng, hubs, ship)
    cust = cust.drop(columns=["_w"])

    db = pathlib.Path(a.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA mart")
    for name, df in (("dim_hub", hubs), ("dim_customer", cust), ("dim_service", services), ("fact_shipment", ship),
                     ("fact_complaint", complaints), ("fact_hub_cost", costs)):
        con.register("_df", df)
        con.execute(f"CREATE TABLE mart.{name} AS SELECT * FROM _df")
        con.unregister("_df")
        print(f"  mart.{name:<16} {len(df):>9,} dòng")
    con.execute("ALTER TABLE mart.fact_shipment ALTER on_time TYPE BOOLEAN")
    con.close()
    print(f"Đã ghi {db}")


if __name__ == "__main__":
    main()
