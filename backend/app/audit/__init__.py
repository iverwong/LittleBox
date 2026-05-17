"""审查 Pipeline：LLM prompt + 装配工厂 + live spike。"""
from .llm import build_audit_llm

__all__ = [
    "build_audit_llm",
]
