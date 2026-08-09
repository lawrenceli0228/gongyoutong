"""Knowledge Agent —— 规范检索(RAG):带页码出处的条文问答。

KW1 阶段先落地检索地基:``store``(BGE-M3 + Chroma 单一入口)+ ``ingest``
(规范 PDF → 逐页切块带页码 → 向量库)。对外的 ``KNOWLEDGE_AGENT_NAME`` /
``build_knowledge_agent`` 在后续步骤补齐(见 .personal/Knowledge_Agent_落地文档.md)。

全部本地跑:BGE-M3 向量(不联网、不烧钱),检索结果每条带 source(完整文件名)+ page(页码)。
"""

from __future__ import annotations
