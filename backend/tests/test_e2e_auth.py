"""Step 9 E2E：完整 M4 鉴权流程，不拆 half-fake/half-real。

两个关键设计：
1. CLI subprocess + API HTTP 请求走同一套真实 DB + 真实 Redis
   （API server 在 Docker 里跑；CLI subprocess 在 Docker 里跑 `cli_runtime()`，
    两者均用 littlebox DB + redis://redis:6379/0）
2. Redis 写前 flush，保证每条用例干净

用例顺序（遵循"logout → re-login → reset → old token 401"）：
1. create_parent CLI → parent phone+password
2. login → parent token
3. /me (parent token) → 200
4. POST /children → child_id
5. POST /bind-tokens body:{ child_user_id } → bind_token
6. POST /bind-tokens/{bind_token}/redeem body:{ device_id } → child token（永不过期）
7. /me (child token) → 200
8. POST /bind-tokens body:{ child_user_id } (child token) → 403
9. POST /auth/logout (parent token) → 204
10. 老 parent token → 401
11. re-login → 新 parent token
12. reset_parent_password CLI
13. 旧 parent token（在 reset 前拿的）→ 401
14. 新密码 login → 200
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Generator

import httpx
import pytest

_BACKEND_ROOT = Path(__file__).parent.parent
# pytest 在 Docker 容器内运行，所以用容器内部端口（API 监听 8000）
_BASE_URL = "http://localhost:8000"


def _run_create_parent(note: str) -> tuple[int, str, str]:
    """在 Docker 里跑 create_parent CLI。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.scripts.create_parent", "--note", note],
        cwd=str(_BACKEND_ROOT),
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


def _run_reset_password(phone: str) -> tuple[int, str, str]:
    """在 Docker 里跑 reset_parent_password CLI。"""
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.scripts.reset_parent_password", "--phone", phone],
        cwd=str(_BACKEND_ROOT),
        env={**os.environ},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


def _flush_redis() -> None:
    """从容器内用 redis://redis:6379/0 写前 flush Redis，保证每条用例干净。"""
    import redis as redis_lib

    # Docker 内部 DNS：api 容器可通过 `redis` hostname 访问 Redis 容器
    r = redis_lib.Redis(host="redis", port=6379, decode_responses=True)
    r.flushdb()
    r.close()


