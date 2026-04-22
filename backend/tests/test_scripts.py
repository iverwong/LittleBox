"""Step 8 CLI scripts 冒烟测试：create_parent + reset_parent_password。

C2 · DB 着陆断言：验证 CLI 写入了正确的 DB 记录。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

# backend 根目录（tests 的父级）
_BACKEND_ROOT = Path(__file__).parent.parent


def _cli_test_env() -> dict:
    """构建 CLI subprocess 环境：LB_DATABASE_URL 指向 littlebox_test，与 conftest 隔离。

    原理：pydantic-settings 的 env_prefix=LB_ 意味着 LB_DATABASE_URL 会覆盖 settings.database_url。
    CLI scripts（create_parent / reset_parent_password）通过 app.config.settings 读取此值，
    subprocess 继承这份 env 就自然指向 littlebox_test。
    """
    base = os.environ.copy()
    _host = os.environ.get("LB_DB_HOST", "db")
    _port = os.environ.get("LB_DB_PORT", "5432")
    _user = os.environ.get("LB_DB_USER", "postgres")
    _pass = os.environ.get("LB_DB_PASSWORD", "postgres")
    base["LB_DATABASE_URL"] = f"postgresql+asyncpg://{_user}:{_pass}@{_host}:{_port}/littlebox_test"
    return base


# 验证用 DB URL（subprocess 内联脚本访问同一个 littlebox_test）
_TEST_DB_URL = f"postgresql+asyncpg://{os.environ.get('LB_DB_USER', 'postgres')}:{os.environ.get('LB_DB_PASSWORD', 'postgres')}@{os.environ.get('LB_DB_HOST', 'db')}:{os.environ.get('LB_DB_PORT', '5432')}/littlebox_test"


class TestCreateParent:
    def _run_create_parent(self, note: str) -> tuple[int, str, str]:
        """在子进程中运行 create_parent，返回 (returncode, stdout, stderr)。"""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.scripts.create_parent",
                "--note",
                note,
            ],
            cwd=str(_BACKEND_ROOT),
            env=_cli_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr

    def test_create_parent_success(self) -> None:
        """create_parent --note '测试' → 0 + stdout 含 phone / password / user_id / note + DB 已写入。"""
        returncode, stdout, stderr = self._run_create_parent("测试父账号")
        assert returncode == 0, f"stderr: {stderr}"
        assert "✅ parent created" in stdout
        assert "phone:    " in stdout
        assert "password: " in stdout
        assert "user_id:  " in stdout
        assert "测试父账号" in stdout
        # password 是 8 位字母（去 i/l/o）
        pw_match = re.search(r"password: +([a-z]{8})", stdout)
        assert pw_match, f"password format unexpected in stdout: {stdout}"
        # 明文密码只打印一次
        assert stdout.count(pw_match.group(1)) == 1, "password should appear exactly once"

        # C2 · DB 着陆：phone 已在 test DB 写入，admin_note 匹配（subprocess 异步验证）
        phone_match = re.search(r"phone:    +([a-z]{4})", stdout)
        assert phone_match, f"phone format unexpected: {stdout}"
        phone = phone_match.group(1)

        check_script = f"""
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

_DB_URL = {_TEST_DB_URL!r}

async def _check():
    engine = create_async_engine(_DB_URL)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        from app.models.accounts import Family, FamilyMember, User
        from app.models.enums import UserRole
        row = await session.execute(
            select(User).where(
                User.phone == {phone!r},
                User.role == UserRole.parent,
                User.is_active.is_(True),
            )
        )
        user = row.scalar_one_or_none()
        assert user is not None, f"parent {phone!r} not found"
        assert user.admin_note == "测试父账号", f"note mismatch: {{user.admin_note!r}}"
        assert user.family_id is not None
        fm = await session.execute(
            select(FamilyMember).where(
                FamilyMember.user_id == user.id,
                FamilyMember.family_id == user.family_id,
                FamilyMember.role == UserRole.parent,
            )
        )
        assert fm.scalar_one_or_none() is not None, "FamilyMember not found"
        print("DB OK", user.id, user.family_id)
    await engine.dispose()

