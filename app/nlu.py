"""
Bộ hiểu câu hỏi tiếng Việt OFFLINE (không cần LLM) -> kế hoạch truy vấn trên semantic layer.
Dùng khi chưa cấu hình Claude, hoặc làm phương án dự phòng khi LLM lỗi.

Các bước: chuẩn hóa (bỏ dấu) -> ý định -> thời gian (khoảng + so sánh) -> chỉ số (khớp cụm dài nhất)
-> giá trị thực thể (entity linking với danh mục giá trị trong kho) -> chiều phân tích -> top N / HAVING
-> hợp nhất ngữ cảnh hội thoại (câu hỏi nối tiếp) -> phát hiện mơ hồ (hỏi lại).
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field

from . import config
from .catalog import get_catalog
from .semantic import get_semantic
from .textutil import norm

DIM_PRIORITY = ["vung", "nganh_hang", "nhom_kenh", "kenh", "tinh_tp", "chi_nhanh", "kho", "thuong_hieu", "hang_tv",
                "khuyen_mai", "pttt", "ly_do_tra", "loai_ch", "gioi_tinh", "ncc", "nhom_sp", "san_pham", "tinh_kh"]
EXTRA_VALUE_ALIASES = {
    "vung": {"Miền Bắc": ["mien bac", "phia bac", "ngoai bac", "khu vuc phia bac", "khu vuc mien bac"],
             "Miền Trung": ["mien trung", "khu vuc mien trung", "phia trung"],
             "Miền Nam": ["mien nam", "phia nam", "trong nam", "khu vuc phia nam", "khu vuc mien nam"]},
    "kenh": {"Tại cửa hàng": ["tai cua hang", "kenh cua hang", "mua truc tiep", "ban truc tiep"],
             "Website": ["website", "web", "trang web"],
             "Ứng dụng di động": ["app", "ung dung", "mobile app", "ung dung di dong"],
             "Sàn TMĐT": ["san tmdt", "san thuong mai dien tu", "shopee", "lazada", "tiki", "marketplace"]},
    "nhom_kenh": {"Online": ["online", "truc tuyen", "kenh online"], "Offline": ["offline", "kenh offline"]},
    "nganh_hang": {"Điện thoại": ["dien thoai", "smartphone", "dien thoai di dong"],
                   "Laptop": ["laptop", "may tinh xach tay", "notebook"],
                   "Máy tính bảng": ["may tinh bang", "tablet"],
                   "Đồng hồ thông minh": ["dong ho thong minh", "smartwatch", "dong ho"],
                   "Tivi": ["tivi", "ti vi", "tv"], "Âm thanh": ["am thanh", "thiet bi am thanh"],
                   "Gia dụng": ["gia dung", "do gia dung"], "Điện lạnh": ["dien lanh"], "Phụ kiện": ["phu kien"]},
    "hang_tv": {"Kim cương": ["kim cuong", "hang kim cuong", "the kim cuong"],
                "Vàng": ["hang vang", "the vang", "khach hang vang", "thanh vien vang"],
                "Bạc": ["hang bac", "the bac", "khach hang bac", "thanh vien bac"],
                "Thường": ["hang thuong", "the thuong", "thanh vien thuong"]},
    "gioi_tinh": {"Nữ": ["khach hang nu", "khach nu", "gioi tinh nu", "phu nu"],
                  "Nam": ["khach hang nam", "khach nam", "gioi tinh nam"]},
    "pttt": {"Tiền mặt": ["tien mat"], "Thẻ ngân hàng": ["the ngan hang", "quet the", "the tin dung"],
             "Chuyển khoản QR": ["chuyen khoan", "qr"], "Ví điện tử": ["vi dien tu", "momo", "zalopay"],
             "Trả góp": ["tra gop"], "COD": ["cod", "thanh toan khi nhan hang"]},
    "loai_ch": {"Flagship": ["flagship", "cua hang flagship"], "Mini": ["cua hang mini", "mini"],
                "Tiêu chuẩn": ["cua hang tieu chuan"]},
}
# cụm từ sau khi bỏ dấu trùng với từ thông dụng khác ("hãng" = "hàng", "năm" = "nam", "sàn" = "sản"...)
AMBIGUOUS_NORM = {"hang", "nam", "san", "the", "ban", "ma", "mau", "kho hang"}
SKIP_BARE_VALUES = {"hang_tv", "gioi_tinh", "loai_ch", "trang_thai"}
MONTH_WORDS = {"gieng": 1, "chap": 12, "muoi mot": 11, "muoi hai": 12, "muoi": 10}
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
FOLLOWUP_START = ("con ", "the con", "vay con", "vay ", "con neu", "neu ", "chi ", "loc ", "bo ", "them ", "so voi",
                  "chia theo", "theo ", "tach theo", "ve bieu do", "hien thi", "doi sang", "chuyen sang", "top ", "con lai")
GROUP_MARKERS = ("theo", "tung", "moi", "cac", "giua cac", "nhung", "phan theo", "chia theo", "tach theo", "tat ca cac")


@dataclass
class Span:
    start: dt.date
    end: dt.date           # không bao gồm
    label: str
    pos: int
    relative: str | None = None   # 'prev' nếu là kỳ trước tương đối


@dataclass
class NLUResult:
    intent: str = "metric_query"
    plan: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    clarification: dict | None = None
    follow_up: bool = False
    chart_hint: str | None = None
    matched: dict = field(default_factory=dict)
    confidence: float = 0.8
    metric_topic: str | None = None


def _fmt(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def month_span(y, m):
    s = dt.date(y, m, 1)
    return s, _add_months(s, 1)


def quarter_span(y, qn):
    s = dt.date(y, 3 * (qn - 1) + 1, 1)
    return s, _add_months(s, 3)


def previous_period(s: dt.date, e: dt.date) -> tuple[dt.date, dt.date, str]:
    if s.day == 1 and e.day == 1:
        months = (e.year - s.year) * 12 + e.month - s.month
        ps = _add_months(s, -months)
        return ps, s, _period_label(ps, s)
    length = (e - s).days
    return s - dt.timedelta(days=length), s, _period_label(s - dt.timedelta(days=length), s)


def same_period_last_year(s, e):
    ps, pe = _add_months(s, -12), _add_months(e, -12)
    return ps, pe, _period_label(ps, pe)


def _period_label(s: dt.date, e: dt.date) -> str:
    last = e - dt.timedelta(days=1)
    if s.day == 1 and e.day == 1:
        months = (e.year - s.year) * 12 + e.month - s.month
        if months == 1:
            return f"T{s.month}/{s.year}"
        if months == 3 and s.month in (1, 4, 7, 10):
            return f"Q{(s.month - 1) // 3 + 1}/{s.year}"
        if months == 12 and s.month == 1:
            return f"năm {s.year}"
        return f"T{s.month}/{s.year}–T{last.month}/{last.year}"
    if s == last:
        return _fmt(s)
    return f"{_fmt(s)}–{_fmt(last)}"


class OfflineNLU:
    def __init__(self):
        self.sem = get_semantic()
        self.cfg = config.semantic_layer()
        self._build_indexes()

    # ------------------------------------------------------------------ chỉ mục
    def _build_indexes(self):
        self.metric_phr = []
        for name, m in self.sem.metrics.items():
            for s in set([m["label"]] + m.get("synonyms", [])):
                p = norm(re.sub(r"\(.*?\)", "", s))
                if p:
                    self.metric_phr.append((p, name))
        extra = {"ty_le_dat_kh": ["dat ke hoach doanh thu", "ty le dat ke hoach doanh thu", "phan tram ke hoach",
                                  "hoan thanh ke hoach doanh thu", "so voi ke hoach"],
                 "so_luong_ban": ["ban duoc bao nhieu cai", "ban duoc bao nhieu chiec", "ban duoc bao nhieu may",
                                  "bao nhieu san pham", "so luong", "ban ra bao nhieu"],
                 "doanh_thu_gop": ["doanh so ban hang"],
                 "gia_tri_ton_kho": ["gia tri hang ton"]}
        for k, lst in extra.items():
            for p in lst:
                self.metric_phr.append((p, k))
        self.metric_phr.sort(key=lambda x: -len(x[0]))

        self.dim_phr = []
        for name, d in self.sem.dims.items():
            if d.get("kind") == "time":
                continue
            for s in set([d["label"]] + d.get("synonyms", [])):
                p = norm(re.sub(r"\(.*?\)", "", s))
                if p and len(p) > 1 and p not in AMBIGUOUS_NORM:
                    self.dim_phr.append((p, name))
        self.dim_phr += [("hang san xuat", "thuong_hieu"), ("nhan hieu", "thuong_hieu"), ("cac hang", "thuong_hieu"),
                         ("theo hang", "thuong_hieu")]
        self.dim_phr.sort(key=lambda x: -len(x[0]))

        cat = get_catalog()
        self.value_phr = []  # (phrase, dim, canonical value, op)
        aliases_prov = {}
        try:
            from .warehouse import get_warehouse
            for alias, prov in get_warehouse().fetch("SELECT alias, tinh_tp FROM ref.tinh_alias"):
                aliases_prov.setdefault(prov, set()).add(norm(alias))
        except Exception:
            pass
        for dim in DIM_PRIORITY:
            if dim not in self.sem.dims:
                continue
            vals = self.sem.dim_values(dim)
            for v in vals:
                # giá trị là từ thông dụng ("Thường", "Vàng", "Nam"≈"năm") -> chỉ nhận qua alias có ngữ cảnh
                forms = set() if dim in SKIP_BARE_VALUES else {norm(v)}
                if dim == "chi_nhanh":
                    base = norm(v.replace("ABC ", ""))
                    forms |= {f"cua hang {base}", f"chi nhanh {base}", f"abc {base}", base}
                if dim == "kho":
                    base = norm(v.replace("Kho tổng ", "").replace("Kho ", ""))
                    forms |= {f"kho {base}", f"kho tong {base}"}
                if dim == "khuyen_mai":
                    forms.add(norm(re.sub(r"\s*\d{4}$", "", v)))
                if dim in ("tinh_tp", "tinh_kh"):
                    forms |= {a for a in aliases_prov.get(v, set()) if len(a) > 2}
                for f in EXTRA_VALUE_ALIASES.get(dim, {}).get(v, []):
                    forms.add(f)
                op = "like" if dim == "san_pham" else "in"
                for f in forms:
                    if f and len(f) >= 2 and not f.isdigit():
                        self.value_phr.append((f, dim, v, op))
        # tên sản phẩm rút gọn (bỏ dung lượng) -> lọc LIKE theo dòng sản phẩm
        for v in self.sem.dim_values("san_pham"):
            short = re.sub(r"\s+\d+GB$|\s+\(.*\)$", "", v)
            if short != v:
                self.value_phr.append((norm(short), "san_pham", short, "like"))
        self.value_phr.sort(key=lambda x: (-len(x[0]), DIM_PRIORITY.index(x[1]) if x[1] in DIM_PRIORITY else 99))
        self.anchor = dt.date.fromisoformat(cat.anchor_date())
        self.data_start = dt.date.fromisoformat(str(cat.meta.get("ngay_bat_dau", "2025-01-01"))[:10])

    # ------------------------------------------------------------------ tiện ích span
    @staticmethod
    def _free(mask, s, e):
        return not any(mask[s:e])

    @staticmethod
    def _take(mask, s, e):
        for i in range(s, e):
            mask[i] = True

    def _find_all(self, text, phrase, mask):
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text):
            if self._free(mask, m.start(), m.end()):
                yield m

    # ------------------------------------------------------------------ thời gian
    def parse_time(self, t: str, mask) -> list[Span]:
        A = self.anchor
        spans: list[Span] = []

        def add(m, s, e, label, rel=None):
            if not self._free(mask, m.start(), m.end()):
                return
            self._take(mask, m.start(), m.end())
            spans.append(Span(s, e, label, m.start(), rel))

        def year_of_month(mo, explicit=None):
            if explicit:
                return int(explicit)
            return A.year if mo <= A.month else A.year - 1

        # khoảng "từ ... đến ..."
        for m in re.finditer(r"tu (?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4}) (?:den|toi|-) (?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})", t):
            s = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            e = dt.date(int(m.group(6)), int(m.group(5)), int(m.group(4))) + dt.timedelta(days=1)
            add(m, s, e, _period_label(s, e))
        for m in re.finditer(r"tu thang (\d{1,2})(?:/(\d{4}))?(?: nam (\d{4}))? (?:den|toi|-) thang (\d{1,2})(?:/(\d{4}))?(?: nam (\d{4}))?", t):
            y2 = m.group(5) or m.group(6)
            y1 = m.group(2) or m.group(3) or y2
            m1, m2 = int(m.group(1)), int(m.group(4))
            yy2 = year_of_month(m2, y2)
            yy1 = int(y1) if y1 else (yy2 if m1 <= m2 else yy2 - 1)
            s, _ = month_span(yy1, m1)
            _, e = month_span(yy2, m2)
            add(m, s, e, _period_label(s, e))
        for m in re.finditer(r"(\d+) thang (dau|cuoi) nam(?: (\d{4}))?", t):
            n, y = int(m.group(1)), int(m.group(3) or A.year)
            if m.group(2) == "dau":
                s, e = dt.date(y, 1, 1), _add_months(dt.date(y, 1, 1), n)
            else:
                s, e = _add_months(dt.date(y + 1, 1, 1), -n), dt.date(y + 1, 1, 1)
            add(m, s, e, f"{n} tháng {m.group(2) == 'dau' and 'đầu' or 'cuối'} năm {y}")
        for m in re.finditer(r"nua (dau|cuoi) nam(?: (\d{4}))?", t):
            y = int(m.group(2) or A.year)
            s, e = (dt.date(y, 1, 1), dt.date(y, 7, 1)) if m.group(1) == "dau" else (dt.date(y, 7, 1), dt.date(y + 1, 1, 1))
            add(m, s, e, f"{'nửa đầu' if m.group(1) == 'dau' else 'nửa cuối'} năm {y}")
        for m in re.finditer(r"(?:tu )?dau nam(?: (\d{4}))? (?:den nay|toi nay|den hien tai)|tu dau nam|ytd|luy ke nam", t):
            y = int(m.group(1)) if m.lastindex and m.group(1) else A.year
            s, e = dt.date(y, 1, 1), A + dt.timedelta(days=1)
            add(m, s, e, f"từ đầu năm {y} đến {_fmt(A)}")
        # N đơn vị gần nhất
        for m in re.finditer(r"(\d+) (ngay|tuan|thang|quy|nam) (?:gan nhat|gan day|qua|vua qua|truoc do|cuoi cung)", t):
            n, u = int(m.group(1)), m.group(2)
            e = A + dt.timedelta(days=1)
            s = {"ngay": lambda: e - dt.timedelta(days=n), "tuan": lambda: e - dt.timedelta(weeks=n),
                 "thang": lambda: _add_months(e, -n), "quy": lambda: _add_months(e, -3 * n),
                 "nam": lambda: _add_months(e, -12 * n)}[u]()
            add(m, s, e, f"{n} {dict(ngay='ngày', tuan='tuần', thang='tháng', quy='quý', nam='năm')[u]} gần nhất (đến {_fmt(A)})")
        # ngày cụ thể
        for m in re.finditer(r"(?:ngay )?(\d{1,2})/(\d{1,2})/(\d{4})", t):
            try:
                s = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
            add(m, s, s + dt.timedelta(days=1), _fmt(s))
        # tháng
        for m in re.finditer(r"(?:thang|t)\s?(\d{1,2})\s?(?:/|-)\s?(\d{4})", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                s, e = month_span(int(m.group(2)), mo)
                add(m, s, e, f"T{mo}/{m.group(2)}")
        for m in re.finditer(r"thang (\d{1,2}) nam (\d{4})", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                s, e = month_span(int(m.group(2)), mo)
                add(m, s, e, f"T{mo}/{m.group(2)}")
        for m in re.finditer(r"thang (\d{1,2})(?![0-9/])", t):
            mo = int(m.group(1))
            if 1 <= mo <= 12:
                y = year_of_month(mo)
                s, e = month_span(y, mo)
                add(m, s, e, f"T{mo}/{y}")
        for w, mo in MONTH_WORDS.items():
            for m in re.finditer(r"thang " + w + r"(?![a-z])(?: nam (\d{4}))?", t):
                y = year_of_month(mo, m.group(1))
                s, e = month_span(y, mo)
                add(m, s, e, f"T{mo}/{y}")
        # quý
        for m in re.finditer(r"(?<![a-z0-9])(?:quy|q)\s?(\d|iv|iii|ii|i)(?![a-z0-9])\s?(?:/|-|nam)?\s?(\d{4})?", t):
            g = m.group(1)
            qn = int(g) if g.isdigit() else ROMAN[g]
            if not 1 <= qn <= 4:
                continue
            if m.group(2):
                y = int(m.group(2))
            else:
                y = A.year if 3 * (qn - 1) + 1 <= A.month else A.year - 1
            s, e = quarter_span(y, qn)
            add(m, s, e, f"Q{qn}/{y}")
        # tương đối
        cur_m = month_span(A.year, A.month)
        prev_m = month_span(*(lambda d: (d.year, d.month))(_add_months(dt.date(A.year, A.month, 1), -1)))
        qn = (A.month - 1) // 3 + 1
        cur_q = quarter_span(A.year, qn)
        pq = _add_months(cur_q[0], -3)
        prev_q = quarter_span(pq.year, (pq.month - 1) // 3 + 1)
        monday = A - dt.timedelta(days=A.weekday())
        rel = [
            (r"hom nay", (A, A + dt.timedelta(days=1), f"hôm nay ({_fmt(A)})", None)),
            (r"hom qua", (A - dt.timedelta(days=1), A, f"hôm qua ({_fmt(A - dt.timedelta(days=1))})", None)),
            (r"tuan nay", (monday, A + dt.timedelta(days=1), f"tuần này ({_fmt(monday)}–{_fmt(A)})", None)),
            (r"tuan truoc|tuan roi", (monday - dt.timedelta(days=7), monday, f"tuần trước ({_fmt(monday - dt.timedelta(days=7))}–{_fmt(monday - dt.timedelta(days=1))})", "prev")),
            (r"thang nay|thang hien tai", (cur_m[0], A + dt.timedelta(days=1), f"tháng này (T{A.month}/{A.year}, đến {_fmt(A)})", None)),
            (r"thang truoc|thang roi|thang vua qua", (prev_m[0], prev_m[1], f"tháng trước (T{prev_m[0].month}/{prev_m[0].year})", "prev")),
            (r"quy nay|quy hien tai", (cur_q[0], A + dt.timedelta(days=1), f"quý này (Q{qn}/{A.year}, đến {_fmt(A)})", None)),
            (r"quy truoc|quy roi", (prev_q[0], prev_q[1], f"quý trước ({_period_label(*prev_q)})", "prev")),
            (r"nam nay|nam hien tai", (dt.date(A.year, 1, 1), A + dt.timedelta(days=1), f"năm nay ({A.year}, đến {_fmt(A)})", None)),
            (r"nam ngoai|nam truoc|nam roi|nam vua qua", (dt.date(A.year - 1, 1, 1), dt.date(A.year, 1, 1), f"năm {A.year - 1}", "prev")),
            (r"ky truoc|cung ky truoc", None),
        ]
        for pat, val in rel:
            for m in re.finditer(r"(?<![a-z0-9])(?:" + pat + r")(?![a-z0-9])", t):
                if val is None:
                    continue
                add(m, val[0], val[1], val[2], val[3])
        # năm
        for m in re.finditer(r"(?:nam )?(?<![0-9/])(20\d{2})(?![0-9])", t):
            y = int(m.group(1))
            add(m, dt.date(y, 1, 1), dt.date(y + 1, 1, 1), f"năm {y}")
        spans.sort(key=lambda s: s.pos)
        return spans

    # ------------------------------------------------------------------ phân tích chính
    def parse(self, question: str, memory: dict | None = None) -> NLUResult:
        memory = memory or {}
        prefs = memory.get("prefs", {})
        prev = memory.get("last_plan")
        t = norm(question)
        mask = [False] * len(t)
        r = NLUResult()

        # ý định
        if re.search(r"\b(vi sao|tai sao|nguyen nhan|ly do (gi|nao)|giai thich (bien dong|su|vi)|why|dieu gi khien|do dau)\b", t):
            r.intent = "variance"
        elif re.search(r"\b(dinh nghia|duoc tinh (nhu the nao|the nao|ra sao)|cong thuc|tinh nhu the nao|nghia la gi|la gi)\b", t):
            r.intent = "metric_info"
        elif re.search(r"\b(bang nao|cot nao|co nhung bang|danh sach bang|schema|luoc do|cau truc du lieu|cot .* nghia)\b", t):
            r.intent = "schema_info"
        elif re.search(r"\b(chat luong du lieu|du lieu (bat thuong|bi loi|co van de)|kiem tra du lieu|data quality|drift|sai lech du lieu)\b", t):
            r.intent = "dq"
        elif re.search(r"^(xin chao|chao|hello|hi|help|huong dan|ban (la ai|lam duoc gi|co the lam gi))\b", t):
            r.intent = "help"

        # so sánh
        cmp_pos = None
        m = re.search(r"\b(so voi|so sanh voi|vs|doi chieu voi)\b", t)
        if m:
            cmp_pos = m.start()
        yoy = bool(re.search(r"\b(cung ky|yoy|nam truoc cung ky)\b", t))
        growth = bool(re.search(r"\b(tang truong|mom|tang giam|bien dong|thay doi)\b", t))

        spans = self.parse_time(t, mask)
        cur = [s for s in spans if cmp_pos is None or s.pos < cmp_pos]
        cmp = [s for s in spans if cmp_pos is not None and s.pos > cmp_pos]

        # chỉ số
        metrics, mpos = [], []
        for p, name in self.metric_phr:
            for mm in self._find_all(t, p, mask):
                self._take(mask, mm.start(), mm.end())
                if name not in metrics:
                    metrics.append(name)
                    mpos.append(mm.start())
        order = sorted(range(len(metrics)), key=lambda i: mpos[i])
        metrics = [metrics[i] for i in order]
        revenue_generic = False
        for mm in self._find_all(t, "doanh thu", mask):
            self._take(mask, mm.start(), mm.end())
            revenue_generic = True
        if re.search(r"\bban duoc bao nhieu tien\b", t):
            revenue_generic = True

        # giá trị thực thể -> bộ lọc
        filters: dict[str, dict] = {}
        for p, dim, val, op in self.value_phr:
            for mm in self._find_all(t, p, mask):
                d2 = dim
                if dim == "tinh_tp" and re.search(r"khach( hang)?( o| tai| song o| den tu)?\s*$", t[:mm.start()][-25:]):
                    d2 = "tinh_kh"
                self._take(mask, mm.start(), mm.end())
                f = filters.setdefault(d2, {"dimension": d2, "op": op, "values": []})
                if val not in f["values"]:
                    f["values"].append(val)
                r.matched.setdefault("values", []).append({"text": p, "dimension": d2, "value": val})
        neg = re.search(r"\b(tru|ngoai tru|khong tinh|khong ke|loai tru|bo qua)\b", t)
        if neg:
            for f in filters.values():
                pass  # đơn giản: không hỗ trợ phủ định tự động (ghi chú giả định)
            r.assumptions.append("Chưa hỗ trợ điều kiện loại trừ trong chế độ offline — vui lòng kiểm tra bộ lọc.")

        # chiều phân tích
        dims: list[str] = []
        grain = None
        for gm in re.finditer(r"\b(theo|tung|moi|hang|cac|qua cac|tren tung|va) (ngay|tuan|thang|quy|nam)\b(?! \d)", t):
            if mask[gm.start(2)]:
                continue  # "mua hàng tháng này": "tháng này" là biểu thức thời gian
            grain = gm.group(2)
            break
        if grain is None and re.search(r"\b(xu huong|dien bien|bieu do duong|theo thoi gian|trend)\b", t):
            grain = "auto"
        for p, dim in self.dim_phr:
            for mm in self._find_all(t, p, mask):
                self._take(mask, mm.start(), mm.end())
                before = t[:mm.start()].rstrip()
                after = t[mm.end():].lstrip()
                if dim in filters and not re.search(r"(theo|tung|cac|moi)$", before):
                    continue  # "ngành hàng Điện thoại" -> chỉ là bộ lọc
                if dim not in dims:
                    dims.append(dim)
                r.matched.setdefault("dimensions", []).append(p)
                _ = after

        # top N / thứ tự
        limit, direction = None, None
        for mt in re.finditer(r"\btop\s?(\d+)\b|\b(\d+) (?:[a-z ]{0,30}?)(cao nhat|lon nhat|nhieu nhat|tot nhat|thap nhat|it nhat|kem nhat|ban chay nhat)\b", t):
            g = 1 if mt.group(1) else 2
            if mask[mt.start(g)]:
                continue  # con số thuộc biểu thức thời gian
            limit = int(mt.group(g))
            break
        if re.search(r"\b(thap nhat|it nhat|kem nhat|te nhat|cham nhat|giam manh nhat)\b", t):
            direction = "asc"
        elif re.search(r"\b(cao nhat|lon nhat|nhieu nhat|tot nhat|ban chay|top|dan dau|manh nhat|tang manh nhat)\b", t):
            direction = "desc"
        if direction and limit is None and dims and re.search(r"\b(nao|gi)\b", t) and not re.search(r"\b(nhung|cac)\b", t):
            limit = 1
        if direction and limit is None and dims:
            limit = 10

        # HAVING
        having = []
        hm = re.search(r"\b(tren|hon|lon hon|vuot|duoi|nho hon|it hon|>|<)\s?(\d+(?:[.,]\d+)?)\s?(ty|ti|trieu|nghin|ngan|k|don|san pham)?\b", t)
        if hm and dims and hm.group(3) and not mask[hm.start(2)]:
            val = float(hm.group(2).replace(",", "."))
            unit = hm.group(3) or ""
            val *= {"ty": 1e9, "ti": 1e9, "trieu": 1e6, "nghin": 1e3, "ngan": 1e3, "k": 1e3}.get(unit, 1)
            op = "<" if hm.group(1) in ("duoi", "nho hon", "it hon", "<") else ">"
            having.append({"op": op, "value": val})

        # biểu đồ
        if re.search(r"\b(bieu do tron|pie|ty trong|co cau|chiem bao nhieu)\b", t):
            r.chart_hint = "pie"
        elif re.search(r"\b(bieu do cot|cot)\b", t) and "bieu do" in t:
            r.chart_hint = "bar"
        elif re.search(r"\b(bieu do duong|duong)\b", t) and "bieu do" in t:
            r.chart_hint = "line"

        # ---------------------------------------------------------------- hợp nhất ngữ cảnh
        follow = bool(prev) and (t.startswith(FOLLOWUP_START) or "thi sao" in t or "the nao" in t and not metrics
                                 or (not metrics and not revenue_generic and r.intent == "metric_query"))
        r.follow_up = follow
        if follow and prev:
            if not metrics and not revenue_generic:
                metrics = list(prev.get("metrics", []))
            if not cur and prev.get("time_range"):
                tr = prev["time_range"]
                cur = [Span(dt.date.fromisoformat(tr["start"]), dt.date.fromisoformat(tr["end"]), tr.get("label", ""), 0)] \
                    if tr.get("start") and tr.get("end") else []
            pf = {f["dimension"]: f for f in prev.get("filters", [])}
            if not re.search(r"\b(tat ca|toan bo|bo loc|bo het|toan quoc|ca nuoc)\b", t):
                for k, f in pf.items():
                    filters.setdefault(k, f)
            if not dims and not grain:
                dims = [d for d in prev.get("dimensions", []) if self.sem.dims[d].get("kind") != "time"]
                pg = [d for d in prev.get("dimensions", []) if self.sem.dims[d].get("kind") == "time"]
                grain = pg[0] if pg else None
                # bộ lọc mới trên chiều đang nhóm -> bỏ nhóm theo chiều đó
                dims = [d for d in dims if d not in filters or d in pf]
            if limit is None and prev.get("limit") and not re.search(r"\b(tat ca|toan bo)\b", t):
                limit = prev.get("limit")
            if not direction and prev.get("order_by"):
                direction = prev["order_by"][0].get("direction")
            if not cmp and prev.get("compare") and not cur:
                c = prev["compare"]
                cmp = [Span(dt.date.fromisoformat(c["start"]), dt.date.fromisoformat(c["end"]), c.get("label", ""), 0)]

        # ---------------------------------------------------------------- mơ hồ
        if revenue_generic:
            pm = prefs.get("revenue_metric")
            if pm:
                if pm not in metrics:
                    metrics.insert(0, pm)
                r.assumptions.append(f"“Doanh thu” hiểu là {self.sem.metrics[pm]['label']} (theo lựa chọn trước đó trong hội thoại).")
            elif not any(m in metrics for m in ("doanh_thu_thuan", "doanh_thu_gop")):
                amb = self.cfg["ambiguities"][0]
                r.clarification = {"id": amb["id"], "question": amb["question"], "pref_key": "revenue_metric",
                                   "options": [{"label": o["label"], "metric": o["metric"]} for o in amb["options"]]}
        if not metrics and r.clarification is None and r.intent in ("metric_query", "variance"):
            if re.search(r"\b(ban chay|tot nhat|hieu qua nhat|noi bat nhat|manh nhat|dan dau|top)\b", t):
                pm = prefs.get("best_criteria")
                if pm:
                    metrics = [pm]
                    r.assumptions.append(f"Xếp hạng theo {self.sem.metrics[pm]['label']} (theo lựa chọn trước đó).")
                else:
                    amb = self.cfg["ambiguities"][1]
                    r.clarification = {"id": amb["id"], "question": amb["question"], "pref_key": "best_criteria",
                                       "options": [{"label": o["label"], "metric": o["metric"]} for o in amb["options"]]}

        # ---------------------------------------------------------------- thời gian -> plan
        time_range, compare = None, None
        if cur:
            if len(cur) > 1 and not cmp:
                s, e = min(x.start for x in cur), max(x.end for x in cur)
                if re.search(r"\b(va|voi|,)\b", t) and grain is None and all(x.start.day == 1 for x in cur):
                    grain = "thang" if all((x.end - x.start).days <= 31 for x in cur) else ("quy" if all((x.end - x.start).days <= 92 for x in cur) else "nam")
                time_range = {"start": s.isoformat(), "end": e.isoformat(), "label": _period_label(s, e)}
            else:
                c0 = cur[0]
                time_range = {"start": c0.start.isoformat(), "end": c0.end.isoformat(), "label": c0.label}
        if cmp_pos is not None or (r.intent == "variance") or (growth and time_range and not dims):
            if time_range is None and r.intent == "variance":
                ps = month_span(*(lambda d: (d.year, d.month))(_add_months(dt.date(self.anchor.year, self.anchor.month, 1), -1)))
                time_range = {"start": ps[0].isoformat(), "end": ps[1].isoformat(), "label": f"T{ps[0].month}/{ps[0].year}"}
                r.assumptions.append(f"Chưa nêu kỳ phân tích — dùng tháng gần nhất đã đủ dữ liệu ({time_range['label']}).")
            if time_range:
                s, e = dt.date.fromisoformat(time_range["start"]), dt.date.fromisoformat(time_range["end"])
                if cmp and cmp[0].relative != "prev":
                    compare = {"start": cmp[0].start.isoformat(), "end": cmp[0].end.isoformat(), "label": cmp[0].label}
                elif yoy:
                    ps, pe, lab = same_period_last_year(s, e)
                    compare = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab + " (cùng kỳ)"}
                elif cmp_pos is not None or r.intent == "variance" or growth:
                    ps, pe, lab = previous_period(s, e)
                    compare = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab + " (kỳ trước)"}
        if time_range is None and r.intent == "metric_query" and metrics:
            r.assumptions.append(f"Không nêu thời gian — tính trên toàn bộ dữ liệu ({_fmt(self.data_start)}–{_fmt(self.anchor)}).")
        if time_range and time_range["end"] > self.anchor.isoformat():
            if time_range["start"] <= self.anchor.isoformat():
                r.assumptions.append(f"Dữ liệu mới có đến {_fmt(self.anchor)} (ngày cuối có thể chưa nạp đủ).")
        if grain == "auto":
            if time_range:
                days = (dt.date.fromisoformat(time_range["end"]) - dt.date.fromisoformat(time_range["start"])).days
                grain = "ngay" if days <= 45 else ("tuan" if days <= 120 else "thang")
            else:
                grain = "thang"

        all_dims = ([grain] if grain else []) + [d for d in dims if d not in ("ngay", "tuan", "thang", "quy", "nam")]
        # metric không hỗ trợ chiều -> bỏ chiều + ghi chú
        if metrics:
            ok_dims = set.intersection(*[set(self.sem.dims_for_metric(m)) for m in metrics])
            dropped = [d for d in all_dims if d not in ok_dims]
            if dropped:
                if grain in dropped and grain in ("ngay", "tuan") and "thang" in ok_dims:
                    all_dims = ["thang" if d == grain else d for d in all_dims]
                    r.assumptions.append("Chỉ số chỉ có độ chi tiết theo tháng — đã chuyển sang theo tháng.")
                    dropped = [d for d in dropped if d != grain]
                for d in dropped:
                    if d in all_dims:
                        all_dims.remove(d)
                    r.assumptions.append(f"Bỏ chiều '{self.sem.dims[d]['label']}' vì chỉ số không phân tích được theo chiều này.")
            for d in list(filters):
                if d not in ok_dims:
                    r.assumptions.append(f"Bỏ bộ lọc '{self.sem.dims[d]['label']}' vì chỉ số không hỗ trợ.")
                    filters.pop(d)
        plan = {"metrics": metrics, "dimensions": all_dims, "filters": list(filters.values()),
                "time_range": time_range, "compare": compare if compare and r.intent != "variance" else None,
                "limit": limit}
        if r.intent == "variance":
            plan["variance_compare"] = compare
        if direction and metrics:
            plan["order_by"] = [{"field": metrics[0], "direction": direction}]
        if having and metrics:
            having[0]["metric"] = metrics[0]
            plan["having"] = having
        r.plan = plan
        if r.intent == "metric_query" and not metrics and r.clarification is None:
            r.intent = "unknown"
            r.confidence = 0.2
        if r.intent in ("metric_info", "schema_info", "dq", "help"):
            r.metric_topic = metrics[0] if metrics else ("__revenue__" if revenue_generic else None)
            r.clarification = None
        return r


_nlu: OfflineNLU | None = None


def get_nlu(reload: bool = False) -> OfflineNLU:
    global _nlu
    if _nlu is None or reload:
        _nlu = OfflineNLU()
    return _nlu