def _http(
    method: str,
    path: str,
    token: str | None = None,
    device_id: str = "e2e-dev",
    json_body: dict | None = None,
) -> httpx.Response:
    headers = {"X-Device-Id": device_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(base_url=_BASE_URL, timeout=10.0) as client:
        return client.request(method, path, headers=headers, json=json_body)


@pytest.fixture(autouse=True)
def clean_redis() -> Generator[None, None, None]:
    """每条用例前 flush Redis。"""
    _flush_redis()
    yield
    _flush_redis()


class TestFullAuthFlow:
    """完整 M4 流程 E2E。"""

    def test_e2e_full_auth_flow(self) -> None:
        """
        1. create_parent CLI → phone / password
        2. login → parent token
        3. /me (parent) → 200 + role=parent
        4. POST /children → child_id
        5. POST /bind-tokens body:{ child_user_id } → bind_token
        6. POST /bind-tokens/{bind_token}/redeem body:{ device_id } → child token
        7. /me (child) → 200 + role=child
        8. POST /bind-tokens body:{ child_user_id } (child token) → 403
        9. POST /auth/logout (parent token) → 204
        10. 老 parent token → 401
        11. re-login → 新 parent token
        12. reset_parent_password CLI
        13. reset 前的旧 parent token → 401
        14. 新密码 login → 200
        """
        # ── 1. create_parent CLI ────────────────────────────────────────────
        note = "e2e-test parent"
        rc, stdout, stderr = _run_create_parent(note)
        assert rc == 0, f"create_parent failed: {stderr}"

        phone_match = re.search(r"phone:    +([a-z]{4})", stdout)
        password_match = re.search(r"password: +([a-z]{8})", stdout)
        assert phone_match is not None and password_match is not None
        phone = phone_match.group(1)
        password = password_match.group(1)
        assert len(phone) == 4 and len(password) == 8
        # 明文只打印一次
        assert stdout.count(password) == 1

        # ── 2. login ─────────────────────────────────────────────────────────
        login_resp = _http(
            "POST",
            "/api/v1/auth/login",
            json_body={
                "phone": phone,
                "password": password,
                "device_id": "parent-e2e",
            },
        )
        assert login_resp.status_code == 200, login_resp.text
        parent_token = login_resp.json()["token"]
        parent_account = login_resp.json()["account"]
        assert parent_account["role"] == "parent"
        assert parent_account["phone"] == phone

        # ── 3. /me (parent token) ─────────────────────────────────────────────
        me_resp = _http("GET", "/api/v1/me", token=parent_token, device_id="parent-e2e")
        assert me_resp.status_code == 200, me_resp.text
        assert me_resp.json()["role"] == "parent"

        # ── 4. POST /children ─────────────────────────────────────────────────
        child_resp = _http(
            "POST",
            "/api/v1/children",
            token=parent_token,
            device_id="parent-e2e",
            json_body={"nickname": "e2e-child", "age": 10, "gender": "unknown"},
        )
        assert child_resp.status_code == 201, child_resp.text
        child_id = child_resp.json()["id"]

        # ── 5. POST /bind-tokens body:{ child_user_id } ────────────────────────
        bind_resp = _http(
            "POST",
            "/api/v1/bind-tokens",
            token=parent_token,
            device_id="parent-e2e",
            json_body={"child_user_id": child_id},
        )
        assert bind_resp.status_code == 201, bind_resp.text
        bind_token = bind_resp.json()["bind_token"]
        assert len(bind_token) > 16  # urlsafe base64

        # ── 6. POST /bind-tokens/{bind_token}/redeem body:{ device_id } ───────
        redeem_resp = _http(
            "POST",
            f"/api/v1/bind-tokens/{bind_token}/redeem",
            json_body={"device_id": "child-e2e"},
        )
        assert redeem_resp.status_code == 200, redeem_resp.text
        child_token = redeem_resp.json()["token"]
        child_account = redeem_resp.json()["account"]
        assert child_account["role"] == "child"
        assert child_account["id"] == child_id

        # ── 7. /me (child token) ───────────────────────────────────────────────
        child_me_resp = _http(
            "GET",
            "/api/v1/me",
            token=child_token,
            device_id="child-e2e",
        )
        assert child_me_resp.status_code == 200, child_me_resp.text
        assert child_me_resp.json()["role"] == "child"

        # ── 8. child token 尝试 parent-only 端点 → 403 ───────────────────────
        child_forbidden_resp = _http(
            "POST",
            "/api/v1/bind-tokens",
            token=child_token,
            device_id="child-e2e",
            json_body={"child_user_id": child_id},
        )
        assert child_forbidden_resp.status_code == 403, (
            f"expected 403, got {child_forbidden_resp.status_code}; "
            f"may be 401 if device_changed fires first"
        )

        # ── 9. logout (parent token) → 204 ────────────────────────────────────
        logout_resp = _http(
            "POST",
            "/api/v1/auth/logout",
            token=parent_token,
            device_id="parent-e2e",
        )
        assert logout_resp.status_code == 204, logout_resp.text

        # ── 10. 老 parent token → 401 ──────────────────────────────────────────
        old_token_resp = _http(
            "GET",
            "/api/v1/me",
            token=parent_token,
            device_id="parent-e2e",
        )
        assert old_token_resp.status_code == 401

        # ── 11. re-login 拿新 parent token ─────────────────────────────────────
        login2_resp = _http(
            "POST",
            "/api/v1/auth/login",
            json_body={
                "phone": phone,
                "password": password,
                "device_id": "parent-e2e-2",
            },
        )
        assert login2_resp.status_code == 200, login2_resp.text
        new_parent_token = login2_resp.json()["token"]
        assert new_parent_token != parent_token

        # ── 12. reset_parent_password CLI ──────────────────────────────────────
        rc3, stdout3, stderr3 = _run_reset_password(phone)
        assert rc3 == 0, f"reset_password failed: {stderr3}"
        new_password_match = re.search(r"password: +([a-z]{8})", stdout3)
        assert new_password_match is not None
        new_password = new_password_match.group(1)
        assert stdout3.count(new_password) == 1

        # ── 13. reset 前的旧 parent token（在 reset 前拿的）→ 401 ──────────────
        old_after_reset_resp = _http(
            "GET",
            "/api/v1/me",
            token=new_parent_token,
            device_id="parent-e2e-2",
        )
        assert old_after_reset_resp.status_code == 401, (
            f"token should be invalidated by reset_password; got {old_after_reset_resp.status_code}"
        )

        # ── 14. 新密码 login → 200 ─────────────────────────────────────────────
        login3_resp = _http(
            "POST",
            "/api/v1/auth/login",
            json_body={
                "phone": phone,
                "password": new_password,
                "device_id": "parent-e2e-3",
            },
        )
        assert login3_resp.status_code == 200, login3_resp.text
        assert login3_resp.json()["token"]
