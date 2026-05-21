"""LLM provider 调用链路探针测试套件。

测试结构：
- L1 (httpx 原生): §3.1 文档基线，证明端点本身能跑通
- L2 (OpenAI SDK): 证明 OpenAI SDK 在两端的处理差异
- L3 (LangChain): 真实生产链路，含 bind_tools / astream / ainvoke

每组测试产出 artifact JSON 到 artifacts/ 目录。
"""
