"""M8 上下文压缩 prompt 构建。

设计：固定 2 条消息（System + Human），history 走 XML 序列化嵌入 Human content，
避免 chat template 进入"续写 assistant"分支。

M6-patch3 scheme R：commit② 写 LLM usage 真值快照，
阈值命中翻 needs_compression 标志，下一轮 user 到达时阻塞压缩。
"""

import logging
from typing import Sequence

from backend.app.domain.chat.prompts import build_compression_prompt
from langchain_core.messages import BaseMessage, HumanMessage

from app.core.history_xml import extract_wrapped_output, serialize_history_to_xml
from app.domain.chat.models import Message

logger = logging.getLogger(__name__)

CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000  # V4 1M 上下文的 50%

# 压缩后保留最近 N 对完整 (h,a) 消息不压,直接进 history。
# 切 N=3:既保证对话有"近因"接住,又给新摘要留足压缩空间。
COMPRESSION_KEEP_RECENT_PAIRS = 3


def split_for_compression(
    actives: Sequence[Message],
    *,
    keep_recent_pairs: int = COMPRESSION_KEEP_RECENT_PAIRS,
) -> tuple[list[Message], list[Message]]:
    """把 active 消息切分为 (to_compress, to_keep)。

    切分规则:
    - 末尾 `keep_recent_pairs * 2` 条进入 to_keep(原状 active,直接进 history)
    - 其余进入 to_compress(待送 LLM 压成一段 summary)
    - 若总长 ≤ `keep_recent_pairs * 2`,全部进 to_compress,to_keep 为空
      ——已到这一步说明已超 token 阈值,即便轮数少也必须压,不留原会话。

    注:按 actives 的传入顺序切(调用方应按 created_at ASC 传入),末尾视为"最近"。

    Args:
        actives: 按 created_at ASC 的 active 消息(已排除本轮 human)
        keep_recent_pairs: 保留最近的对数(每对 = 1 human + 1 ai = 2 条)

    Returns:
        (to_compress, to_keep)
    """
    keep_n = keep_recent_pairs * 2
    if len(actives) <= keep_n:
        return list(actives), []
    return list(actives[:-keep_n]), list(actives[-keep_n:])


def build_compression_messages(history: list[BaseMessage]) -> list[BaseMessage]:
    """构建压缩调用的 messages。返回 list 长度恒为 2。

    - SystemMessage: 角色定位及输入输出说明
    - HumanMessage: 纯 <history>…</history> 序列化
    """
    history_xml = serialize_history_to_xml(history, include_system=False)
    return [
        build_compression_prompt(),
        HumanMessage(content=history_xml),
    ]


def extract_compression_summary(raw_output: str) -> str:
    """从压缩 LLM 输出提取 <summary>…</summary>；失败时兜底使用 raw_output.strip() 并记 warning。"""
    extracted = extract_wrapped_output(raw_output, "summary")
    if extracted is not None:
        return extracted
    logger.warning("compression.summary_tag_missing", extra={"raw_len": len(raw_output)})
    return raw_output.strip()
