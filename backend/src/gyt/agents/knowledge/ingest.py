"""规范 PDF → 逐页切块(带页码)→ Chroma。方案 B 预置建库 + 方案 A 增量入库共用。

===========================================================================
命脉:页码必须在切分时钉进每个 chunk 的 metadata(落地文档 §4)
---------------------------------------------------------------------------
    RAG 的全部价值是「带页码的出处」——判分要 source + page 都对。做法:
    **逐页抽文本、逐页切块**,绝不跨页合并。每个 chunk 带
        metadata = {"source": 完整文件名, "page": PDF 物理页(1 起)}
    页码口径用 **PDF 物理页**(不是印刷页):系统内自洽即判对,演示展示的就是这个 PDF。
    (印刷页与 PDF 页有偏移,解析印刷页脚噪声大,留作后续增强。)

幂等(别每次启动都重嵌 464 页,那太慢):
    manifest 记 {文件名: sha256}。build_index 时文件 sha 未变就跳过整份;
    变了就先删同 source 的旧块再重嵌。chunk id 也确定化(source#p页#c序),重跑不产重复。

阻塞:PDF 抽取、embedding、写库全是阻塞 IO/CPU。本模块同步实现;
    启动预置(build 期、事件循环之前)直接调即可;方案 A 在请求期调时,调用方负责 to_thread。
===========================================================================
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Final

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader

from gyt.agents.knowledge.store import get_vectorstore
from gyt.config import get_settings

logger = logging.getLogger(__name__)

# 规范原文 PDF 所在的子目录,挂在 config.demo_assets_dir 底下(= <data_dir>/demo/docs)。
_DOCS_SUBDIR: Final[str] = "docs"

_MANIFEST_NAME = "_ingest_manifest.json"


def _default_docs_dir() -> Path:
    """规范原文 PDF 的默认目录(``<data_dir>/demo/docs``)。**每次调用都现查配置**。

    以前这里是一对模块级常量::

        _REPO_ROOT = Path(__file__).resolve().parents[5]
        DEFAULT_DOCS_DIR = _REPO_ROOT / "data" / "demo" / "docs"

    两个洞叠在一起,而且症状一模一样(都是"库是空的"):

    1. **层数按本机目录结构写死。** 本机文件在
       ``<仓库根>/backend/src/gyt/agents/knowledge/ingest.py``,往上 5 层正好是仓库根;
       但容器的构建上下文是 ``backend/``、``COPY . /app``,文件落在
       ``/app/src/gyt/agents/knowledge/ingest.py``,**少了一层**,往上 5 层变成 ``/`` ——
       目录被算成 ``/data/demo/docs``,根本不存在。
    2. **常量还被当函数默认参数用。** 默认参数在 import 时求值一次就定死,测试里
       改完 ``GYT_DATA_DIR`` 再 ``get_settings.cache_clear()`` 也换不动它。

    改成函数之后,取值时刻 = 调用时刻,配置指哪就扫哪。
    路径本身则由 ``config.demo_assets_dir`` 统一给,本机与容器自动对齐。
    """
    return get_settings().demo_assets_dir / _DOCS_SUBDIR


# 中文规范切分:块不宜过大(稀释召回)也不宜过小(丢上下文)。优先按段/句切,
# 尽量别把「第 3.2.1 条」这类切碎。500 字符 ≈ 一两段。
_CHUNK_SIZE = 500
_CHUNK_OVERLAP = 80
_SEPARATORS = ["\n\n", "\n", "。", ";", "；", ",", "，", " ", ""]
_MIN_CHUNK_CHARS = 10  # 丢掉页眉页脚这种碎片

# 中文/中日韩标点区间。用于清洗这份 PDF 文字层的 OCR 噪声(中文字之间被塞了空格)。
_CJK = r"一-鿿　-〿＀-￯"
_CJK_SPACE_RE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def _normalize_cjk_spaces(text: str) -> str:
    """去掉夹在**两个中文字/中文标点之间**的空白(「国 家 标准」→「国家标准」)。

    **只删中文-中文之间的空格**,保留 ASCII 词间空格(「GB 50016」不动)——
    这份规范 PDF 的文字层有 OCR 噪声,不清洗会稀释检索、也让引用的原文难看。
    多次 sub 处理连续的「字 空 字 空 字」链。
    """
    prev = None
    while prev != text:
        prev = text
        text = _CJK_SPACE_RE.sub("", text)
    return text


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        separators=_SEPARATORS,
        keep_separator=True,
    )


def pdf_to_documents(pdf_path: Path) -> list[Document]:
    """逐页抽文本→按长度切块。每块钉 source(完整文件名)+ page(PDF 物理页,1 起)。

    阻塞函数。空页/碎片(<10 字符)跳过。文件损坏时让 pypdf 的异常向上抛给调用方。
    """
    reader = PdfReader(str(pdf_path))
    splitter = _splitter()
    source = pdf_path.name
    docs: list[Document] = []
    for i, page in enumerate(reader.pages):
        text = _normalize_cjk_spaces((page.extract_text() or "").strip())
        if not text:
            continue
        for chunk in splitter.split_text(text):
            chunk = chunk.strip()
            if len(chunk) < _MIN_CHUNK_CHARS:
                continue
            docs.append(Document(page_content=chunk, metadata={"source": source, "page": i + 1}))
    return docs


def _chunk_ids(docs: list[Document]) -> list[str]:
    """确定化的 chunk id:source#p<页>#c<序>。重跑同一文件 → 同 id → upsert 不产重复。"""
    return [f"{d.metadata['source']}#p{d.metadata['page']}#c{i}" for i, d in enumerate(docs)]


