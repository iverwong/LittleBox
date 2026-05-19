"""补4: 多轮 agentic loop reasoning_content 回传验证。

三层 (L1/L2/L3) 各跑一次 agentic loop:
  1. 用户问"明天杭州天气怎么样"
  2. 模型返回 tool_call (get_date)
  3. 回传 tool result, 再请求
  4. 关键检查: 第二轮请求 body 中的 assistant message 是否含 reasoning_content

工具: get_date (无参) + get_weather (location, date)
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest

from .conftest import ArtifactBuilder

pytestmark = [pytest.mark.live, pytest.mark.asyncio]

# ── 探针专用工具 ──────────────────────────────────────
TOOL_GET_DATE = {
    "type": "function",
    "function": {
        "name": "get_date",
        "description": "获取今天的日期，返回 YYYY-MM-DD 格式",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
TOOL_GET_WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询某个城市某天的天气",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string"},
                "date": {"type": "string"},
            },
            "required": ["location", "date"],
        },
    },
}
MULTI_TOOLS = [TOOL_GET_DATE, TOOL_GET_WEATHER]

USER_MSG = "明天杭州天气怎么样"


def _check_reasoning_in_messages(
    messages: list[dict],
    round_num: int,
    label: str,
) -> bool:
    """检查 messages 中所有 assistant 角色消息是否含 reasoning_content。"""
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            rc = msg.get("reasoning_content")
            if not rc:
                print(f"  [{label}] Round {round_num}, msg[{i}]: assistant 缺少 reasoning_content!")
                return False
            print(f"  [{label}] Round {round_num}, msg[{i}]: reasoning_content 存在 ({len(rc)} chars)")
    return True


# ── L1: httpx 原生手撸 agentic loop ────────────────────
async def test_f8a_l1_multiround(ds_config: dict) -> None:
    """L1 httpx 原生多轮: 检查每轮 assistant message 的 reasoning_content。"""
    builder = ArtifactBuilder("L1", "ds-native", "httpx", "F8a-multiround", "enabled")
    client = builder.make_http_client()
    headers = {"Authorization": f"Bearer {ds_config['api_key']}"}

    url = f"{ds_config['base_url']}/chat/completions"
    all_messages: list[dict] = [{"role": "user", "content": USER_MSG}]
    all_rounds: list[dict] = []
    max_rounds = 4

    for rnd in range(max_rounds):
        body = {
            "model": ds_config["model"],
            "messages": all_messages,
            "tools": MULTI_TOOLS,
            "tool_choice": "auto",
            "stream": False,
            "thinking": {"type": "enabled", "reasoning_effort": "high"},
        }

        try:
            resp = await client.post(url, json=body, headers=headers)
            data = resp.json()
        except Exception as exc:
            all_rounds.append({"error": repr(exc)})
            break

        choice = data["choices"][0]
        msg = choice["message"]
        all_rounds.append({
            "round": rnd,
            "request_messages_before_assistant_append": len(all_messages),
            "response_msg": msg,
            "finish_reason": choice["finish_reason"],
        })

        # 检查当前 messages 中已有的 assistant 有没有 reasoning_content
        has_rc = _check_reasoning_in_messages(all_messages, rnd, "L1")

        # 把模型回复追加到消息数组
        all_messages.append(msg)

        # 处理 tool_calls
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            break  # 模型不再调工具，结束

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])
            if fn_name == "get_date":
                result = "2026-05-20"
            elif fn_name == "get_weather":
                result = json.dumps({"temperature": 28, "condition": "晴"})
            else:
                result = "unknown"

            all_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
    else:
        # max_rounds 达到但模型仍在调工具
        all_rounds.append({"note": "max_rounds reached"})

    builder.set_parsed_output({
        "rounds": all_rounds,
        "final_messages_count": len(all_messages),
        "has_reasoning_in_second_round": _check_reasoning_in_messages(
            [m for m in all_messages if m.get("role") == "assistant"], 0, "L1-final"
        ),
    })
    builder.save()

    # 验证第二轮 messages 中 assistant 是否有 reasoning_content
    assistant_msgs = [m for m in all_messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    # 第一个 assistant message（第一轮回复）应该有 reasoning_content
    # 关键是它被作为输入传给第二轮
    assert assistant_msgs[0].get("reasoning_content"), "第一轮 assistant message 缺少 reasoning_content!"


# ── L2: OpenAI SDK 手撸 agentic loop ───────────────────
async def test_f8b_l2_multiround(ds_config: dict) -> None:
    """L2 OpenAI SDK 多轮: 验证 reasoning_content 回传。"""
    from openai import AsyncOpenAI

    builder = ArtifactBuilder("L2", "ds-native", "AsyncOpenAI", "F8b-multiround", "enabled")
    raw = builder.make_http_client()
    client = AsyncOpenAI(api_key=ds_config["api_key"], base_url=ds_config["base_url"], http_client=raw)

    all_messages: list[dict] = [{"role": "user", "content": USER_MSG}]
    all_rounds: list[dict] = []

    for rnd in range(4):
        try:
            response = await client.chat.completions.create(
                model=ds_config["model"],
                messages=all_messages,
                tools=MULTI_TOOLS,
                tool_choice="auto",
                extra_body={"thinking": {"type": "enabled", "reasoning_effort": "high"}},
            )
        except Exception as exc:
            all_rounds.append({"error": repr(exc)})
            break

        data = response.model_dump()
        choice = data["choices"][0]
        msg = choice["message"]
        all_rounds.append({
            "round": rnd,
            "finish_reason": choice["finish_reason"],
            "response_msg_keys": list(msg.keys()),
        })

        has_rc = _check_reasoning_in_messages(all_messages, rnd, "L2")

        all_messages.append(msg)

        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            break

        for tc in tool_calls:
            fn_name = tc["function"]["name"]
            fn_args = json.loads(tc["function"]["arguments"])
            if fn_name == "get_date":
                result = "2026-05-20"
            elif fn_name == "get_weather":
                result = json.dumps({"temperature": 28, "condition": "晴"})
            else:
                result = "unknown"

            all_messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    builder.set_parsed_output({
        "rounds": all_rounds,
        "final_messages_count": len(all_messages),
    })
    builder.save()

    assistant_msgs = [m for m in all_messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) >= 1
    assert assistant_msgs[0].get("reasoning_content"), "第一轮 assistant 缺少 reasoning_content!"


# ── L3: LangChain ChatDeepSeek 手写 agentic loop ───────
async def test_f8c_l3_multiround(ds_config: dict) -> None:
    """L3 LangChain 多轮: 验证 reasoning_content 是否在第二轮请求中回传。

    LangChain 的 AIMessage 序列化路径在 langchain-openai 和 langchain-deepseek
    中可能不同，需要验证 additional_kwargs.reasoning_content 是否能写入 HTTP body。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_deepseek import ChatDeepSeek

    builder = ArtifactBuilder("L3", "ds-native", "ChatDeepSeek", "F8c-multiround", "enabled")
    raw = builder.make_http_client()
    llm = ChatDeepSeek(
        api_key=ds_config["api_key"],
        api_base=ds_config["base_url"],
        model=ds_config["model"],
        http_async_client=raw,
        timeout=60,
        extra_body={"thinking": {"type": "enabled", "reasoning_effort": "high"}},
    )

    bound = llm.bind_tools(MULTI_TOOLS, tool_choice="auto")
    messages = [HumanMessage(content=USER_MSG)]
    all_rounds: list[dict] = []

    for rnd in range(4):
        try:
            response = await bound.ainvoke(messages)
        except Exception as exc:
            all_rounds.append({"error": repr(exc)})
            break

        all_rounds.append({
            "round": rnd,
            "finish_reason": response.response_metadata.get("finish_reason"),
            "tool_calls_count": len(response.tool_calls) if hasattr(response, "tool_calls") and response.tool_calls else 0,
            "response_additional_kwargs_keys": list(response.additional_kwargs.keys()),
            "reasoning_content_present": bool(response.additional_kwargs.get("reasoning_content")),
        })

        if not hasattr(response, "tool_calls") or not response.tool_calls:
            break

        # 检查现有 messages 中 AIMessage 的 additional_kwargs
        for i, m in enumerate(messages):
            if isinstance(m, AIMessage):
                rc = m.additional_kwargs.get("reasoning_content")
                all_rounds[-1][f"msg[{i}]_has_reasoning_content"] = bool(rc)

        # 将 AIMessage 加入消息列表 → 下一轮序列化时看是否带 reasoning_content
        messages.append(response)

        for tc in response.tool_calls:
            fn_name = tc["name"]
            if fn_name == "get_date":
                result = "2026-05-20"
            elif fn_name == "get_weather":
                result = '{"temperature": 28, "condition": "晴"}'
            else:
                result = "unknown"
            messages.append(ToolMessage(
                content=result,
                tool_call_id=tc["id"],
            ))

    # 关键验证: 对比第二轮请求的 HTTP body 中 assistant message 是否含 reasoning_content
    reasoning_in_http = False
    if len(builder.req_entries) >= 2:
        body2 = builder.req_entries[1].get("body", {})
        msgs2 = body2.get("messages", [])
        for m in msgs2:
            if m.get("role") == "assistant":
                if m.get("reasoning_content"):
                    reasoning_in_http = True
                    break

    builder.set_parsed_output({
        "rounds": all_rounds,
        "final_messages_count": len(messages),
        "reasoning_content_in_second_request_http_body": reasoning_in_http,
        "second_request_messages": builder.req_entries[1].get("body", {}).get("messages", []) if len(builder.req_entries) >= 2 else [],
        "note": "如果 reasoning_content_in_second_request_http_body=False，说明 LangChain 序列化丢失了 reasoning_content",
    })
    builder.save()

    assert len(builder.req_entries) >= 2, "未发出第二轮请求（可能第一轮就结束了）"
    print(f"\n=== L3 多轮验证 ===")
    print(f"reasoning_content 在第二轮 HTTP body 中: {reasoning_in_http}")
    if reasoning_in_http:
        print("✅ LangChain ChatDeepSeek 正确回传了 reasoning_content")
    else:
        print("❌ LangChain ChatDeepSeek 未能回传 reasoning_content（序列化丢失）")
