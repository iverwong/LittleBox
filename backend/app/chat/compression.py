"""M8 上下文压缩 prompt 构建。

设计：固定 2 条消息（System + Human），history 走 XML 序列化嵌入 Human content，
避免 chat template 进入"续写 assistant"分支。

M6-patch3 scheme R：commit② 写 LLM usage 真值快照，
阈值命中翻 needs_compression 标志，下一轮 user 到达时阻塞压缩。
"""

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from .history_xml import extract_wrapped_output, serialize_history_to_xml
from .prompts import COMPRESSION_PROMPT_STUB

logger = __import__("logging").getLogger(__name__)

CONTEXT_COMPRESS_THRESHOLD_TOKENS = 500_000  # V4 1M 上下文的 50%

_OUTPUT_CONTRACT = (
    "\n\n请只输出 <summary>…</summary> 包裹的内容，不要其他文字。"
)


def build_compression_prompt(history: list[BaseMessage]) -> list[BaseMessage]:
    """构建压缩调用的 messages。返回 list 长度恒为 2。

    - SystemMessage: 角色定位
    - HumanMessage: 任务说明 + <history>…</history> 序列化 + 末尾输出契约
    """
    history_xml = serialize_history_to_xml(history, include_system=False)
    human_content = f"{COMPRESSION_PROMPT_STUB}\n\n{history_xml}{_OUTPUT_CONTRACT}"
    return [
        SystemMessage(content="你是对话压缩助手。"),
        HumanMessage(content=human_content),
    ]


def extract_compression_summary(raw_output: str) -> str:
    """从压缩 LLM 输出提取 <summary>…</summary>；失败时兜底使用 raw_output.strip()。

    兜底失败不抛异常，由调用方决定是否记 warning。建议调用方：
        summary = extract_compression_summary(raw)
        if extract_wrapped_output(raw, "summary") is None:
            logger.warning("compression.summary_tag_missing", extra={"raw_len": len(raw)})
    """
    extracted = extract_wrapped_output(raw_output, "summary")
    if extracted is not None:
        return extracted
    return raw_output.strip()
