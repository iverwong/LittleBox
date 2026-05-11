"""上下文 token 累加器辅助。

M6-patch3 范围：仅提供 estimate_tokens 单函数 + 阈值常量。
累加器写入 / 阈值触发判定都在 me.py 主对话事务内联完成。
M8 升级：内联 log.warning → arq_pool.enqueue_job("compress_session", sid)；
本文件可保持不变或扩展。
"""

CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000  # V4 1M 上下文的 50%


def estimate_tokens(content: str) -> int:
    """中文 1 char = 1 token；ASCII 4 char = 1 token。M8 可换 tiktoken。"""
    cjk = sum(1 for c in content if "一" <= c <= "鿿")
    ascii_chars = len(content) - cjk
    return cjk + (ascii_chars + 3) // 4
