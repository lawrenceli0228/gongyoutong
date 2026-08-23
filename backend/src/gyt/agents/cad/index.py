"""CAD 解析索引:落盘 / 读取 / ensure_index(落地文档第 3、4 节)。

===========================================================================
ensure_index —— 消除「顺序错」这一整类 bug(落地文档第 3 节)
---------------------------------------------------------------------------
    别让每个查询工具各自赌「parse 已经先跑过」。模型可能张口就调 query_dimension,
    那样索引不存在就 fail。做法:所有工具第一步都调 ensure_index —— 命中读盘、
    未命中就地解析并落盘。顺序错的 bug 从根上没了。

    读盘(read)、写盘(write)、解析(parse_dxf)全是**阻塞 IO/CPU**,
    ensure_index 用 asyncio.to_thread 把三者都丢线程池:否则 langgraph dev 的
    blockbuster 会抛 BlockingError(schedule/tools.py 的 sqlite 踩过同款坑)。

落盘位置:走 config 的 ``cad_index_dir``(访问即创建),别裸 mkdir。
    一张图一份 ``<drawing_id>.json``,drawing_id 是 32 位 artifact_id ——
    过 ARTIFACT_ID_RE 校验后才拼文件名,天然不含 "/"/".."/glob 通配符,防路径穿越。

并发首解(已知取舍,落地文档第 3 节):两个协程同时首次解析同一张图,理论上会各解
    一次、后写覆盖先写,结果一致只是多算一遍。单用户演示可接受;要严谨就按 id 上
    asyncio.Lock 串行,记 TODO,不在 MVP 里做。
===========================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from gyt.agents.cad import parse, parse_pdf
from gyt.config import get_settings
from gyt.core import artifacts

logger = logging.getLogger(__name__)

_TMP_SUFFIX = ".tmp"
_WRITE_LOCK = threading.Lock()


def _index_path(drawing_id: str) -> Path:
    """校验 id 后定位索引文件。id 非法直接抛,别拿脏字符串去拼路径。"""
    if not artifacts.ARTIFACT_ID_RE.fullmatch(drawing_id):
        raise ValueError(f"drawing_id 不是合法的 artifact_id:{drawing_id!r}")
    return get_settings().cad_index_dir / f"{drawing_id}.json"


def write(drawing_id: str, data: dict[str, Any]) -> None:
    """把解析结果原子落盘(先写临时文件再 rename,避免读到写一半的文件)。阻塞 IO。

    ⚠️ 临时文件名**必须带唯一后缀**,不能是固定的 ``<id>.json.tmp``。
    2026-08-11 真机跑演示场景时实测炸过:

        FileNotFoundError: '<id>.json.tmp' -> '<id>.json'
        (gyt.agents.cad.tools.layer_stats 抛出)

    成因是**同一回合里两个 cad 工具并发**(LangGraph 会并行执行一个回合里的多个
    tool call):两边都 ``ensure_index`` 未命中 → 都解析 → 都往**同一个**
    ``<id>.json.tmp`` 写 → 先跑完的 ``replace`` 把它移走 → 后一个 rename 时源已经没了。

    表现是随机某个 cad 工具失败,而另一个成功 —— 同样的提问重跑一遍又好了,
    最难复现的那一类。用 mkstemp 在**同目录**下开一个唯一文件解决源文件争抢;
    Windows 上多个线程同时 replace 同一目标仍可能 PermissionError,所以最终写盘再用
    进程内锁串行。解析仍在线程池并行,锁只包几十 KB 的 JSON 写入,不会拖慢读图。
    """
    path = _index_path(drawing_id)
    with _WRITE_LOCK:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=path.name + ".", suffix=_TMP_SUFFIX)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except BaseException:
            # 失败了别把半截临时文件留在索引目录里 —— 那些名字长得像索引,
            # 下一个人排查时会以为索引写坏了。
            tmp.unlink(missing_ok=True)
            raise
    if data.get("format") == "pdf":
        logger.info("已写入 CAD 索引 %s(PDF,%d 页)", drawing_id, data.get("page_count", 0))
    else:
        logger.info("已写入 CAD 索引 %s(%d 图层)", drawing_id, len(data.get("layers", [])))


def read(drawing_id: str) -> dict[str, Any] | None:
    """读索引;文件不存在或读坏了都返回 None(交给 ensure_index 重新解析),不抛。

    坏文件不静默当好文件用:记一条 warning,再当未命中处理。
    """
    path = _index_path(drawing_id)
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError 覆盖 JSONDecodeError
        logger.warning("CAD 索引 %s 读坏了,按未命中重新解析:%s", drawing_id, exc)
        return None
    if not isinstance(loaded, dict):
        logger.warning("CAD 索引 %s 格式不对(非对象),按未命中重新解析", drawing_id)
        return None
    return loaded


def _stale(cached: dict[str, Any]) -> bool:
    """旧版本 DXF 索引缺新字段时重解析,避免修了代码却继续命中旧缓存。

    用 ``entities_by_kind`` 认 DXF —— 不用 ``format``:该键是后加的,更老的缓存索引压根没有它
    (会漏判)。PDF 索引没有 ``entities_by_kind`` 也没有 ``extents_outlier``,天然不触发重建
    (否则 parse_pdf 不产出这些字段,会每次访问都重解析、死循环)。

    2026-08-23 又给尺寸补了端点/方向、给文字补了坐标。旧索引若继续命中,
    「衣帽间开间」仍无法做空间关联,所以任一已有记录缺空间字段都判旧。
    """
    if "entities_by_kind" not in cached:
        return False
    if cached.get("index_schema_version") != parse.DXF_INDEX_SCHEMA_VERSION:
        return True
    if "extents_outlier" not in cached:
        return True
    dimensions = cached.get("dimensions", [])
    annotations = cached.get("annotations", [])
    return any(
        "orientation" not in dim or "start" not in dim or "end" not in dim for dim in dimensions
    ) or any("position" not in annotation for annotation in annotations)


async def ensure_index(drawing_id: str) -> dict[str, Any]:
    """所有查询工具的第一步:命中读盘、未命中就地解析并落盘,返回索引 dict。

    可能抛 ``artifacts.ArtifactNotFound``(图纸 id 无效/文件丢失)与 ezdxf 的解析异常
    (文件损坏)—— 都由工具层接住翻成对应的中文信封,本函数不吞。
    """
    cached = await asyncio.to_thread(read, drawing_id)
    if cached is not None and not _stale(cached):
        return cached

    path = await asyncio.to_thread(artifacts.resolve, drawing_id)  # 可能抛 ArtifactNotFound
    # 按后缀分流:.pdf 走 pypdf 抽文字(可能抛 pypdf.errors.PyPdfError),
    # 其余(.dxf)走 ezdxf 结构化解析(可能抛 ezdxf 异常)。两类异常都由工具层接住翻 FILE_CORRUPT。
    if path.suffix.lower() == ".pdf":
        parsed = await asyncio.to_thread(parse_pdf.parse_pdf, path)
    else:
        parsed = await asyncio.to_thread(parse.parse_dxf, path)
    # source_artifact_id 是索引级信息,parse 不认得 id,这里补上再落盘。
    parsed["source_artifact_id"] = drawing_id
    await asyncio.to_thread(write, drawing_id, parsed)
    return parsed


__all__ = ["ensure_index", "read", "write"]
