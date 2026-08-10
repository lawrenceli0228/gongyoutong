"""演示图纸预注册(方案 B,落地文档第 1.5 节)—— 让「已在系统里的」图纸有个来处。

===========================================================================
为什么是预注册,不是实时上传(落地文档 1.5 背景)
---------------------------------------------------------------------------
    core/uploads.py 的 ingest_uploads 只认图片/文本,``.dxf`` 走到 _rewrite 没有分支、
    被静默丢弃,前端上传按钮也只写「PDF or Image」。所以实时传 DXF 这条路当前是死的。
    定案:不碰上传管线、不碰前端,改用**启动预注册** ——

        进程启动
          │ 扫 <data_dir>/demo/drawings/*.dxf(本机 <仓库根>/data/…,容器 /app/data/…)
          ▼ 逐个 artifacts.register(kind=DRAWING) → 拿到 32 位 id
        建【中文名 → artifact_id】映射表 DEMO_DRAWINGS
          │ 名字取自同目录 names.json(没配就用文件名主干)
          ▼
        cad 工具永远通过这张表把「首层平面图」换成当前 id

    幂等/id 会变:artifacts.register 用 uuid4,每次进程启动 id 都不同 —— 没关系,
    工具从不硬编码 id,永远查这张表拿**当前** id。演示前重启一次后端即可保证表是新的。
===========================================================================
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Final

from gyt.config import get_settings
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind

logger = logging.getLogger(__name__)

# 演示 DXF 所在的子目录,挂在 config.demo_assets_dir 底下(= <data_dir>/demo/drawings)。
_DRAWINGS_SUBDIR: Final[str] = "drawings"

# 同目录可选的【文件名 → 中文展示名】配置。没有它就退回文件名主干。
_NAMES_FILENAME = "names.json"


def _default_drawings_dir() -> Path:
    """演示 DXF 图纸的默认目录(``<data_dir>/demo/drawings``)。**每次调用都现查配置**。

    以前这里是一对模块级常量::

        _REPO_ROOT = Path(__file__).resolve().parents[5]
        DEFAULT_DRAWINGS_DIR = _REPO_ROOT / "data" / "demo" / "drawings"

    两个洞叠在一起,而且症状一样(都是"一张图都没有"):

    1. **层数按本机目录结构写死。** 本机文件在
       ``<仓库根>/backend/src/gyt/agents/cad/demo_registry.py``,往上 5 层正好是仓库根;
       容器的构建上下文是 ``backend/``、``COPY . /app``,文件落在
       ``/app/src/gyt/agents/cad/demo_registry.py``,**少了一层**,往上 5 层变成 ``/`` ——
       目录被算成 ``/data/demo/drawings``,不存在。
    2. **常量还被当函数默认参数用。** 默认参数在 import 时求值一次就定死,
       测试改完 ``GYT_DATA_DIR`` 再 ``get_settings.cache_clear()`` 也换不动它。

    改成函数之后,取值时刻 = 调用时刻;路径由 ``config.demo_assets_dir`` 统一给出,
    本机(``<仓库根>/data/demo``)与容器(``/app/data/demo``)自动对齐。
    """
    return get_settings().demo_assets_dir / _DRAWINGS_SUBDIR


def _load_names(drawings_dir: Path) -> dict[str, str]:
    """读 names.json(文件名 → 中文名)。文件缺失/坏了都退回空表,不阻塞注册。"""
    path = drawings_dir / _NAMES_FILENAME
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("演示图纸 names.json 读不了,退回文件名主干:%s", exc)
        return {}
    return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def register_demo_drawings(drawings_dir: Path | None = None) -> dict[str, str]:
    """扫目录、逐个登记,返回【展示名 → artifact_id】映射(每次调用都真的重登记一遍)。

    · 名字:names.json 里配了用配的,没配用文件名主干(如 plan_gbk → "plan_gbk")。
    · 重名:同名后来的覆盖先来的,记一条 warning(演示配置错更该吵,不该静默)。
    · 目录不存在/无 .dxf:返回空表并 warning —— 演示当天这就是「一张图都没有」的信号。

    ``drawings_dir=None`` = 现查配置(``_default_drawings_dir()``)。**默认值必须是 None,
    不许写成模块级常量** —— 函数默认参数在 import 时求值一次就定死,配置再改也换不动,
    而且 import 期就会顺手构造一次 Settings。理由详见 ``_default_drawings_dir``。
    """
    if drawings_dir is None:
        drawings_dir = _default_drawings_dir()
    if not drawings_dir.is_dir():
        logger.warning("演示图纸目录不存在:%s(list_drawings 会是空的)", drawings_dir)
        return {}

    names = _load_names(drawings_dir)
    mapping: dict[str, str] = {}
    for dxf in sorted(drawings_dir.glob("*.dxf")):
        display = names.get(dxf.name, dxf.stem)
        artifact_id = artifacts.register(dxf, kind=ArtifactKind.DRAWING, original_name=dxf.name)
        if display in mapping:
            logger.warning("演示图纸展示名重复:「%s」被后一个文件覆盖", display)
        mapping[display] = artifact_id
    logger.info("演示图纸预注册完成:%d 张 —— %s", len(mapping), "、".join(mapping))
    return mapping


@lru_cache(maxsize=1)
def get_demo_drawings() -> dict[str, str]:
    """进程内单次预注册的结果(工具层通过它拿到当前映射表)。

    带 lru_cache:一个进程只注册一次。测试要换目录/重置,直接调 register_demo_drawings()
    传自己的 dir,或 get_demo_drawings.cache_clear() 后再取。
    """
    return register_demo_drawings()


# DEFAULT_DRAWINGS_DIR 已删除(全仓无引用点,grep 过 src / tests / scripts / eval)。
# 要拿默认目录请调 _default_drawings_dir() —— 常量会在 import 时把配置定死,那正是这次的病根。
__all__ = [
    "get_demo_drawings",
    "register_demo_drawings",
]
