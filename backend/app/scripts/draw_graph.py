"""draw_graph 工具：把 LangGraph 的两张图导出为 Mermaid 文本。

脚本作为模块被导入时即执行：依次构造主对话图（`main`）与审查图
（`audit`），调用 LangGraph 的 `get_graph(xray=True)` 展开子图，再通过
`draw_mermaid()` 生成 Mermaid 文本字符串，并：

  - 写入 `/app/app/artifacts/graph_main.mmd` 与
    `/app/app/artifacts/graph_audit.mmd`（容器内路径，对应仓库
    `backend/app/artifacts/`）；
  - 在 stdout 分别以 `===== main =====` / `===== audit =====` 分隔打印
    完整文本，便于人工复制。

不连接 DB / Redis，因此不走 `cli_runtime` / `build_runtime` —— 仅
依赖图工厂与 LangGraph 自身。运行方式：

    docker compose exec api python -m app.scripts.draw_graph
"""

from app.domain.audit.graph import build_audit_graph
from app.domain.chat.graph import build_main_graph

for name, build in [("main", build_main_graph), ("audit", build_audit_graph)]:
    graph = build().get_graph(xray=True)  # xray=True 会展开子图
    mermaid = graph.draw_mermaid()  # 返回 Mermaid 文本字符串
    with open(f"/app/app/artifacts/graph_{name}.mmd", "w") as f:
        f.write(mermaid)
    print(f"===== {name} =====")
    print(mermaid)
