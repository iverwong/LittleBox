"""Given 密码明文 / When 走 hash+verify / Then 返回正确布尔值。
覆盖范围：hash 格式、正/反向验证、随机生成器的字符集与长度。
"""
import pytest

from app.auth.password import (
    generate_password,
    generate_phone,
    hash_password,
    verify_password,
)

_ALPHABET = set("abcdefghjkmnpqrstuvwxyz")

# ---- A6 · verify_password 非法 hash 异常上抛 ----

try:
    from argon2.exceptions import InvalidHash, VerificationError
except ImportError:
    # argon2-cffi 历史版本分别导出 InvalidHashError
    from argon2.exceptions import InvalidHashError as InvalidHash
    from argon2.exceptions import VerificationError


class TestHashAndVerify:
    def test_hash_returns_argon2id_string(self) -> None:
        """Given 任意明文 When hash_password Then 返回以 $argon2id$ 起头的字符串"""
        h = hash_password("abcdefgh")
        assert h.startswith("$argon2id$")
        assert len(h) < 255

    def test_verify_correct_password_returns_true(self) -> None:
        h = hash_password("correctpw")
        assert verify_password(h, "correctpw") is True

    def test_verify_wrong_password_returns_false(self) -> None:
        h = hash_password("correctpw")
        assert verify_password(h, "wrongpw") is False


class TestVerifyPasswordInvalidHash:
    """A6 · verify_password 非 VerifyMismatchError 的异常上抛（fail-fast 契约）。

    注意：截断的 argon2 字符串实际抛出 VerificationError（非 InvalidHash），
    因为 argon2 库识别出格式头但解码失败。已根据实测修正。
    """

    def test_verify_password_raises_invalid_hash_on_non_argon2_string(self) -> None:
        """非 argon2 格式字符串 → InvalidHash。"""
        with pytest.raises(InvalidHash):
            verify_password("not-a-hash", "any")

    def test_verify_password_raises_verification_error_on_truncated_hash(self) -> None:
        """有效 argon2 字符串被截断 → VerificationError（argon2 库能解析头但解码失败）。"""
        full = hash_password("x")
        with pytest.raises(VerificationError):
            verify_password(full[:30], "any")

    def test_verify_password_raises_invalid_hash_on_empty_string(self) -> None:
        """空字符串 → InvalidHash。"""
        with pytest.raises(InvalidHash):
            verify_password("", "any")


class TestGenerators:
    @pytest.mark.parametrize("_", range(20))
    def test_phone_is_4_lowercase_letters(self, _: int) -> None:
        p = generate_phone()
        assert len(p) == 4
        assert set(p) <= _ALPHABET

    @pytest.mark.parametrize("_", range(20))
    def test_password_is_8_lowercase_letters(self, _: int) -> None:
        p = generate_password()
        assert len(p) == 8
        assert set(p) <= _ALPHABET
