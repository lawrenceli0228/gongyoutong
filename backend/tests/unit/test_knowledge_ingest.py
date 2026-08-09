"""ingest 单测:CJK 清洗 / 逐页切块带页码 / manifest 落盘复读。

**不加载 BGE-M3、不建真库**:pdf_to_documents 用假 PdfReader 打桩(只验抽取→切块→页码逻辑);
清洗与 manifest 是纯函数 / 纯文件,直接测。build_index/ensure_index_built(要模型)不在此测。
"""

from __future__ import annotations

from gyt.agents.knowledge import ingest


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
