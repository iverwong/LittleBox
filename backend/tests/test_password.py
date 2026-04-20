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
