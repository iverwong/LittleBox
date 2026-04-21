"""Step 8 CLI scripts 冒烟测试：create_parent + reset_parent_password。"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# backend 根目录（tests 的父级）
_BACKEND_ROOT = Path(__file__).parent.parent


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
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr

    def test_create_parent_success(self) -> None:
        """create_parent --note '测试' → 0 + stdout 含 phone / password / user_id / note。"""
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
            env={**os.environ},
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
            env={**os.environ},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr

    def test_reset_password_unknown_phone_exits_nonzero(self) -> None:
        """--phone 不存在 → 非 0 退出码 + stderr 含错误信息。"""
        returncode, stdout, stderr = self._run_reset_password("zzzz")
        assert returncode != 0, f"expected non-zero exit, got {returncode}"
        assert "ERROR" in stderr or "no active parent" in stderr

    def test_reset_password_success_flow(self) -> None:
        """完整流程：create_parent → 用其 phone reset_password → 验证输出。"""
        # 1. 创建 parent
        returncode, stdout, stderr = self._run_create_parent("reset 测试")
        assert returncode == 0, f"create_parent failed: {stderr}"
        phone_match = re.search(r"phone:    +([a-z]{4})", stdout)
        assert phone_match, f"phone format unexpected: {stdout}"
        phone = phone_match.group(1)

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
