"""密码哈希与随机生成。argon2id + OWASP 2024 参数。"""
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ALPHABET = "abcdefghjkmnpqrstuvwxyz"  # 去 i/l/o

_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    """argon2id 哈希。返回 `$argon2id$...` 字符串，入 DB 的 password_hash 列。"""
    raise NotImplementedError


def verify_password(hashed: str, password: str) -> bool:
    """验证密码。输入 mismatch 返回 False；其它异常向上抛（记审计 / 500）。"""
    raise NotImplementedError


def generate_phone() -> str:
    """MVP 特供：4 位小写字母（字符集去 i/l/o）。"""
    raise NotImplementedError


def generate_password() -> str:
    """MVP 特供：8 位小写字母（字符集去 i/l/o）。"""
    raise NotImplementedError
