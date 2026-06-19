"""父账号密码哈希、验证与登录凭据生成工具。

算法:argon2id,采用 OWASP 2024 推荐参数(time_cost=3 / memory_cost=64MiB / parallelism=4);
verify 走常量时间比较,避免计时侧信道泄露哈希匹配信息。
"""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_ALPHABET = "abcdefghjkmnpqrstuvwxyz"  # 去 i / l / o,降低肉眼误读概率

_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    """对明文密码做 argon2id 哈希。

    Args:
        password: 明文密码。

    Returns:
        str: `$argon2id$...` 编码的哈希字符串,可直接写入 DB 的 `password_hash` 列。
    """
    return _HASHER.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """以常量时间比对验证密码。

    Args:
        hashed: DB 中存储的 argon2id 哈希字符串。
        password: 用户提交的明文密码。

    Returns:
        bool: 匹配返回 True;不匹配返回 False。

    Raises:
        argon2.exceptions.VerifyMismatchError 之外的 argon2 异常(如 InvalidHashError):
            视为数据损坏,直接向上抛,交由上游记审计或返回 500。
    """
    try:
        return _HASHER.verify(hashed, password)
    except VerifyMismatchError:
        return False
    # 其它 argon2 异常(如 InvalidHashError)属数据损坏,向上抛


def generate_phone() -> str:
    """生成 4 位小写字母临时 phone 标识。

    字符集去 i / l / o,降低肉眼误读概率;供父账号初始化脚本使用。

    Returns:
        str: 4 位小写字母字符串。
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(4))


def generate_password() -> str:
    """生成 8 位小写字母临时密码。

    字符集去 i / l / o,降低肉眼误读概率;供父账号初始化脚本使用。

    Returns:
        str: 8 位小写字母字符串。
    """
    return "".join(secrets.choice(_ALPHABET) for _ in range(8))
