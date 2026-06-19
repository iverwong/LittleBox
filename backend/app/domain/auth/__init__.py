"""auth 域:鉴权、token 生命周期与子端绑定凭证。

子模块:
- schemas:协议层 Pydantic 模型(登录请求响应、bind_token 入参出参)。
- deps:FastAPI Depends 集(`get_current_account` / `require_parent` / `require_child`)。
- password:argon2id 哈希、验证、临时凭据生成。
- tokens:`AuthToken` 生命周期(issue / resolve / roll / revoke)。
- bind_tokens:一次性 bind_token 生命周期(issue / consume / 兑换结果 stage)。
"""
