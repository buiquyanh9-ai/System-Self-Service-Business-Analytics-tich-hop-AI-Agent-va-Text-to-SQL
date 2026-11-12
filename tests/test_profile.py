"""Hồ sơ doanh nghiệp: nhận diện câu hỏi giới thiệu, lời tự giới thiệu dựng từ cấu hình, prompt không gắn cứng một công ty."""
import pytest

from app import config, llm, profile
from app.agent import get_agent
from app.governance import UserCtx
from app.store import get_store


@pytest.mark.parametrize("q", [
    "Bạn là ai?", "bạn là gì", "Hệ thống này là gì?", "Nó là gì?", "Đây là gì", "Bạn làm được gì?",
    "bạn có thể làm gì", "Nó hỗ trợ gì?", "Hệ thống này hỗ trợ gì", "Trợ lý này giúp được gì cho tôi",
    "ban ho tro duoc nhung gi", "Có những tính năng gì?", "Hướng dẫn sử dụng", "Tôi có thể hỏi gì?", "xin chào", "help",
])
def test_intro_question_detected(q):
    assert profile.is_intro_question(q)


@pytest.mark.parametrize("q", [
    "Doanh thu thuần là gì?", "Doanh thu thuần theo tháng năm 2026", "Công nợ là gì", "no la gi",
    "Tỷ lệ hủy đơn được tính như thế nào?", "Top 5 cửa hàng có doanh thu thuần cao nhất tháng 10/2026",
    "Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ?", "",
])
def test_data_question_not_treated_as_intro(q):
    assert not profile.is_intro_question(q)


def test_intro_text_comes_from_profile_and_semantic_layer():
    p = profile.get_profile()
    txt = profile.intro_text()
    assert p["company"]["name"] in txt and p["assistant"]["name"] in txt
    assert "Mình hỗ trợ bạn" in txt and "Mình không làm" in txt
    from app.semantic import get_semantic
    first_metric = next(iter(get_semantic().metrics.values()))["label"].lower()
    assert first_metric in txt.lower()


def test_agent_answers_intro_without_calling_llm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Không được gọi LLM cho câu hỏi giới thiệu")
    monkeypatch.setattr(llm, "get_client", boom)
    user = UserCtx("analyst", "analyst", {})
    for mode in ("offline", "openrouter"):
        cid = get_store().create_conversation("analyst", "t")
        r = get_agent().ask(user, cid, "Bạn là gì và hỗ trợ được gì?", mode=mode)
        assert r["intent"] == "help" and r["type"] == "info" and not r["error"]
        assert profile.get_profile()["company"]["name"] in r["text"]


def test_system_prompt_follows_profile(monkeypatch):
    assert profile.get_profile()["company"]["name"] in llm.system_prompt()
    other = {"company": {"name": "Chuỗi Nhà thuốc XYZ", "industry": "bán lẻ dược phẩm"},
             "warehouse": {"engine": "DuckDB", "schema": "pharma", "notes": ["Prescription lines live in pharma.fct_rx."]}}
    monkeypatch.setattr(config, "profile", lambda: other)
    sp = llm.system_prompt()
    assert "Chuỗi Nhà thuốc XYZ" in sp and "bán lẻ dược phẩm" in sp and "pharma.table_name" in sp
    assert "Prescription lines live in pharma.fct_rx." in sp
    assert "ABC" not in sp and "fct_ct_dh" not in sp and "__" not in sp


def test_sample_questions_fall_back_to_semantic_layer(monkeypatch):
    monkeypatch.setattr(config, "profile", lambda: {})
    qs = profile.sample_questions()
    assert qs and all(isinstance(q, str) and q for q in qs)
    assert profile.get_profile()["assistant"]["name"] == "Trợ lý dữ liệu"
