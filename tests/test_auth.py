"""Đăng nhập khi triển khai công khai: mật khẩu bí mật qua biến môi trường và danh sách tài khoản demo."""
from app import auth, main


def _demo_names():
    return [u["username"] for u in main.public_info()["demo_users"]]


def test_sample_passwords_work_and_are_listed_by_default(monkeypatch):
    monkeypatch.delenv("PASSWORD_ADMIN", raising=False)
    monkeypatch.delenv("DEMO_LOGIN", raising=False)
    assert auth.login("admin", "admin123") and auth.login("analyst", "analyst123")
    assert auth.login("admin", "sai") is None and auth.login("khong_co", "x") is None
    assert "admin" in _demo_names() and "analyst" in _demo_names()


def test_secret_password_replaces_sample_and_is_never_listed(monkeypatch):
    monkeypatch.delenv("DEMO_LOGIN", raising=False)
    monkeypatch.setenv("PASSWORD_ADMIN", "mat-khau-bi-mat")
    assert auth.login("admin", "admin123") is None            # mật khẩu mẫu trên GitHub không còn dùng được
    assert auth.login("admin", "mat-khau-bi-mat")["user"]["role"] == "admin"
    assert "admin" not in _demo_names() and "analyst" in _demo_names()
    assert all(u["password"] != "mat-khau-bi-mat" for u in main.public_info()["demo_users"])


def test_demo_login_can_be_switched_off_by_environment(monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN", "0")
    assert main.public_info()["demo_users"] == []
    assert auth.login("analyst", "analyst123")                # tắt danh sách không khóa tài khoản
