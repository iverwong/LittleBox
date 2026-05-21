"""LLM provider probe: 共享基础设施。

包含：
- ArtifactBuilder: 请求/响应捕获 + artifact JSON 写入
- provider fixtures: ds_config / bl_config（从 Settings 读真实 key）
- thinking_mode parametrize fixture
- 共享工具定义 + 消息模板
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings

# ── 路径 ──────────────────────────────────────────────
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

# ── 端点常量 ──────────────────────────────────────────
DS_BASE_URL = "https://api.deepseek.com/v1"
BL_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# ── 共享工具定义（F2/F3/F4 共用） ──────────────────────
TOOL_APPEND_NOTE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "append_note",
        "description": "向 session_notes 尾部追加一条结构化笔记",
        "parameters": {
            "type": "object",
            "properties": {"note": {"type": "string"}},
            "required": ["note"],
            "additionalProperties": False,
        },
    },
}

TOOL_REPLACE_IN_NOTES: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "replace_in_notes",
        "description": "在 session_notes 中查找替换",
        "parameters": {
            "type": "object",
            "properties": {
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["old", "new"],
            "additionalProperties": False,
        },
    },
}

TOOL_AUDIT_OUTPUT: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "audit_output",
        "description": "输出本轮审查结论",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "warn", "fail"]},
                "reason": {"type": "string"},
            },
            "required": ["verdict", "reason"],
            "additionalProperties": False,
        },
    },
}

SHARED_TOOLS = [TOOL_APPEND_NOTE, TOOL_REPLACE_IN_NOTES, TOOL_AUDIT_OUTPUT]

# ── 消息模板 ──────────────────────────────────────────
SYSTEM_MESSAGE = "你是儿童心理辅导助手，请对用户输入进行分析，需要时记笔记并输出结论。"
USER_MESSAGE = "孩子说他考试没考好，想找朋友聊聊。请记笔记并输出结论。"

# ── Auth 脱敏 ─────────────────────────────────────────
def _sanitize(val: Any) -> Any:
    """将 dict 中 Authorization 头截断脱敏。"""
    if isinstance(val, dict):
        return {k: _sanitize(v) for k, v in val.items()}
    if isinstance(val, str) and val.startswith("Bearer "):
        return val[:20] + "..."
    return val


def write_artifact(data: dict[str, Any]) -> None:
    """写入 artifact JSON，Authorization 自动脱敏。"""
    safe = _sanitize(data)
    case = safe.get("case", "unknown")
    layer = safe.get("layer", "Lx")
    provider = safe.get("provider", "unknown")
    wrapper = safe.get("wrapper", "na")
    thinking = safe.get("thinking_mode", "default")
    filename = f"{layer}_{provider}_{wrapper}_{case}_{thinking}.json"
    path = ARTIFACTS_DIR / filename
    path.write_text(json.dumps(safe, indent=2, ensure_ascii=False))


# ── ArtifactBuilder ────────────────────────────────────
class ArtifactBuilder:
    """捕获一次探针调用的完整证据链。

    用法：
        builder = ArtifactBuilder("L1", "ds-native", "httpx", "F1-streaming", "enabled")
        client = builder.make_http_client()
        # ... 用 client 发请求 ...
        builder.record_response(status, headers, body)
        builder.set_parsed_output({...})
        builder.save()
    """

    def __init__(
        self,
        layer: str,
        provider: str,
        wrapper: str,
        case: str,
        thinking_mode: str,
    ) -> None:
        self.meta = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "layer": layer,
            "provider": provider,
            "wrapper": wrapper,
            "case": case,
            "thinking_mode": thinking_mode,
        }
        self.langchain_input: dict[str, Any] | None = None
        self.req_entries: list[dict[str, Any]] = []
        self.response_data: dict[str, Any] = {}
        self.parsed_output: dict[str, Any] = {}

    def make_http_client(self) -> httpx.AsyncClient:
        """创建挂载了 request 事件钩子的 AsyncClient。"""
        async def _dump_req(req: httpx.Request) -> None:
            body_raw = req.content.decode() if req.content else None
            self.req_entries.append({
                "url": str(req.url),
                "headers": dict(req.headers),
                "body": json.loads(body_raw) if body_raw else None,
            })
        return httpx.AsyncClient(
            event_hooks={"request": [_dump_req]},
            timeout=httpx.Timeout(60.0),
        )

    def record_response(
        self, status: int, headers: dict[str, str], body: Any,
    ) -> None:
        self.response_data = {"status": status, "headers": headers, "body": body}

    def set_parsed_output(self, parsed: dict[str, Any]) -> None:
        self.parsed_output = parsed

    def set_langchain_input(self, inp: dict[str, Any]) -> None:
        self.langchain_input = inp

    def save(self, custom_filename: str | None = None) -> None:
        data: dict[str, Any] = {**self.meta}
        if self.langchain_input is not None:
            data["langchain_input"] = self.langchain_input
        data["request"] = self.req_entries[-1] if self.req_entries else {}
        data["response"] = self.response_data
        data["parsed_output"] = self.parsed_output
        data["_req_timeline"] = self.req_entries
        if custom_filename:
            path = ARTIFACTS_DIR / custom_filename
            path.write_text(json.dumps(_sanitize(data), indent=2, ensure_ascii=False))
        else:
            write_artifact(data)


# ── Provider fixtures ─────────────────────────────────
_Settings = Settings()
_DS_KEY = _Settings.deepseek_api_key.get_secret_value()
_BL_KEY = _Settings.bailian_api_key.get_secret_value()


@pytest.fixture(scope="module")
def ds_config() -> dict[str, str]:
    """DeepSeek native provider 配置。"""
    return {
        "base_url": DS_BASE_URL,
        "api_key": _DS_KEY,
        "model": "deepseek-v4-flash",
    }


@pytest.fixture(scope="module")
def bl_config() -> dict[str, str]:
    """百炼兼容端 provider 配置。"""
    return {
        "base_url": BL_BASE_URL,
        "api_key": _BL_KEY,
        "model": "deepseek-v4-flash",
    }


# ── Thinking mode parametrization ─────────────────────
THINKING_MODES = ["enabled", "disabled", "notset"]


@pytest.fixture(params=THINKING_MODES)
def thinking_mode(request: pytest.FixtureRequest) -> str:
    return request.param


# ── Test marker ───────────────────────────────────────
pytestmark = [
    pytest.mark.live,
    pytest.mark.asyncio,
]
