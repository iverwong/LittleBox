"""M6 收尾补丁 · 探针 D · ChatDeepSeek 双端 reasoning_content 真机验证。

用法: python -m backend.scripts.verify.chatdeepseek_dual_provider_probe
依赖: settings 已加载 .env，含 DeepSeek 原生 base_url + 百炼 base_url + 双端 api_key + model
输出: stdout JSON {"d1": {...}, "d2": {...}, "verdict": "pass|fail"}
闸门: d1.reasoning_chunks > 0 AND d2.reasoning_chunks > 0 → pass
"""

import asyncio
import json

from langchain_deepseek import ChatDeepSeek

from app.config import settings

PROBE_PROMPT = "3 + 5 等于多少？请仔细思考后回答。"


async def probe(label: str, base_url: str, api_key: str, model: str) -> dict:
    """对单个端点发起一次 ChatDeepSeek 流式调用，统计 reasoning_chunks。"""
    llm = ChatDeepSeek(
        api_key=api_key,
        base_url=base_url,
        model=model,
        extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
    )
    reasoning_chunks, content_chunks = 0, 0
    reasoning_text, content_text = "", ""
    async for chunk in llm.astream(PROBE_PROMPT):
        r = (chunk.additional_kwargs or {}).get("reasoning_content")
        if r:
            reasoning_chunks += 1
            reasoning_text += r
        if chunk.content:
            content_chunks += 1
            content_text += chunk.content
    return {
        "label": label,
        "base_url": base_url,
        "model": model,
        "reasoning_chunks": reasoning_chunks,
        "content_chunks": content_chunks,
        "reasoning_text_len": len(reasoning_text),
        "content_text_len": len(content_text),
        "reasoning_text_head": reasoning_text[:80],
        "content_text_head": content_text[:80],
    }


async def main() -> None:
    d1 = await probe(
        "deepseek-native",
        settings.deepseek_base_url,
        settings.deepseek_api_key.get_secret_value(),
        settings.deepseek_model,
    )
    d2 = await probe(
        "bailian-compat",
        settings.bailian_base_url,
        settings.bailian_api_key.get_secret_value(),
        settings.bailian_model,
    )
    verdict = "pass" if d1["reasoning_chunks"] > 0 and d2["reasoning_chunks"] > 0 else "fail"
    result = {"d1": d1, "d2": d2, "verdict": verdict}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
