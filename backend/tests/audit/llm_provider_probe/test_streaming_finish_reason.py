"""补2: F1 流式 末 chunk 完整结构 dump + extractors.py 路径验证。

验证: extract_finish_reason / extract_reasoning_content 在当前
langchain-deepseek 版本下的取值路径是否仍有效。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from .conftest import SYSTEM_MESSAGE, USER_MESSAGE, ArtifactBuilder

pytestmark = [pytest.mark.live, pytest.mark.asyncio]


async def test_finish_reason_last_chunk(ds_config: dict) -> None:
    """ChatDeepSeek@DS 流式，dump 末 chunk 全部字段。"""
    from langchain_deepseek import ChatDeepSeek

    builder = ArtifactBuilder("L3", "ds-native", "ChatDeepSeek", "F1-finish-reason", "enabled")
    raw = builder.make_http_client()
    llm = ChatDeepSeek(
        api_key=ds_config["api_key"],
        api_base=ds_config["base_url"],
        model=ds_config["model"],
        http_async_client=raw,
        timeout=60,
        extra_body={"thinking": {"type": "enabled"}},
    )

    messages = [
        SystemMessage(content=SYSTEM_MESSAGE),
        HumanMessage(content=USER_MESSAGE),
    ]

    last_chunk_raw = None
    all_content = ""
    chunk_count = 0

    async for chunk in llm.astream(messages):
        chunk_count += 1
        last_chunk_raw = chunk.model_dump()  # 每次覆盖，循环结束时就是末 chunk
        if chunk.content:
            all_content += chunk.content

    # 末 chunk 的完整原始 model_dump
    builder.response_data = {"body": last_chunk_raw if last_chunk_raw else {}}

    # 按 extractors.py 路径取值
    ak = (last_chunk_raw or {}).get("additional_kwargs", {}) or {}
    metadata = ak.get("response_metadata", {}) or {}
    finish_reason_from_ak_meta = metadata.get("finish_reason")
    finish_reason_from_top = (last_chunk_raw or {}).get("response_metadata", {}).get("finish_reason")
    usage = (last_chunk_raw or {}).get("usage_metadata")
    rc = ak.get("reasoning_content")

    builder.set_parsed_output({
        "chunk_count": chunk_count,
        "all_content": all_content,
        "last_chunk_top_keys": list(last_chunk_raw.keys()) if last_chunk_raw else [],
        "additional_kwargs": ak,
        "response_metadata_top": (last_chunk_raw or {}).get("response_metadata"),
        "additional_kwargs.response_metadata": metadata,
        "extractors_path__finish_reason": finish_reason_from_ak_meta,
        "extractors_path__finish_reason_alt": finish_reason_from_top,
        "extractors_path__reasoning_content": rc,
        "usage_metadata": usage,
    })
    builder.save(custom_filename="F1-finish-reason-末chunk.json")

    assert all_content, "streaming content 为空"
    # 输出解析信息到 stdout 便于肉眼确认
    print("\n=== 末 chunk 分析 ===")
    print(f"chunk_count={chunk_count}")
    print(f"additional_kwargs={ak}")
    print(f"response_metadata顶层={last_chunk_raw.get('response_metadata') if last_chunk_raw else None}")
    print(f"additional_kwargs.response_metadata={metadata}")
    print(f"extract_finish_reason 路径取值={finish_reason_from_ak_meta}")
    print(f"response_metadata 顶层取值={finish_reason_from_top}")
    print(f"reasoning_content={rc}")
    print(f"usage_metadata={usage}")
