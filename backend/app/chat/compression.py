"""上下文压缩辅助：阈值常量 + 压缩 prompt。

M6-patch3 scheme R：commit② 写 LLM usage 真值快照，
阈值命中翻 needs_compression 标志，下一轮 user 到达时阻塞压缩。
"""

import logging

from langchain_core.messages import BaseMessage, SystemMessage

logger = logging.getLogger(__name__)

CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000  # V4 1M 上下文的 50%

# 压缩 prompt 占位文案；TODO(prompts-content): 专人审核后填充正式摘要指令
COMPRESSION_PROMPT_STUB = (
    "请将以下对话历史压缩为简洁的客观摘要，"
    "保留事件、决定、待办与重要细节；"
    "不带情绪标签、不做风险评估、不做安全判断。"
)


def build_compression_prompt(history: list[BaseMessage]) -> list[BaseMessage]:
    """构建压缩 LLM 的 prompt。

    Args:
        history: 待压缩的 active 消息列表

    Returns:
        可直接传入 llm.ainvoke() 的消息列表
    """
    messages = [SystemMessage(content=COMPRESSION_PROMPT_STUB), *history]
    return messages
