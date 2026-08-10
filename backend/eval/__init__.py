"""工友通评测包 —— 数据集 + 判分 + 跑分,三套评测的共享基座。

目录结构:

    eval/
      README.md      数据集怎么填、判分规则(唯一真相,改判分先改它)
      datasets/      routing.csv / safety.csv / rag.csv
      scorers.py     三套判分函数(纯函数,可单独密集测试)
      runner.py      读盘 → 逐条跑 → 汇总 → 比门槛 → 出明细 → 定退出码
      hooks.py       被测函数的**唯一接线点**(RUNNERS:套名 → async 函数)。
                     加新 Agent 只改这一处 —— 它是包里唯一 import 真 Agent 的文件
      prefilter.py   标注前的照片预筛工具(离线跑,不参与判分)

**「不 import 任何具体 Agent」这条约束属于 `runner.py`,不属于整个包。**
这段以前写的是「本包刻意不 import 任何具体 Agent」,而同一个包里的 hooks.py
第一件事就是 `from gyt.agents.safety.tools import analyze_site_photo` ——
按那句话去理解,会以为 hooks.py 写错了地方。真实分工是:

  · runner.py  一个 Agent 都不 import,被测对象靠 `--runners 模块:属性` 注入
    (见 runner.load_runners)。所以没配 API Key 也能跑它自己的单元测试。
  · hooks.py   反过来,它的**全部职责就是 import 真东西**并把它们装进 RUNNERS。
"""
