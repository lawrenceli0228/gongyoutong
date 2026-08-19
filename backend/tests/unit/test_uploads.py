"""聊天界面直传图片 → 产物注册表(gyt.core.uploads)的单元测试。

这一层薄,但它是**演示日的主路径** —— 评委会去点「Upload Image」按钮,
不会去复制 32 位十六进制编号。而它坏掉的表现极其难查:
文本档模型收到 image 块直接 400,前端不渲染这个错,用户只看到"点了没反应"。
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from gyt.config import ALLOWED_IMAGE_EXT
from gyt.core import artifacts
from gyt.core.uploads import EXT_BY_MIME, ingest_uploads


def _ids_in(text: str) -> list[str]:
    """从改写后的正文里抠出所有产物编号。

    刻意用正则而不是 split():编号前面是中文冒号(「照片编号:」),
    按空白切分会把标签和编号粘成一个词 —— 这正是这条辅助函数第一版踩的坑。
    """
    return re.findall(r"[0-9a-f]{32}", text)


def _jpeg(color: tuple[int, int, int] = (90, 130, 110)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _data_uri(payload: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _image_part(payload: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
    """照 agent-chat-ui 真正发出来的形状构造:image_url 是个嵌套对象。"""
    return {"type": "image_url", "image_url": {"url": _data_uri(payload, mime)}}


def test_上传的图片被登记成产物_消息换成编号() -> None:
    """核心路径:文本档模型再也见不到 image 块,而 Safety 工具拿到的正是它要的编号。"""
    payload = _jpeg()
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "查安全隐患"}, _image_part(payload)], id="u1"
            )
        ]
    }

    result = ingest_uploads(state)

    updates = result["messages"]
    assert isinstance(updates[0], RemoveMessage), "原消息必须被删掉,否则 image 块还在 state 里"
    assert updates[0].id == "u1"

    rewritten = updates[1]
    assert isinstance(rewritten.content, str), "改写后必须是纯文本,含 image 块就会让文本档 400"
    assert "查安全隐患" in rewritten.content

    # 编号要能真的解析回同一张图 —— 只断言"有个 32 位串"是不够的
    ids = _ids_in(rewritten.content)
    assert len(ids) == 1
    assert artifacts.resolve(ids[0]).read_bytes() == payload


def test_纯文本消息原样放行() -> None:
    """绝大多数轮次用户只是打字。这条路径必须零开销、零副作用。"""
    state = {"messages": [HumanMessage(content="脚手架间距是多少", id="u1")]}
    assert ingest_uploads(state) == {}


def test_没有消息时不报错() -> None:
    assert ingest_uploads({"messages": []}) == {}
    assert ingest_uploads({}) == {}


def test_历史里遗留的image块也要改写_哪怕最后一条不是用户消息() -> None:
    """🔴 这条原来断的是反的(「最后一条不是用户消息就什么都不动」),而那正是 bug。

    原假设:「更早的那些在它们自己那一轮已经被改写过了」。
    **run 失败时不成立** —— 失败的 run 不提交检查点,改写就丢了。

    2026-08-19 真机实录(断网那阵子):
        发照片 → 改写了 → 模型调用 APIConnectionError → run 失败 → 改写没落盘
        再发一张 → 线程变成 [文本, image原样, image原样]
        hook 只看最后一条 → 更早那条永远是原样
    于是那条线程**被永久毒化**:此后每次提问,文本档模型都收到 image 块,
    DeepSeek 回 400「unknown variant `image`, expected `text`」,
    而工友只看到「出错了」,自己好不了 —— 除非删掉整条对话。

    下面这个 state 就是毒化后的形状:历史里躺着一条原样 image,后面跟着 AI 消息。
    """
    state = {
        "messages": [
            HumanMessage(content=[_image_part(_jpeg())], id="u1"),
            AIMessage(content="好的", id="a1"),
        ]
    }
    out = ingest_uploads(state)
    assert out, "历史里的 image 块没被改写 —— 这条线程会在下一次提问时 400"
    rewritten = [m for m in out["messages"] if isinstance(m, HumanMessage)]
    assert len(rewritten) == 1
    assert isinstance(rewritten[0].content, str)
    assert "照片编号" in rewritten[0].content
    assert rewritten[0].id == "u1", "必须用同一个 id 原地替换,不然位置会乱"


def test_已改写过的消息不会被重复登记() -> None:
    """扫全部消息是否会把同一张图登记两次 —— **不会**,而这是扫全部能成立的前提。

    改写完的 content 是纯字符串,`_rewrite` 第一行 isinstance(content, list)
    就返回 None。所以对已改写的消息本函数是空操作。
    """
    state = {
        "messages": [
            HumanMessage(content="看看这张照片。(照片编号:" + "a" * 32 + ")", id="u1"),
            AIMessage(content="好的", id="a1"),
            HumanMessage(content="再看看这条", id="u2"),
        ]
    }
    assert ingest_uploads(state) == {}


def test_多张图片各自登记() -> None:
    a, b = _jpeg((200, 30, 30)), _jpeg((30, 30, 200))
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "这两张都看下"}, _image_part(a), _image_part(b)],
                id="u1",
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    ids = _ids_in(rewritten.content)
    assert len(ids) == 2
    assert artifacts.resolve(ids[0]).read_bytes() == a
    assert artifacts.resolve(ids[1]).read_bytes() == b


@pytest.mark.parametrize("mime", ["image/gif", "image/bmp", "image/avif", "image/svg+xml"])
def test_不支持的格式给出可操作的提示(mime: str) -> None:
    """这些格式 Kimi 或我们的白名单不收。**关键是要告诉用户怎么办** ——
    静默丢掉的话,用户会以为传上去了,然后等一个永远不来的答复。"""
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "看看"}, _image_part(_jpeg(), mime)], id="u1"
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "转成 JPG 或 PNG" in rewritten.content
    assert "看看" in rewritten.content, "原来的文字不能丢"


def test_坏掉的base64被忽略而不是崩掉() -> None:
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "看看"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,@@@不是base64@@@"},
                    },
                ],
                id="u1",
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "转成 JPG 或 PNG" in rewritten.content


def test_只有图片没有文字时也给一句话() -> None:
    """用户可能只传图不打字。空正文会让 supervisor 无从判断该派给谁。"""
    state = {"messages": [HumanMessage(content=[_image_part(_jpeg())], id="u1")]}
    rewritten = ingest_uploads(state)["messages"][1]
    assert "照片" in rewritten.content


def test_MIME映射的扩展名都在白名单里() -> None:
    """落到白名单外的扩展名会被 artifacts._safe_ext 剥成空串 —— 文件没有后缀,
    而 Safety 工具按后缀判格式,于是变成一条「上传成功但永远看不了」的静默死路。

    uploads.py 末尾有条 import 期断言守着,这里是它的显式副本,
    免得有人把那条断言当死代码删掉。
    """
    assert not set(EXT_BY_MIME.values()) - ALLOWED_IMAGE_EXT


def test_content里混着裸字符串也能处理() -> None:
    """有些客户端把纯文本直接放进 content 数组而不包成 {"type":"text"}。"""
    state = {"messages": [HumanMessage(content=["先看这个", _image_part(_jpeg())], id="u1")]}
    rewritten = ingest_uploads(state)["messages"][1]
    assert "先看这个" in rewritten.content
    assert len(_ids_in(rewritten.content)) == 1


def test_image_url直接是字符串的形状也接得住() -> None:
    """LangChain 的 image content 块有两种写法,别只认嵌套对象那一种。"""
    payload = _jpeg()
    state = {
        "messages": [
            HumanMessage(content=[{"type": "image_url", "image_url": _data_uri(payload)}], id="u1")
        ]
    }
    rewritten = ingest_uploads(state)["messages"][1]
    ids = _ids_in(rewritten.content)
    assert len(ids) == 1
    assert artifacts.resolve(ids[0]).read_bytes() == payload


def test_非data_uri的图片链接被拒并提示() -> None:
    """月之暗面只收 base64 data URI,公网 http/https 图片链接不支持 ——
    这里就挡掉,别等发到上游才失败。"""
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "看看"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}},
                ],
                id="u1",
            )
        ]
    }
    rewritten = ingest_uploads(state)["messages"][1]
    assert "转成 JPG 或 PNG" in rewritten.content


# ---------------------------------------------------------------------------
# agent-chat-ui 真正发出来的形状
#
# 这一节是照着 frontend/src/lib/multimodal-utils.ts 的源码写的,不是猜的。
# 之前正是因为照 OpenAI 的形状写、没去读前端源码,导致用户传 JPG 却被告知
# 「请转成 JPG 或 PNG」—— 上面那些 image_url 用例全绿,这条路照样是断的。
# ---------------------------------------------------------------------------


def _ui_image_part(payload: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
    """agent-chat-ui 的 fileToContentBlock 真正产出的形状。

    注意三点都和 OpenAI 格式不同:字段叫 data 不叫 url;base64 **不带**
    "data:...;base64," 前缀;MIME 的键是驼峰 mimeType。
    """
    return {
        "type": "image",
        "mimeType": mime,
        "data": base64.b64encode(payload).decode("ascii"),
        "metadata": {"name": "photo.jpg"},
    }


def test_前端真实格式的图片能被登记() -> None:
    payload = _jpeg()
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "查安全隐患"}, _ui_image_part(payload)], id="u1"
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    ids = _ids_in(rewritten.content)
    assert len(ids) == 1, "前端格式没被认出来 —— 用户会看到「请转成 JPG」而他传的就是 JPG"
    assert artifacts.resolve(ids[0]).read_bytes() == payload


def test_前端格式也支持下划线写法的mime() -> None:
    """LangChain 自己序列化 content block 时会写成 mime_type(下划线)。"""
    payload = _jpeg()
    part = _ui_image_part(payload)
    part["mime_type"] = part.pop("mimeType")
    state = {"messages": [HumanMessage(content=[part], id="u1")]}

    ids = _ids_in(ingest_uploads(state)["messages"][1].content)
    assert len(ids) == 1


def test_传PDF时引导去资料归档面板而不是让人转成JPG() -> None:
    """上传按钮上明写着「Upload PDF or Image」,所以用户真的会传 PDF。

    聊天窗口当场看图仍只认 DXF(PDF 图纸走「资料归档」面板,那里 PDF/DXF 都收)——
    所以这时候仍然要拒,但要给**准确的指路**:是图纸就去资料归档面板。
    说「请转成 JPG 或 PNG」是**错的** —— 他传 PDF 本来就是这个按钮允许的操作,
    那句话会让他以为自己搞错了。
    """
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "解析这张图"},
                    {"type": "file", "mimeType": "application/pdf", "data": "JVBERi0="},
                ],
                id="u1",
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "PDF" in rewritten.content
    assert "资料归档" in rewritten.content  # 指路到那个接 PDF 图纸的面板
    assert "转成 JPG 或 PNG" not in rewritten.content


def test_前端格式里不支持的图片类型仍会被拒() -> None:
    state = {"messages": [HumanMessage(content=[_ui_image_part(_jpeg(), "image/gif")], id="u1")]}
    assert "转成 JPG 或 PNG" in ingest_uploads(state)["messages"][1].content


# ---------------------------------------------------------------------------
# 方案 A:工友现场上传 DXF 图纸
#
# 关键约束:DXF 的浏览器 MIME 不可靠(常是空串或 application/octet-stream),
# 所以一律按文件名 .dxf 后缀认,不信 MIME。前端(override 后)发的是 file 块,与 PDF 同形。
# ---------------------------------------------------------------------------


def _dxf_bytes(tmp_path: Any) -> bytes:
    """就地造一张最小 DXF 的字节(复用 CAD 测试的造样工具)。"""
    from tests.unit._dxf_fixtures import make_plain_dxf

    path = tmp_path / "u.dxf"
    make_plain_dxf(path)
    return path.read_bytes()


def _ui_dxf_part(
    payload: bytes, filename: str = "首层平面图.dxf", mime: str = "image/vnd.dxf"
) -> dict[str, Any]:
    """agent-chat-ui(override 后)对 .dxf 产出的 file 块形状。data 是裸 base64。"""
    return {
        "type": "file",
        "mimeType": mime,
        "data": base64.b64encode(payload).decode("ascii"),
        "metadata": {"filename": filename},
    }


def test_上传的DXF被登记成图纸_消息带图纸编号(tmp_path) -> None:
    payload = _dxf_bytes(tmp_path)
    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "看看这张图有哪些图层"}, _ui_dxf_part(payload)],
                id="u1",
            )
        ]
    }

    updates = ingest_uploads(state)["messages"]
    assert isinstance(updates[0], RemoveMessage)
    rewritten = updates[1]
    assert isinstance(rewritten.content, str)
    assert "看看这张图有哪些图层" in rewritten.content
    assert "图纸编号" in rewritten.content  # 不是「照片编号」——cad 按这个词认

    ids = _ids_in(rewritten.content)
    assert len(ids) == 1
    # 真的登记成 DRAWING,且字节一致(cad 后面按编号解析)。
    assert artifacts.resolve(ids[0]).read_bytes() == payload
    assert artifacts.read_meta(ids[0])["kind"] == "DRAWING"


def test_DXF按文件名认_MIME是octet_stream也收(tmp_path) -> None:
    """浏览器对 .dxf 常给 application/octet-stream —— 只要文件名是 .dxf 就得收。"""
    payload = _dxf_bytes(tmp_path)
    part = _ui_dxf_part(payload, filename="结构布置图.dxf", mime="application/octet-stream")
    state = {"messages": [HumanMessage(content=[part], id="u1")]}

    rewritten = ingest_uploads(state)["messages"][1]
    ids = _ids_in(rewritten.content)
    assert len(ids) == 1
    assert artifacts.read_meta(ids[0])["kind"] == "DRAWING"


def test_非DXF的file块不被误当图纸(tmp_path) -> None:
    """文件名不是 .dxf、MIME 也不含 dxf 的 file 块,绝不能被误登记成图纸。"""
    part = {
        "type": "file",
        "mimeType": "application/octet-stream",
        "data": base64.b64encode(b"just some bytes").decode("ascii"),
        "metadata": {"filename": "notes.bin"},
    }
    state = {"messages": [HumanMessage(content=[{"type": "text", "text": "看看"}, part], id="u1")]}
    # 既不是图纸也不是 PDF/图片 → 没有附件被登记 → 原样放行(不改写)。
    assert ingest_uploads(state) == {}


def test_超大DXF在入口就被挡(tmp_path, monkeypatch) -> None:
    """把上限压到极小,任何 DXF 都算超大 —— 入口就拒,并给中文提示,不静默登记。"""
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_DRAWING_MAX_MB", "0.000001")
    get_settings.cache_clear()

    payload = _dxf_bytes(tmp_path)
    state = {
        "messages": [
            HumanMessage(content=[{"type": "text", "text": "看图"}, _ui_dxf_part(payload)], id="u1")
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "太大" in rewritten.content
    assert _ids_in(rewritten.content) == []  # 超大不登记,不留编号
    assert "看图" in rewritten.content


def test_只传图纸没文字也给一句话(tmp_path) -> None:
    state = {"messages": [HumanMessage(content=[_ui_dxf_part(_dxf_bytes(tmp_path))], id="u1")]}
    rewritten = ingest_uploads(state)["messages"][1]
    assert "图纸" in rewritten.content
    assert len(_ids_in(rewritten.content)) == 1


def test_超大照片在入口就被挡(monkeypatch) -> None:
    """照 DXF 的写法把上限压到极小 —— 入口就拒、给中文提示、不静默登记。

    这条闸 2026-08-15 才补上(W7 §1.10 记录的现存 bug):此前只有图纸分支查上限,
    照片分支裸奔,超大图会被原样登记落盘,直到 Safety 工具解码才在链路深处翻车。
    """
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_PHOTO_MAX_MB", "0.000001")
    get_settings.cache_clear()

    state = {
        "messages": [
            HumanMessage(
                content=[{"type": "text", "text": "查安全隐患"}, _image_part(_jpeg())], id="u1"
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "太大" in rewritten.content
    assert "照片" in rewritten.content, "提示要说的是照片,不许错拿图纸那句"
    assert _ids_in(rewritten.content) == []  # 超大不登记,不留编号
    assert "查安全隐患" in rewritten.content


def test_正常大小的照片不受照片上限影响() -> None:
    """默认上限 10MB,几 KB 的测试图必须照常登记 —— 新闸不许误伤正常路径。"""
    state = {
        "messages": [
            HumanMessage(content=[{"type": "text", "text": "看看"}, _image_part(_jpeg())], id="u1")
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    ids = _ids_in(rewritten.content)
    assert len(ids) == 1
    assert "太大" not in rewritten.content
    assert artifacts.resolve(ids[0]).is_file()