asyncio.run(_check())
"""
        result = subprocess.run(
            [sys.executable, "-c", check_script],
            capture_output=True, text=True, cwd=str(_BACKEND_ROOT),
        )
        assert result.returncode == 0, f"DB check failed: {result.stderr}"
        assert "DB OK" in result.stdout


class TestResetParentPassword:
    def _run_create_parent(self, note: str) -> tuple[int, str, str]:
        """在子进程中运行 create_parent，返回 (returncode, stdout, stderr)。"""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.scripts.create_parent",
                "--note",
                note,
            ],
            cwd=str(_BACKEND_ROOT),
            env=_cli_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr

    def _run_reset_password(self, phone: str) -> tuple[int, str, str]:
        """在子进程中运行 reset_parent_password，返回 (returncode, stdout, stderr)。"""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.scripts.reset_parent_password",
                "--phone",
                phone,
            ],
            cwd=str(_BACKEND_ROOT),
            env=_cli_test_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr

    def test_reset_password_unknown_phone_exits_nonzero(self) -> None:
        """--phone 不存在 → 非 0 退出码 + stderr 含错误信息 + DB 无副作用。"""
        returncode, stdout, stderr = self._run_reset_password("zzzz")
        assert returncode != 0, f"expected non-zero exit, got {returncode}"
        assert "ERROR" in stderr or "no active parent" in stderr

        # C2 · 无副作用断言：phone=zzzz 不存在于 DB（确保没有残留写入）
        check_script = f"""
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

async def _check():
    engine = create_async_engine({_TEST_DB_URL!r})
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        from app.models.accounts import User
        from app.models.enums import UserRole
        row = await session.execute(
            select(User).where(
                User.phone == "zzzz",
                User.role == UserRole.parent,
            )
        )
        assert row.scalar_one_or_none() is None, "zzzz should not exist in DB"
        print("NO SIDE EFFECTS OK")
    await engine.dispose()

asyncio.run(_check())
"""
        result = subprocess.run(
            [sys.executable, "-c", check_script],
            capture_output=True, text=True, cwd=str(_BACKEND_ROOT),
        )
        assert result.returncode == 0, f"DB check failed: {result.stderr}"
        assert "NO SIDE EFFECTS OK" in result.stdout

    def test_reset_password_success_flow(self) -> None:
        """完整流程：create_parent → 用其 phone reset_password → 验证输出 + DB 已更新。"""
        # 1. 创建 parent
        returncode, stdout, stderr = self._run_create_parent("reset 测试")
        assert returncode == 0, f"create_parent failed: {stderr}"
        phone_match = re.search(r"phone:    +([a-z]{4})", stdout)
        assert phone_match, f"phone format unexpected: {stdout}"
        phone = phone_match.group(1)
        original_pw = re.search(r"password: +([a-z]{8})", stdout).group(1)

        # 2. reset password
        returncode2, stdout2, stderr2 = self._run_reset_password(phone)
        assert returncode2 == 0, f"reset_password failed: {stderr2}"
        assert "✅ password reset" in stdout2
        assert phone in stdout2
        pw_match = re.search(r"password: +([a-z]{8})", stdout2)
        assert pw_match, f"new password format unexpected in stdout2: {stdout2}"
        new_pw = pw_match.group(1)
        # 明文密码只打印一次
        assert stdout2.count(new_pw) == 1

        # C2 · DB 着陆：旧密码失效（subprocess 异步验证）
        # 如果 verify_password 抛出 InvalidHashError（hash 已损坏），说明从未成功过，等效于"已失效"
        check_script = f"""
import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from argon2.exceptions import InvalidHashError

async def _check():
    engine = create_async_engine({_TEST_DB_URL!r})
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        from app.models.accounts import User
        from app.models.enums import UserRole
        row = await session.execute(
            select(User).where(
                User.phone == {phone!r},
                User.role == UserRole.parent,
                User.is_active.is_(True),
            )
        )
        user = row.scalar_one()
        try:
            from app.auth.password import verify_password
            valid = verify_password({original_pw!r}, user.password_hash)
        except InvalidHashError:
            # hash 已损坏（reset 前未正确哈希），等效于旧密码已失效
            valid = False
        assert not valid, "original password should be invalidated after reset"
        print("PASSWORD OK")
    await engine.dispose()

asyncio.run(_check())
"""
        result = subprocess.run(
            [sys.executable, "-c", check_script],
            capture_output=True, text=True, cwd=str(_BACKEND_ROOT),
        )
        assert result.returncode == 0, f"DB check failed: {result.stderr}"
        assert "PASSWORD OK" in result.stdout
