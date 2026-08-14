"""知识库的 embedding 与向量存储 —— ingest 与 retriever 的**单一入口**。

===========================================================================
为什么单独一层
---------------------------------------------------------------------------
    建库(ingest)和查询(retriever)必须用**同一个 embedding 模型 + 同一个 collection**,
    否则查询向量和入库向量不在一个空间里,检索永远对不上。把两者收到这里一处定义,
    杜绝「入库用 A 模型、查询用 B 模型」这类静默失灵。

    · embedding:本地 BGE-M3(config.embedding_model)。**首次实例化会加载约 2.2GB 权重**,
      慢;所以用 lru_cache 一个进程只加载一次。CPU 跑,归一化便于余弦相似。
      BGE-M3 是 instruction-free 的稠密检索模型,查询侧不用加特殊前缀。
    · 向量库:Chroma,持久化到 config.chroma_dir(data/chroma/)。langchain-chroma 1.x
      自动落盘,不用手动 persist。collection 名字固定,别改(改了等于换库)。
===========================================================================
"""

from __future__ import annotations

import logging
from collections import Counter
from functools import lru_cache
from typing import TYPE_CHECKING

from gyt.config import get_settings

if TYPE_CHECKING:  # 仅类型检查用,运行期不触发重导入
    from langchain_chroma import Chroma
    from langchain_huggingface import HuggingFaceEmbeddings

logger = logging.getLogger(__name__)

# ⚠️ langchain_huggingface / langchain_chroma 会在 import 时把 torch/transformers/chromadb
# 整条重依赖拉进来(实测冷启动 import 就要 2 分钟)。**绝不在模块顶层 import**,否则
# `import gyt.graph`(建图/起服务/跑测试都会走)会被平白拖慢、甚至超时。
# 一律在函数内部惰性 import —— 只有真的要做 embedding/建库时才付这个代价。

COLLECTION_NAME = "gyt_specs"
"""规范库的 collection 名。入库与查询必须一致,定了别改。"""


@lru_cache(maxsize=1)
def get_embeddings() -> HuggingFaceEmbeddings:
    """本地 BGE-M3 embedding(进程内只加载一次)。

    ⚠️ 首次调用会加载 ~2.2GB 权重(本地没缓存时还要先下载),慢是正常的。
    Dockerfile 已在构建期把权重烤进镜像(冷启动零下载);本地首跑会真下一次。
    """
    from langchain_huggingface import HuggingFaceEmbeddings  # 惰性:见文件顶部说明

    settings = get_settings()
    logger.info("加载 embedding 模型 %s(首次约 2.2GB,慢属正常)", settings.embedding_model)
    return HuggingFaceEmbeddings(
        model_name=settings.embedding_model,
        model_kwargs={"device": "cpu"},
        encode_kwargs={"normalize_embeddings": True},
    )


def get_vectorstore() -> Chroma:
    """规范向量库句柄(持久化在 config.chroma_dir)。

    每次返回一个新句柄但共享同一个持久化目录与同一个(缓存的)embedding ——
    句柄本身很轻,真正贵的是 embedding 模型,那个只加载一次。
    """
    from langchain_chroma import Chroma  # 惰性:见文件顶部说明

    settings = get_settings()
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=get_embeddings(),
        persist_directory=str(settings.chroma_dir),
    )


def count_chunks_by_doc() -> dict[tuple[str, str, str], int]:
    """数每份文档在向量库里的 chunk 数,键 (scope, project_id, source)。供 /library 标入库进度。

    只读 metadata、**绝不加载 embedding 模型** —— 走 chromadb 原生 client(不经 get_vectorstore,
    否则一开资料库就把 2.2GB BGE-M3 拉进内存)。库还没建 / 并发锁 / 版本差异都回空 dict,绝不抛:
    浏览端点不该被向量库的临时状态拖垮(数不出就当「暂无 / 入库中」,下次刷新再数)。
    """
    import chromadb  # 惰性:见文件顶部说明(chromadb 也不轻,别进 import gyt.graph 的热路径)

    try:
        client = chromadb.PersistentClient(path=str(get_settings().chroma_dir))
        col = client.get_collection(COLLECTION_NAME)
        metas = col.get(include=["metadatas"]).get("metadatas") or []
    except Exception:  # noqa: BLE001 —— 库缺失 / 并发锁 / 版本差异一律按「暂时数不出」处理
        logger.debug("数向量块失败,当作暂无", exc_info=True)
        return {}
    return dict(
        Counter(
            (
                str(m.get("scope") or ""),
                str(m.get("project_id") or ""),
                str(m.get("source") or ""),
            )
            for m in metas
        )
    )


__all__ = ["COLLECTION_NAME", "count_chunks_by_doc", "get_embeddings", "get_vectorstore"]
