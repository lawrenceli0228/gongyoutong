"""demo_registry.py 单测:扫目录注册 + 名字→id + list_drawings 名单(落地文档第 11 节)。"""

from __future__ import annotations

import json

from gyt.agents.cad import demo_registry
from gyt.config import get_settings
from gyt.core import artifacts
from tests.unit._dxf_fixtures import make_gbk_dxf, make_plain_dxf


def _make_dir(tmp_path):
    drawings = tmp_path / "drawings"
    drawings.mkdir()
    make_gbk_dxf(drawings / "plan_gbk.dxf")
    make_plain_dxf(drawings / "plan_utf8.dxf")
    (drawings / "names.json").write_text(
        json.dumps({"plan_gbk.dxf": "首层平面图"}, ensure_ascii=False), encoding="utf-8"
    )
    return drawings


def test_扫目录注册出映射表(tmp_path):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))

    # names.json 配了的用中文名,没配的退回文件名主干。
    assert "首层平面图" in mapping
    assert "plan_utf8" in mapping
    assert len(mapping) == 2


def test_名字映射到合法artifact_id且能resolve(tmp_path):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))
    drawing_id = mapping["首层平面图"]

    assert artifacts.ARTIFACT_ID_RE.fullmatch(drawing_id)
    # 注册是真落盘了的:resolve 得到真实文件路径。
    assert artifacts.resolve(drawing_id).is_file()


def test_目录不存在返回空表(tmp_path):
    assert demo_registry.register_demo_drawings(tmp_path / "不存在") == {}


def test_图纸目录跟着配置走而不是import时定死(tmp_path, monkeypatch):
    """默认目录必须**每次调用现算**,同一个进程里改了配置就得跟着变。

    以前是 ``_REPO_ROOT = parents[5]`` 兼函数默认参数,两个洞叠在一起:
    ① 层数按本机目录结构写死,容器里代码少一层,算出来是 /data/demo/drawings(不存在),
       而目录不存在只 warning 返回空表 —— 表现就是演示当天"一张图都没有";
    ② 常量当默认参数用,import 时求值一次就定死,改 GYT_DATA_DIR 也换不动。
    连着换两个数据根各算一次,把"import 期定死"这个洞钉死。
    """
    # Arrange & Act & Assert:第一个数据根
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "根一"))
    get_settings.cache_clear()
    assert demo_registry._default_drawings_dir() == tmp_path / "根一" / "demo" / "drawings"

    # Act & Assert:同一进程里换第二个数据根,结果必须跟着变
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "根二"))
    get_settings.cache_clear()
    assert demo_registry._default_drawings_dir() == tmp_path / "根二" / "demo" / "drawings"


def test_不传目录时扫的是配置指出来的那个目录(tmp_path, monkeypatch):
    """``register_demo_drawings()`` 不传参时要真的扫到 <data_dir>/demo/drawings。

    上一条只验了算路径的函数,这条走完整条链:配置 → 默认目录 → 扫盘 → 登记出 id。
    只有它绿了,才敢说容器里"启动预注册"能找到图。
    """
    # Arrange:按真实布局把图放进 <data_dir>/demo/drawings
    data_root = tmp_path / "data"
    drawings = data_root / "demo" / "drawings"
    drawings.mkdir(parents=True)
    make_gbk_dxf(drawings / "plan_gbk.dxf")
    monkeypatch.setenv("GYT_DATA_DIR", str(data_root))
    get_settings.cache_clear()

    # Act:一个参数都不传
    mapping = demo_registry.register_demo_drawings()

    # Assert:没配 names.json,展示名退回文件名主干;id 是真登记过、能 resolve 的
    assert set(mapping) == {"plan_gbk"}
    assert artifacts.resolve(mapping["plan_gbk"]).is_file()


async def test_list_drawings工具返回名字清单(tmp_path, monkeypatch):
    mapping = demo_registry.register_demo_drawings(_make_dir(tmp_path))
    # 工具通过 get_demo_drawings 取表;这里把它换成本次注册的结果。
    monkeypatch.setattr("gyt.agents.cad.tools.get_demo_drawings", lambda: mapping)

    from gyt.agents.cad.tools import list_drawings

    envelope = await list_drawings.ainvoke({})
    assert envelope["ok"] is True
    assert set(envelope["data"]["drawings"]) == set(mapping.keys())