def ingest_pdf(pdf_path: Path, vectorstore=None) -> int:
    """把一部 PDF 入库(先删同 source 旧块,再加新块)。返回入库块数。阻塞。

    先删后加,是为了「同名文件内容变了」也能干净替换,不留旧块。
    """
    vs = vectorstore or get_vectorstore()
    source = pdf_path.name
    existing = vs.get(where={"source": source})
    stale_ids = (existing or {}).get("ids") or []
    if stale_ids:
        vs.delete(ids=stale_ids)
    docs = pdf_to_documents(pdf_path)
    if not docs:
        logger.warning("规范 %s 一个 chunk 都没抽出来(可能是扫描件?要 OCR)", source)
        return 0
    vs.add_documents(docs, ids=_chunk_ids(docs))
    logger.info("已入库 %s:%d 个 chunk", source, len(docs))
    return len(docs)


def _manifest_path() -> Path:
    return get_settings().chroma_dir / _MANIFEST_NAME


def _load_manifest() -> dict[str, str]:
    path = _manifest_path()
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("ingest manifest 读坏了,按未建库处理")
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _save_manifest(manifest: dict[str, str]) -> None:
    _manifest_path().write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def build_index(docs_dir: Path | None = None, *, rebuild: bool = False) -> dict[str, int]:
    """扫目录建库。幂等:文件 sha 未变则跳过(不重复 embedding)。返回 {文件名: 入库块数}。阻塞。

    rebuild=True 时无视 manifest,每份都重嵌(换 embedding 模型/改切分后用)。

    ``docs_dir=None`` = 现查配置(``_default_docs_dir()``)。**默认值必须是 None,
    不许写成模块级常量** —— 函数默认参数在 import 时求值一次就定死,配置再改也换不动,
    而且 import 期就会顺手构造一次 Settings。理由详见 ``_default_docs_dir`` 的说明。
    """
    if docs_dir is None:
        docs_dir = _default_docs_dir()
    if not docs_dir.is_dir():
        logger.warning("规范目录不存在:%s(知识库会是空的)", docs_dir)
        return {}

    vs = get_vectorstore()
    manifest = _load_manifest()
    result: dict[str, int] = {}
    for pdf in sorted(docs_dir.glob("*.pdf")):
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if not rebuild and manifest.get(pdf.name) == sha:
            logger.info("跳过(已入库、未改动):%s", pdf.name)
            continue
        result[pdf.name] = ingest_pdf(pdf, vs)
        manifest[pdf.name] = sha
    _save_manifest(manifest)
    logger.info("建库完成:%s", result or "(无新增,全部已入库)")
    return result


