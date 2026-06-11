"""LangChain message → XML 序列化与输出提取工具。

M8 上下文压缩、M9 危机 / 红线干预 LLM 调用、M10 audit history XML 包装共用本模块。
将多轮 history 包装为 XML 字符串而非真实 HumanMessage/AIMessage 序列，
避免 chat template 把待压缩的 assistant 末帧视为 generation prefix 触发续写。

设计约束：
- 序列化输出禁止使用 `Human:` / `AI:` / `[AI]` 等与 chat template 角色标记同形的文本前缀
- content 必须 XML 转义
- 配套 extract_wrapped_output 用于从 LLM 输出提取契约 tag 内容
"""

from __future__ import annotations

import re
from typing import Final

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

_TURN_TEMPLATE: Final = '<turn idx="{idx}" role="{role}">{content}</turn>'


def escape_xml_text(text: str) -> str:
    """XML 文本节点转义（& < > → 实体）。属性引号未处理，调用方勿把用户输入塞进属性值。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serialize_history_to_xml(
    history: list[BaseMessage],
    *,
    include_system: bool = False,
) -> str:
    """将 message 序列化为 <history><turn idx="N" role="user|assistant|system">…</turn></history>。

    轮次编号规则（按 user/assistant 配对）：
    - 遇 HumanMessage → 开启新 idx (从 1 起)，role="user"
    - 紧随其后的 AIMessage → 复用当前 idx，role="assistant"
    - 多条 AIMessage 连续 → 复用同一 idx
    - 末尾孤立 HumanMessage → 单独占据下一个 idx，无 assistant 对应
    - SystemMessage（include_system=True 时）→ idx="sys"，role="system"，单独成段
    - 空 history → 返回 "<history></history>"

    Args:
        history: LangChain message 列表
        include_system: 是否包含 SystemMessage。默认 False；调用方通常已将 system 拆出独立槽。

    Returns:
        XML 字符串（单行，无缩进；调试需要时调用方自行 prettify）
    """
    turns: list[str] = []
    current_idx = 0

    for msg in history:
        if isinstance(msg, SystemMessage):
            if not include_system:
                continue
            turns.append(
                _TURN_TEMPLATE.format(
                    idx="sys", role="system", content=escape_xml_text(str(msg.content))
                )
            )
            continue

        if isinstance(msg, HumanMessage):
            current_idx += 1
            turns.append(
                _TURN_TEMPLATE.format(
                    idx=current_idx,
                    role="user",
                    content=escape_xml_text(str(msg.content)),
                )
            )
            continue

        if isinstance(msg, AIMessage):
            if current_idx == 0:
                # 防御：history 以 AIMessage 开头（不应发生但兜底）
                current_idx = 1
            turns.append(
                _TURN_TEMPLATE.format(
                    idx=current_idx,
                    role="assistant",
                    content=escape_xml_text(str(msg.content)),
                )
            )
            continue

        # 未知 message 类型：跳过（调用方监控决定是否补类型分支）
        continue

    return f"<history>{''.join(turns)}</history>"


_WRAPPED_OUTPUT_PATTERNS: dict[str, re.Pattern[str]] = {}


def extract_wrapped_output(raw: str, tag: str) -> str | None:
    """从 LLM 输出提取 <tag>...</tag> 内容。

    - 容忍前后空白、markdown code fence
    - 多次出现取第一段
    - 找不到返回 None（调用方决定兜底策略）
    - tag 必须为简单标识符（字母 / 下划线）；不做 tag 输入校验，调用方传字面常量
    """
    pattern = _WRAPPED_OUTPUT_PATTERNS.get(tag)
    if pattern is None:
        pattern = re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL)
        _WRAPPED_OUTPUT_PATTERNS[tag] = pattern
    m = pattern.search(raw)
    if m is None:
        return None
    return m.group(1).strip()
