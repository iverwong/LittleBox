"""accounts 域。

账号上下文与 child profile 的 bounded context:对外暴露 ORM 模型 (models)、
HTTP 协议 schema (schemas)、跨表业务编排 (service)、父端登录限流 (rate_limit)。
所有跨域通信走 schemas + 显式事件,遵循 D-1 边界。
"""