def ensure_index_built(docs_dir: Path | None = None) -> None:
    """方案 B 启动预置:docs 下有规范但索引缺/有改动时就地建一次;已建好(manifest 命中)秒过。

    幂等 + **安全**:建库失败只记日志、不抛 —— 知识库塌了不该拖垮整个服务(检索侧拿不到库
    自会回「无依据」)。**默认不在启动时被调**:由 config.knowledge_prebuild_at_startup 开关控制,
    该开关默认 False,专为**保护测试不触发 2.2GB 建库**(测试 chroma_dir 是 tmp,一调必重建)。
    dev/生产要「零操作起库」就在 .env / compose 里把开关打开(首次会阻塞启动约 15 分钟)。

    ``docs_dir=None`` 同 build_index:现查配置,不是 import 期定死的常量。
    取默认值这一步也放在 try 里面 —— 本函数对外承诺"绝不抛",配置读坏了也一样。
    """
    try:
        if docs_dir is None:
            docs_dir = _default_docs_dir()
        if not docs_dir.is_dir():
            return
        manifest = _load_manifest()
        pending = [
            p
            for p in sorted(docs_dir.glob("*.pdf"))
            if manifest.get(p.name) != hashlib.sha256(p.read_bytes()).hexdigest()
        ]
        if not pending:
            logger.info("知识库已是最新,跳过启动建库")
            return
        logger.info(
            "启动预置:%d 份规范未入库/有改动,开始建库(首次含 2.2GB 模型,慢是正常的)",
            len(pending),
        )
        build_index(docs_dir)
    except Exception:  # noqa: BLE001 —— 建库失败不许拖垮启动
        logger.exception("启动建知识库失败(不影响其它功能;检索会回无依据)")


def main() -> int:
    """CLI:``uv run python -m gyt.agents.knowledge.ingest [--rebuild]``。

    **失败必须长得像失败。** 以前这里只有一句「没有新增入库(目录为空,或全部已入库/未改动)」
    加 exit 0 —— 而它同时覆盖了两种截然相反的情况:

        真·成功:规范都在,sha 没变,跳过重嵌   ← 该绿
        真·失败:规范目录压根不存在(路径算错/容器没挂卷/资产没同步)  ← 该红,却也绿了

    队友的启动手册就把那句话当成了成功判据,于是"资产根本没进来"在验收里显示为通过,
    一直拖到演示时 knowledge 回一句"规范里查不到"才暴露 —— 而那句话跟真查不到一模一样。
    现在把两种失败提到建库之前,各自给一句能直接照着排查的中文提示 + 退出码 1。
    """
    import sys

    docs_dir = _default_docs_dir()
    if not docs_dir.is_dir():
        print(f"[错误] 规范目录不存在:{docs_dir}")
        print("       容器里:确认 compose 把仓库根 ./data 挂到了 /app/data;")
        print("       本机:确认演示资产在仓库根 data/demo/docs 下(不是 backend/data)。")
        return 1
    pdfs = sorted(docs_dir.glob("*.pdf"))
    if not pdfs:
        print(f"[错误] {docs_dir} 里一份 PDF 都没有 —— 这样建出来的知识库会是空的。")
        print("       把规范原文 PDF 放进这个目录再重跑。")
        return 1

    result = build_index(docs_dir, rebuild="--rebuild" in sys.argv)
    if not result:
        # 走到这里说明目录在、PDF 也在,只是 sha 都没变 —— 这才是真的成功。
        print(f"全部已入库、无需重建(共 {len(pdfs)} 份规范)。")
        return 0
    print("已建库:")
    for name, n in result.items():
        print(f"  {name}: {n} 个 chunk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# DEFAULT_DOCS_DIR 已删除(全仓无引用点,grep 过 src / tests / scripts / eval)。
# 要拿默认目录请调 _default_docs_dir() —— 常量会在 import 时把配置定死,那正是这次的病根。
__all__ = [
    "build_index",
    "ensure_index_built",
    "ingest_pdf",
    "pdf_to_documents",
]
