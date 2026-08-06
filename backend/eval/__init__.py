"""工友通评测包 —— 数据集 + 判分 + 跑分,三套评测的共享基座。

目录结构:

    eval/
      README.md      数据集怎么填、判分规则(唯一真相,改判分先改它)
      datasets/      routing.csv / safety.csv / rag.csv
      scorers.py     三套判分函数(纯函数,可单独密集测试)
      runner.py      读盘 → 逐条跑 → 汇总 → 比门槛 → 出明细 → 定退出码

本包**刻意不 import 任何具体 Agent**:被测对象由调用方注入
(见 runner.load_runners),这样 Agent 还没写出来时评测框架也能先跑起来。
"""
