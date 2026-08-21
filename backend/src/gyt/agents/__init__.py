"""工友通子 Agent 集合 —— 一个 Agent 一个子包。

每个子包的固定结构(照抄 ping/ 即可):
    agents/<名字>/
        __init__.py   # 导出 <名字>_AGENT_NAME 与 build_<名字>_agent()
        prompt.md     # 中文系统提示词,维护说明写在 <!-- --> 里(加载时会被剥掉)
        tools.py      # 工具函数,一律 @tool + @tool_guard,返回 errors 信封

T1 阶段这里只有 ping(连通性占位),用来证明
「Supervisor → 子 Agent → 工具」这条链路真的通了。

W2/W3 按泳道各自往这里加,互不相交(D11「结构即分工,合并冲突≈0」):
    X 泳道 → knowledge/   规范文档检索 + 页码引用,空结果防编造
             schedule/    SQLite CRUD + 中文自然语言查任务
    Y 泳道 → safety/      kimi-k3 视觉识图 + 结构化检查清单
             cad/         DXF 四类查询 + PNG 预览
             report/      日报/巡检两个模板,python-docx 渲染落盘

加新 Agent 时只需往 ``gyt.graph.AGENT_REGISTRY``(模块级常量)追加一条
``AgentSpec(name=..., summary=..., build=..., requires_project=...)``,
Supervisor 会自动生成对应的交接工具,
其余地方一个字都不用改。

    ⚠️ 挂载点只有 AGENT_REGISTRY 这**一处**,别去动 build_graph() 的函数体 ——
       它内部只有一行 ``agents = [spec.build() for spec in specs]``,没有"手工列表"可追加。
       绕开登记表还会绕开 _validate_registry() 的校验。
    ⚠️ summary **必填**:它是 Supervisor 判断"这活派给谁"的唯一依据(D18 路由门槛 0.90 靠它),
       留空会被 _validate_registry() 直接 raise ValueError。
"""

__all__: list[str] = []
