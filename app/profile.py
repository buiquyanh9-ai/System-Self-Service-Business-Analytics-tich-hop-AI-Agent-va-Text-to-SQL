"""
Hồ sơ doanh nghiệp (config/profile.yml): tên, ngành, quy ước kho dữ liệu, câu hỏi mẫu và lời tự giới thiệu của trợ lý.

Đổi bộ file cấu hình (profile + catalog + semantic layer + policies) là hệ thống phục vụ một doanh nghiệp khác mà không sửa mã.
Lời tự giới thiệu được dựng bằng code từ cấu hình đang nạp, không do LLM viết, nên không bịa tính năng hay dữ liệu.
"""
from __future__ import annotations

import re

from . import config
from .textutil import norm

DEFAULTS = {
    "company": {"name": "doanh nghiệp của bạn", "short_name": "", "industry": "", "description": ""},
    "assistant": {"name": "Trợ lý dữ liệu", "language": "Vietnamese"},
    "warehouse": {"engine": "DuckDB", "schema": "dw", "schema_note": "", "anchor": None, "notes": [], "core_tables": []},
    "domain_pack": None,      # gói kiểm tra theo ngành viết bằng code (vd retail_abc); không khai báo thì chỉ chạy phần tổng quát
    "quality": {"skip_drift_columns": []},     # cột không đem kiểm trôi lệch phân phối (vd trạng thái có vòng đời)
    "console_example": "",    # câu SQL mẫu ở trang tự viết SQL; để trống thì biên dịch từ chỉ số đầu tiên
    "console_tries": [],      # vài lệnh để người dùng thử xem hệ thống chặn thế nào
    "out_of_scope": ["dự báo tương lai hay khuyến nghị đầu tư", "sửa, xóa dữ liệu (chỉ đọc)",
                     "trả dữ liệu bị hạn chế theo vai trò của bạn", "câu hỏi ngoài dữ liệu của doanh nghiệp"],
    "sample_questions": [],
}


def get_profile() -> dict:
    raw = config.profile()
    out = {}
    for key, default in DEFAULTS.items():
        val = raw.get(key)
        out[key] = {**default, **(val or {})} if isinstance(default, dict) else (val or default)
    return out


# ---------------------------------------------------------------------- nhận diện câu hỏi "bạn là gì / hỗ trợ gì"
_SUBJ = r"(ban|may|he thong|ung dung|tro ly|chatbot|bot|cong cu|phan mem|app)( nay)?"
_INTRO = re.compile(
    r"^(xin chao|chao( ban)?|hello|hi|hey|help|tro giup|giup( toi| minh)?( voi)?|huong dan( su dung)?)$"
    rf"|\b{_SUBJ} la (ai|gi|cai gi)\b"
    rf"|\b{_SUBJ} (co the |co )?(lam|giup|ho tro)( duoc)?( cho (toi|minh))?( nhung)? (gi|viec gi|nhung gi)\b"
    r"|\b(ho tro|giup|lam) duoc (nhung )?(gi|viec gi)\b"
    r"|\b(co|gom) (nhung )?(tinh nang|chuc nang) (gi|nao)\b"
    r"|\b(cach|huong dan) (su dung|dung)\b"
    r"|\b(toi|minh) (co the|nen) hoi (nhung )?gi\b|\bhoi duoc (nhung )?gi\b")
# "nó/đây là gì", "nó hỗ trợ gì": chỉ nhận khi gõ có dấu, vì không dấu thì "no la gi" trùng với "nợ là gì"
_INTRO_ACCENTED = re.compile(
    r"^\s*(nó|đây|cái này)\s+là\s+(ai|gì|cái gì)\b"
    r"|^\s*(nó|cái này)\s+(có thể\s+|có\s+)?(làm|giúp|hỗ trợ)(\s+được)?(\s+những)?\s+(gì|việc gì)\b")


def is_intro_question(text: str) -> bool:
    """Người dùng đang hỏi trợ lý là gì, làm được gì, dùng thế nào (hoặc chỉ chào)."""
    raw = (text or "").strip().lower()
    if not raw or len(raw) > 120:
        return False
    if _INTRO_ACCENTED.search(raw):
        return True
    return bool(_INTRO.search(re.sub(r"[^a-z0-9 ]+", " ", norm(raw)).strip()))


