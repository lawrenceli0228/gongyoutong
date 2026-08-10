"""ingest 单测:CJK 清洗 / 逐页切块带页码 / manifest 落盘复读 / 规范目录现算 + CLI 退出码。

**不加载 BGE-M3、不建真库**:pdf_to_documents 用假 PdfReader 打桩(只验抽取→切块→页码逻辑);
清洗与 manifest 是纯函数 / 纯文件,直接测。真正要嵌入的那条路径(build_index 走到
get_vectorstore)不在此测 —— 下面几条路径用例都停在"目录不存在 / 没有 PDF"的早退分支上,
一步都碰不到向量库。
"""

from __future__ import annotations

import logging
import sys

from gyt.agents.knowledge import ingest
from gyt.config import get_settings


def test_CJK清洗只删中文间空格保留ASCII():
    assert ingest._normalize_cjk_spaces("国 家 标准") == "国家标准"
    assert ingest._normalize_cjk_spaces("疏 散 门 至安全出口") == "疏散门至安全出口"
    # ASCII 词间空格不动
    assert ingest._normalize_cjk_spaces("GB 50016 - 2014") == "GB 50016 - 2014"


class _FakePage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeReader:
    """假 PdfReader:第1页有中文(带噪声空格)、第2页空。"""

    def __init__(self, _path: str) -> None:
        self.pages = [_FakePage("消 防 车 道 的净宽度不应小于 4.0 米。"), _FakePage("")]


def test_逐页切块每块带完整文件名与页码(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "PdfReader", _FakeReader)
    pdf = tmp_path / "某规范.pdf"
    docs = ingest.pdf_to_documents(pdf)

    assert docs, "第一页有内容,应产出 chunk"
    # source = 完整文件名(评测按它判);page = PDF 物理页(1 起);空页不产 chunk。
    assert all(d.metadata["source"] == "某规范.pdf" for d in docs)
    assert all(d.metadata["page"] == 1 for d in docs)
    # 清洗生效:中文间空格没了
    assert "消防车道" in docs[0].page_content


def test_chunk_id确定化(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "PdfReader", _FakeReader)
    docs = ingest.pdf_to_documents(tmp_path / "x.pdf")
    ids = ingest._chunk_ids(docs)
    assert ids[0].startswith("x.pdf#p1#c0")
    assert len(ids) == len(set(ids)), "id 必须唯一"


def test_manifest落盘复读一致(tmp_path):
    # manifest 路径走 config.chroma_dir(conftest 已把 data_dir 指到 tmp)。
    ingest._save_manifest({"a.pdf": "sha_a", "b.pdf": "sha_b"})
    assert ingest._load_manifest() == {"a.pdf": "sha_a", "b.pdf": "sha_b"}


def test_manifest读坏了当空处理(tmp_path):
    ingest._manifest_path().write_text("{ 这不是 json", encoding="utf-8")
    assert ingest._load_manifest() == {}


# ---------------------------------------------------------------------------
# 规范目录从哪来(以前是 parents[5] 常量,容器里算错还不报错)
# ---------------------------------------------------------------------------


def test_规范目录跟着配置走而不是import时定死(tmp_path, monkeypatch):
    """默认目录必须**每次调用现算**,同一个进程里改了配置就得跟着变。

    以前是 ``_REPO_ROOT = parents[5]`` 兼函数默认参数,两个洞叠在一起:
    ① 层数按本机目录结构写死,容器里代码少一层,算出来是 /data/demo/docs(不存在);
    ② 常量当默认参数用,import 时求值一次就定死,改 GYT_DATA_DIR 也换不动。
    连着换两个数据根各算一次,就把"import 期定死"这个洞钉死了。
    """
    # Arrange & Act & Assert:第一个数据根
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "根一"))
    get_settings.cache_clear()
    assert ingest._default_docs_dir() == tmp_path / "根一" / "demo" / "docs"

    # Act & Assert:同一进程里换第二个数据根,结果必须跟着变
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "根二"))
    get_settings.cache_clear()
    assert ingest._default_docs_dir() == tmp_path / "根二" / "demo" / "docs"


def test_build_index不传参时也是现算目录(tmp_path, monkeypatch, caplog):
    """``build_index()`` 不传 docs_dir 时走的是同一条现算路径(默认值是 None,不是常量)。

    目录不存在,函数在碰 get_vectorstore 之前就返回 {} —— 所以这条用例不加载 BGE-M3、
    不建真库。断言警告里印的是**配置指出来的那个目录**,证明默认值确实来自配置。
    """
    # Arrange
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "空的"))
    get_settings.cache_clear()

    # Act
    with caplog.at_level(logging.WARNING):
        result = ingest.build_index()

    # Assert
    assert result == {}
    assert str(tmp_path / "空的" / "demo" / "docs") in caplog.text


# ---------------------------------------------------------------------------
# CLI:失败必须长得像失败(以前两种失败都印"没有新增入库" + exit 0)
# ---------------------------------------------------------------------------


def test_CLI在规范目录不存在时报错并退出码1(tmp_path, monkeypatch, capsys):
    """资产没跟过来的时候,验收必须是红的。

    以前:目录不存在 → build_index 只 warning + return {} → CLI 印
    「没有新增入库(目录为空,或全部已入库/未改动)」并 exit 0。队友的启动手册把那句话
    写成了成功判据,于是路径错/没挂卷在验收里显示为通过,一直拖到演示时 knowledge
    回一句"规范里查不到"才暴露 —— 而那句话跟真查不到长得一模一样。
    """
    # Arrange
    monkeypatch.setenv("GYT_DATA_DIR", str(tmp_path / "没有资产"))
    get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["ingest"])

    # Act
    code = ingest.main()

    # Assert:退出码非 0,且提示里带上真实路径(工人/队友能照着这句话去查)
    assert code == 1
    out = capsys.readouterr().out
    assert "规范目录不存在" in out
    assert str(tmp_path / "没有资产" / "demo" / "docs") in out


def test_CLI在目录里一份PDF都没有时报错并退出码1(tmp_path, monkeypatch, capsys):
    """目录在、但里面是空的 —— 建出来的库必然是空的,同样不许假装成功。"""
    # Arrange:只建目录,不放 PDF
    data_root = tmp_path / "data"
    (data_root / "demo" / "docs").mkdir(parents=True)
    monkeypatch.setenv("GYT_DATA_DIR", str(data_root))
    get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["ingest"])

    # Act
    code = ingest.main()

    # Assert:在调 build_index(会拉 2.2GB 模型)之前就判掉了
    assert code == 1
    assert "一份 PDF 都没有" in capsys.readouterr().out
