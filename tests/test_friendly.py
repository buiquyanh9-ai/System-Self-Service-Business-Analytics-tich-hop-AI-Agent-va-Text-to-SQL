"""Phần phục vụ giao diện thân thiện: màn hình chào theo doanh nghiệp, định nghĩa chỉ số kèm câu trả lời, gợi ý hỏi tiếp,
báo rõ khi bộ luật trả lời thay LLM. Mọi thứ dựng bằng code từ cấu hình và kế hoạch truy vấn, không do LLM viết."""
import pytest

from app import llm, profile
from app.agent import get_agent
from app.governance import UserCtx
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})


def ask(question, mode="offline", follow=None):
    store = get_store()
    cid = store.create_conversation("admin", "test")
    try:
        first = get_agent().ask(ADMIN, cid, question, eval_mode=True, mode=mode)
        return (first, get_agent().ask(ADMIN, cid, follow, eval_mode=True, mode=mode)) if follow else first
    finally:
        store.delete_conversation(cid)


# ------------------------------------------------------------------ màn hình chào
def test_welcome_groups_sample_questions_by_what_the_assistant_can_do():
    groups = {g["key"]: g for g in profile.starter_groups()}
    assert list(groups) == ["lookup", "compare", "why", "define"]
    assert all(g["title"] and g["hint"] and 1 <= len(g["questions"]) <= 3 for g in groups.values())
    assert all("Vì sao" in q for q in groups["why"]["questions"])
    assert all("là gì" in q for q in groups["define"]["questions"])
    assert any("Top" in q or "so với" in q for q in groups["compare"]["questions"])


def test_welcome_fills_empty_groups_from_the_semantic_layer(monkeypatch):
    monkeypatch.setattr(profile, "sample_questions", lambda: ["Doanh thu thuần theo tháng năm 2026"])
    groups = {g["key"]: g["questions"] for g in profile.starter_groups()}
    assert groups["lookup"] == ["Doanh thu thuần theo tháng năm 2026"]
    assert groups["define"] == ["Doanh thu gộp là gì?"] and groups["why"][0].startswith("Vì sao doanh thu gộp")
    assert groups["compare"][0].startswith("Top 5 ")


def test_public_summary_describes_the_data_for_the_welcome_screen():
    s = profile.public_summary()
    assert s["n_metrics"] == 25 and s["n_dimensions"] >= 15 and "Bán hàng (cấp dòng đơn hàng)" in s["areas"]
    assert len(s["starters"]) == 4 and len(s["metric_examples"]) == 8


@pytest.mark.parametrize("group", ["lookup", "compare", "why", "define"])
def test_every_starter_question_gets_a_real_answer(group):
    """Câu hỏi hiện trên màn hình chào phải chạy được, nếu không người mới bấm vào là gặp lỗi ngay."""
    for q in next(g for g in profile.starter_groups() if g["key"] == group)["questions"]:
        r = ask(q)
        assert r["type"] in ("result", "variance", "info", "clarification"), (q, r["type"], r.get("text"))


def test_sql_page_example_and_lineage_belong_to_the_running_business():
    from app.governance import validate
    from app.lineage import get_model_lineage, models_file
    s = profile.public_summary()
    assert "dw.fct_ct_dh" in s["console_example"] and s["console_tries"][0] == "DROP TABLE dw.fct_dh" and s["schema"] == "dw"
    assert validate(s["console_example"], ADMIN).errors[0]["code"] == "UNKNOWN_COLUMN"      # câu mẫu cố ý sai tên cột để minh họa tự sửa
    assert models_file().name == "models.sql" and "dw.fct_ct_dh" in {n["id"] for n in get_model_lineage().graph()["nodes"]}


def test_sql_page_example_is_compiled_from_metrics_when_profile_has_none(monkeypatch):
    monkeypatch.setattr(profile, "get_profile", lambda: dict(profile.DEFAULTS, console_example="", console_tries=[]))
    from app.governance import validate
    assert validate(profile.console_example(), ADMIN).ok
    assert profile.console_tries() == ["SELECT * FROM read_csv('x.csv')"]


def test_unknown_question_gets_plain_guidance_without_jargon():
    r = ask("Thời tiết Hà Nội hôm nay thế nào?")
    assert r["type"] == "info" and "chưa hiểu" in r["text"] and r["suggestions"]
    assert "semantic layer" not in r["text"] and "LLM" not in r["text"]


# ------------------------------------------------------------------ kèm theo câu trả lời
def test_answer_carries_the_metric_definition_it_used():
    r = ask("Doanh thu thuần theo miền tháng 9/2026")
    assert r["definitions"] == [{"name": "doanh_thu_thuan", "label": "Doanh thu thuần", "owner": "Phòng Tài chính",
                                 "description": r["definitions"][0]["description"]}]
    assert "chiết khấu" in r["definitions"][0]["description"]


def test_follow_ups_are_suggested_and_actually_work():
    r = ask("Doanh thu thuần tháng 9/2026")
    assert r["follow_ups"][:2] == ["Chia theo vùng miền", "Chia theo kênh bán"]
    assert "So với cùng kỳ năm trước" in r["follow_ups"]
    assert "Tìm nguyên nhân biến động doanh thu thuần T9/2026" in r["follow_ups"]
    for f in r["follow_ups"]:
        _, second = ask("Doanh thu thuần tháng 9/2026", follow=f)
        assert second["type"] in ("result", "variance"), (f, second["type"], second.get("text"))
    _, split = ask("Doanh thu thuần tháng 9/2026", follow="Chia theo vùng miền")
    assert split["columns"][0] == "vung" and len(split["rows"]) == 3
    _, why = ask("Doanh thu thuần tháng 9/2026", follow="Tìm nguyên nhân biến động doanh thu thuần T9/2026")
    assert why["type"] == "variance"


def test_follow_ups_skip_what_is_already_in_the_question():
    r = ask("Doanh thu thuần theo miền tháng 10/2026 so với tháng 9/2026")
    assert "Chia theo vùng miền" not in r["follow_ups"] and "So với cùng kỳ năm trước" not in r["follow_ups"]
    ratio = ask("Tỷ lệ hủy đơn năm 2025")
    assert not any(f.startswith("Tìm nguyên nhân") for f in ratio["follow_ups"])      # chỉ số tỷ lệ chưa phân rã được
    assert "follow_ups" not in ask("Doanh thu tháng trước là bao nhiêu?")                 # câu hỏi lại thì không gợi ý


# ------------------------------------------------------------------ báo rõ khi bộ luật trả lời thay LLM
def test_answer_says_when_rules_answered_instead_of_the_model(monkeypatch):
    class Broken:
        model, effort, usage = "x/y", "", {"calls": 0}

        def plan(self, *a, **k):
            raise llm.LLMError("hết lượt gọi")

    monkeypatch.setattr(llm, "get_client", lambda settings, mode: Broken())
    r = ask("Doanh thu thuần theo miền tháng 9/2026", mode="openrouter")
    assert r["type"] == "result" and r["mode"] == "offline" and "hết lượt gọi" in r["fallback"]
    assert ask("Doanh thu thuần theo miền tháng 9/2026")["fallback"] is None