# ---------------------------------------------------------------------- nội dung dựng từ cấu hình
def _semantic():
    from .semantic import get_semantic
    return get_semantic()


def sample_questions() -> list[str]:
    qs = list(get_profile()["sample_questions"])
    if qs:
        return qs
    sem = _semantic()        # doanh nghiệp chưa khai báo câu mẫu: tự gợi ý từ chỉ số và chiều phân tích
    out = []
    for mname, m in list(sem.metrics.items())[:6]:
        dim = next((d for dn, d in sem.dims.items() if m.get("model") and sem.dim_supported(m["model"], dn)), None)
        out.append(f"{m['label']} theo {dim['label'].lower()}" if dim else m["label"])
    if sem.metrics:
        out.append(f"{next(iter(sem.metrics.values()))['label']} là gì?")
    return out


def intro_text() -> str:
    """Lời tự giới thiệu: trợ lý là gì, hỗ trợ gì, có dữ liệu nào, không làm gì."""
    p = get_profile()
    c, a = p["company"], p["assistant"]
    sem = _semantic()
    who = f"Mình là **{a['name']}**, trợ lý phân tích dữ liệu tự phục vụ của **{c['name']}**"
    who += f" (ngành {c['industry']})." if c.get("industry") else "."
    if c.get("description"):
        who += " " + c["description"].strip()
    areas = [m.get("label") or n for n, m in sem.models.items()]
    metrics = [m["label"] for m in sem.metrics.values()]
    dims = [d["label"] for d in sem.dims.values()]
    lines = [
        who, "",
        "**Mình hỗ trợ bạn:**",
        "- Trả lời câu hỏi số liệu bằng tiếng Việt: mình lập kế hoạch truy vấn, sinh SQL theo đúng định nghĩa chỉ số, "
        "chạy trên kho dữ liệu và trả về bảng, biểu đồ, lời diễn giải. Mọi con số đều lấy từ kết quả truy vấn.",
        "- So sánh giữa các kỳ, xếp hạng, lọc và chia theo các chiều phân tích.",
        "- Phân tích vì sao một chỉ số tăng hoặc giảm.",
        "- Giải thích định nghĩa chỉ số, tìm bảng và cột trong kho dữ liệu, báo tình trạng chất lượng dữ liệu.",
        "- Hỏi lại khi câu hỏi mơ hồ; cho bạn xem và duyệt SQL trước khi chạy truy vấn lớn.",
        "",
    ]
    data = "**Dữ liệu hiện có:** " + ("; ".join(areas) if areas else "chưa khai báo") + "."
    if metrics:
        data += f" Có {len(metrics)} chỉ số đã định nghĩa, ví dụ: " + ", ".join(x.lower() for x in metrics[:8]) + "."
    if dims:
        data += " Phân tích được theo: " + ", ".join(x.lower() for x in dims[:10]) + "."
    lines += [data, "", "**Mình không làm:** " + "; ".join(p["out_of_scope"]) + ".", ""]
    qs = sample_questions()[:5]
    if qs:
        lines.append("Bạn có thể bắt đầu bằng một câu như:")
        lines += [f"- {q}" for q in qs]
    return "\n".join(lines).strip()


STARTERS = [   # (khóa, tiêu đề, mô tả một dòng) — bốn việc chính trợ lý làm được, hiện trên màn hình chào
    ("lookup", "Tra cứu số liệu", "Hỏi một chỉ số theo thời gian, khu vực, nhóm… và nhận bảng, biểu đồ, lời giải thích."),
    ("compare", "So sánh và xếp hạng", "So với kỳ trước, cùng kỳ năm ngoái, hoặc tìm top đầu, top cuối."),
    ("why", "Tìm nguyên nhân", "Hỏi vì sao một chỉ số tăng hay giảm; trợ lý phân rã theo từng chiều và kiểm định."),
    ("define", "Hỏi định nghĩa", "Một chỉ số được tính thế nào, do bộ phận nào chịu trách nhiệm."),
]
_DEFINE = re.compile(r"\b(la gi|dinh nghia|duoc tinh (nhu the nao|the nao|ra sao)|cong thuc|nghia la gi)\b")
_COMPARE = re.compile(r"\b(so voi|so sanh|cung ky|top|cao nhat|thap nhat|nhieu nhat|it nhat|ban chay|tot nhat|xep hang)\b")


