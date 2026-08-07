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


def test_最后一条不是用户消息时不动() -> None:
    """只处理最后一条用户消息 —— 更早的在它们自己那轮已经改写过了,
    重复处理会把同一张图反复登记,artifacts 目录白白膨胀。"""
    state = {
        "messages": [
            HumanMessage(content=[_image_part(_jpeg())], id="u1"),
            AIMessage(content="好的", id="a1"),
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


def test_传PDF时给出准确的话而不是让人转成JPG() -> None:
    """上传按钮上明写着「Upload PDF or Image」,所以用户真的会传 PDF。

    看规范文档是 knowledge Agent 的活(还没接)。这时候说「请转成 JPG 或 PNG」
    是**错的** —— 他传 PDF 本来就是这个按钮允许的操作,那句话会让他以为自己搞错了。
    """
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "看看这个规范"},
                    {"type": "file", "mimeType": "application/pdf", "data": "JVBERi0="},
                ],
                id="u1",
            )
        ]
    }

    rewritten = ingest_uploads(state)["messages"][1]
    assert "PDF" in rewritten.content
    assert "还没做好" in rewritten.content
    assert "转成 JPG 或 PNG" not in rewritten.content


def test_前端格式里不支持的图片类型仍会被拒() -> None:
    state = {"messages": [HumanMessage(content=[_ui_image_part(_jpeg(), "image/gif")], id="u1")]}
    assert "转成 JPG 或 PNG" in ingest_uploads(state)["messages"][1].content
