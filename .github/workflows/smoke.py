"""Kiểm tra nhanh một container đang chạy: đăng nhập, mật khẩu admin mẫu bị vô hiệu, hỏi một câu và nhận bảng kết quả."""
import json
import sys
import urllib.error
import urllib.request

port, question = sys.argv[1], sys.argv[2]
base = f"http://127.0.0.1:{port}"


def call(path, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())


public = call("/api/public")
assert all(u["username"] != "admin" for u in public["demo_users"]), "tài khoản admin bị lộ trên màn hình đăng nhập"
try:
    call("/api/login", {"username": "admin", "password": "admin123"})
    raise SystemExit("mật khẩu admin mẫu vẫn dùng được")
except urllib.error.HTTPError as e:
    assert e.code == 401
assert call("/api/login", {"username": "admin", "password": "ci-admin"})["user"]["role"] == "admin"
token = call("/api/login", {"username": "analyst", "password": "analyst123"})["token"]
answer = call("/api/chat", {"message": question}, token)
assert answer["type"] == "result" and answer["rows"], answer.get("text")
assert (answer.get("audit") or {}).get("ok"), "lời giải thích không qua kiểm chứng số liệu"
print(f"{public['company']}: {len(answer['rows'])} dòng, kiểm chứng số liệu đạt")