def starter_groups(per_group: int = 3) -> list[dict]:
    """Câu hỏi mẫu của doanh nghiệp chia theo bốn việc trợ lý làm được. Nhóm nào trống thì tự đặt một câu từ semantic layer."""
    from .nlu import is_why_question
    groups = {k: [] for k, _, _ in STARTERS}
    for q in sample_questions():
        n = norm(q)
        key = "why" if is_why_question(q) else "define" if _DEFINE.search(n) else "compare" if _COMPARE.search(n) else "lookup"
        groups[key].append(q)
    sem = _semantic()
    first = next(iter(sem.metrics.items()), None)
    if first:
        name, m = first
        label = m["label"]
        dim = next((d for dn, d in sem.dims.items() if d.get("kind") != "time" and dn in sem.dims_for_metric(name)), None)
        auto = {"lookup": f"{label} theo {dim['label'].lower()}" if dim else label,
                "compare": f"Top 5 {dim['label'].lower()} có {label.lower()} cao nhất" if dim else f"{label} so với kỳ trước",
                "why": f"Vì sao {label.lower()} tháng trước thay đổi?",
                "define": f"{label} là gì?"}
        for k, qs in groups.items():
            if not qs:
                qs.append(auto[k])
    return [{"key": k, "title": t, "hint": hint, "questions": groups[k][:per_group]} for k, t, hint in STARTERS if groups[k]]


def console_example() -> str:
    """Câu SQL mẫu cho trang tự viết SQL: lấy từ hồ sơ nếu có, không thì biên dịch từ chỉ số đầu tiên của semantic layer."""
    p = get_profile()
    if p.get("console_example"):
        return p["console_example"].strip()
    sem = _semantic()
    for name in sem.metrics:
        dim = next((d for d, x in sem.dims.items() if x.get("kind") != "time" and d in sem.dims_for_metric(name)), None)
        try:
            return sem.compile({"metrics": [name], "dimensions": [dim] if dim else [], "limit": 10})["sql"]
        except Exception:
            continue
    return "SELECT 1"


def console_tries() -> list[str]:
    p = get_profile()
    if p.get("console_tries"):
        return list(p["console_tries"])
    a = anchor()
    return ([f"DROP TABLE {a[0]}"] if a else []) + ["SELECT * FROM read_csv('x.csv')"]


def public_summary() -> dict:
    """Thông tin hồ sơ gửi cho giao diện (màn hình chào, tiêu đề)."""
    p = get_profile()
    sem = _semantic()
    return {"company": p["company"]["name"], "short_name": p["company"].get("short_name") or "",
            "schema": p["warehouse"]["schema"], "console_example": console_example(), "console_tries": console_tries(),
            "industry": p["company"].get("industry") or "", "description": p["company"].get("description") or "",
            "assistant": p["assistant"]["name"], "intro": intro_text(), "out_of_scope": list(p["out_of_scope"]),
            "areas": [m.get("label") or n for n, m in sem.models.items()],
            "n_metrics": len(sem.metrics), "n_dimensions": sum(1 for d in sem.dims.values() if d.get("kind") != "time"),
            "metric_examples": [m["label"] for m in list(sem.metrics.values())[:8]],
            "starters": starter_groups()}


def anchor() -> tuple[str, str] | None:
    a = get_profile()["warehouse"].get("anchor") or {}
    return (a["table"], a["column"]) if a.get("table") and a.get("column") else None


def serving_schema() -> str:
    """Schema phục vụ phân tích (tầng đã làm sạch) của doanh nghiệp đang nạp."""
    return get_profile()["warehouse"]["schema"]


def core_tables() -> list[str]:
    """Các bảng luôn kèm vào kết quả tìm schema cho LLM (bảng sự kiện chính và các chiều hay dùng)."""
    return list(get_profile()["warehouse"].get("core_tables") or [])


def domain_pack() -> str | None:
    return get_profile().get("domain_pack")
