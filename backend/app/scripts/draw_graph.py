from app.audit.graph import build_audit_graph
from app.chat.graph import build_main_graph

for name, build in [("main", build_main_graph), ("audit", build_audit_graph)]:
    graph = build().get_graph(xray=True)  # xray=True 会展开子图
    mermaid = graph.draw_mermaid()  # 返回 Mermaid 文本字符串
    with open(f"/app/app/artifacts/graph_{name}.mmd", "w") as f:
        f.write(mermaid)
    print(f"===== {name} =====")
    print(mermaid)
