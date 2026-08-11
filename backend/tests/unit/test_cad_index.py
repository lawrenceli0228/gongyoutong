"""index.py 单测:落盘复读一致 + 键=id + ensure_index 命中不重复解析(落地文档第 11 节)。"""

from __future__ import annotations

import pytest

from gyt.agents.cad import index, parse
from gyt.core import artifacts
from gyt.core.artifacts import ArtifactKind
from tests.unit._dxf_fixtures import make_gbk_dxf


def _register(tmp_path) -> str:
    make_gbk_dxf(tmp_path / "gbk.dxf")
    return artifacts.register(
        tmp_path / "gbk.dxf", kind=ArtifactKind.DRAWING, original_name="gbk.dxf"
    )


def test_落盘后可复读且一致(tmp_path):
    drawing_id = _register(tmp_path)
    parsed = parse.parse_dxf(artifacts.resolve(drawing_id))
    index.write(drawing_id, parsed)

    back = index.read(drawing_id)
    assert back == parsed


def test_索引键就是drawing_id(tmp_path):
    from gyt.config import get_settings

    drawing_id = _register(tmp_path)
    index.write(drawing_id, {"layers": []})
    expected = get_settings().cad_index_dir / f"{drawing_id}.json"
    assert expected.is_file()


def test_读不存在的索引返回None(tmp_path):
    assert index.read("a" * 32) is None


def test_读坏掉的索引返回None(tmp_path):
    from gyt.config import get_settings

    bad = get_settings().cad_index_dir / f"{'b' * 32}.json"
    bad.write_text("{ 这不是合法 json", encoding="utf-8")
    assert index.read("b" * 32) is None


def test_非法id拒绝拼路径(tmp_path):
    with pytest.raises(ValueError):
        index.read("../etc/passwd")


async def test_ensure_index命中读盘不重复解析(tmp_path, monkeypatch):
    # 打桩数 parse 调用次数,和 safety 数内层模型调用同理(落地文档第 11 节)。
    drawing_id = _register(tmp_path)

    calls = {"n": 0}
    real = parse.parse_dxf

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(parse, "parse_dxf", counting)

    first = await index.ensure_index(drawing_id)  # 未命中 → 解析一次并落盘
    assert calls["n"] == 1
    assert first["source_artifact_id"] == drawing_id

    second = await index.ensure_index(drawing_id)  # 命中读盘 → 不再解析
    assert calls["n"] == 1
    assert second == first


async def test_ensure_index图纸不存在时抛ArtifactNotFound(tmp_path):
    with pytest.raises(artifacts.ArtifactNotFound):
        await index.ensure_index("f" * 32)


def test_并发写同一张图不会互相把临时文件抢掉(tmp_path):
    """回归:2026-08-11 真机跑演示场景时,layer_stats 抛过

        FileNotFoundError: '<id>.json.tmp' -> '<id>.json'

    成因是临时文件名写死成 `<id>.json.tmp`:同一回合里两个 cad 工具并发
    (LangGraph 会并行执行一个回合里的多个 tool call),都未命中、都解析、
    都往**同一个** tmp 写,先跑完的 replace 把它移走,后一个 rename 时源没了。

    这个 bug 的特征是「同样的提问重跑一遍又好了」—— 没有测试盯着必然复发。
    所以这里真起线程并发写,而不是只断言文件名长相。
    """
    import threading

    drawing_id = _register(tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            barrier.wait(timeout=10)  # 让 8 个线程尽量同时进 write
            index.write(drawing_id, {"layers": [f"L{n}"]})
        except BaseException as exc:  # noqa: BLE001 —— 要把任何异常带回主线程
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert not errors, f"并发写抛异常了:{errors!r}"

    # 落盘的必须是**某一次完整的写**,不能是半截或空文件
    back = index.read(drawing_id)
    assert back is not None, "并发写完之后索引读不出来"
    assert back["layers"][0].startswith("L")

    # 临时文件不许留在索引目录里 —— 那些名字长得像索引,会误导排查的人
    from gyt.config import get_settings

    leftovers = list(get_settings().cad_index_dir.glob("*.tmp"))
    assert not leftovers, f"残留临时文件:{leftovers}"
