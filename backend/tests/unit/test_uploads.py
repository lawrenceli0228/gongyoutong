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
from gyt.core.uploads import (
    _DRAWING_TOO_LARGE_HINT,
    _PDF_HINT,
    _PHOTO_TOO_LARGE_HINT,
    _UNSUPPORTED_HINT,
    ATTACHMENT_BLOCK_TYPE,
    EXT_BY_MIME,
    OUTCOME_DRAWING,
    OUTCOME_DRAWING_TOO_LARGE,
    OUTCOME_PDF,
    OUTCOME_PHOTO,
    OUTCOME_PHOTO_TOO_LARGE,
    OUTCOME_UNSUPPORTED,
    _decode_dxf_part,
    _decode_image_part,
    classify_attachment,
    ingest_uploads,
)


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


# ---------------------------------------------------------------------------
# 直传:附件先换成编号,消息里只带**编号块**(2026-08-21)
#
# 🔴 为什么会多出这条路(完整推演在 core/uploads.py 那节注释与 upload_api.py 头注):
#    老路是「base64 随消息发上来、本层再改写成编号」,而改写发生在 pre_model_hook
#    (第 9 步),那条带 base64 的 HumanMessage **第 8 步就已经提交进检查点了** ——
#    RemoveMessage 改得了以后的状态,改不掉已经落盘的那一份,于是每张照片都在某个
#    检查点里留一份**永久**拷贝。线上实测 11 条会话 = langgraph 内存库 1.1 GB
#    (机器一共 1966 MB),一条**零载荷的 404** 都要 12.7 秒。
#
# 这一节盯的是 `_rewrite` 里新增的 `gyt_attachment` 分支。**一律走 ingest_uploads 这条
# 真路径**(不直接调 `_rewrite`),理由同本文件其余用例:私有函数的形状可以改,
# hook 的入口不能改,而线上真正被调用的是后者。
#
# ⚠️ 拼人话的活**还在这一层**,没有搬去前端。所以下面一律拿 uploads.py 里那四个
#    `_*_HINT` 常量去比,**测试里不许手抄字符串** —— 手抄的话改文案时两边各绿各的,
#    而 `lang-lib.ts` 的 userTypedText() 正是靠数这几句里的简体字判语种
#    (`_PDF_HINT` 一句就投 28 张票)。
# ---------------------------------------------------------------------------

DIRECT_PHOTO_ID = "a" * 32
DIRECT_DRAWING_ID = "b" * 32
"""两个长相合法的编号。**这一层不查产物在不在** —— 登记早在 upload_api 那一步做完了,
这里只负责把编号拼进消息,所以用字面量比登记一份真产物更贴近它的职责。
(要验"编号真能解回同一张图"的那条另有一例,见 `test_直传的照片编号原样进消息`。)"""

_BYTES_PER_MB = 1024 * 1024

_OTHER_HINTS = {
    OUTCOME_PDF: _PDF_HINT,
    OUTCOME_UNSUPPORTED: _UNSUPPORTED_HINT,
    OUTCOME_PHOTO_TOO_LARGE: _PHOTO_TOO_LARGE_HINT,
    OUTCOME_DRAWING_TOO_LARGE: _DRAWING_TOO_LARGE_HINT,
}
"""四种「这张不能用」各自那句话。拿它做**互斥**断言:说了 PDF 那句就不许同时说
"请转成 JPG",否则工友会以为自己两件事都做错了。"""


def _编号块(outcome: str, artifact_id: str | None = None) -> dict[str, Any]:
    """前端直传完塞进消息的**编号块**。

    形状的唯一真相有两处:`type` 在 `core/uploads.ATTACHMENT_BLOCK_TYPE`,
    `outcome` / `artifact_id` 在 `upload_api.py` 的响应契约。这里两处都从源码取,
    不写字面量 —— 改名等于改对外 API,写死的话前端改了这儿还是绿的。
    """
    return {"type": ATTACHMENT_BLOCK_TYPE, "outcome": outcome, "artifact_id": artifact_id}


def _改写(*parts: Any) -> str:
    """把若干 content 块塞进一条用户消息,跑一遍 hook,返回改写后的正文。"""
    state = {"messages": [HumanMessage(content=list(parts), id="u1")]}
    rewritten = ingest_uploads(state)["messages"][1]
    assert isinstance(rewritten.content, str), "改写后必须是纯文本,含结构块就会让文本档 400"
    return rewritten.content


def _超上限的字节数(哪一档: str) -> int:
    """比某一档上限多一个字节。

    **拿 settings 算,不写死 10 / 100** —— 上限是配置项,写死的话改配置时这两条
    用例会静默地变成"其实没超",而断言看起来还是绿的。

    ⚠️ 这里能大大方方造几百兆而不花一分内存,是因为 `classify_attachment` 收的是
       **字节数**而不是字节本身(它的 docstring 明写了这个取舍:判定用不到内容,
       而图纸上限是 100 MB,多传一份进来纯属浪费)。
    """
    from gyt.config import get_settings

    settings = get_settings()
    上限 = settings.photo_max_mb if 哪一档 == "photo" else settings.drawing_max_mb
    return int(上限 * _BYTES_PER_MB) + 1


def test_直传的照片编号原样进消息() -> None:
    """核心路径甲:编号块 → 「(照片编号:…)」,而且那个编号真能解回同一张图。

    这条特意登记一份**真产物**再把编号发进来,验的是两条泳道接不接得上:
    upload_api 落盘用的是 `artifacts.register`,safety 取图用的是同一个编号 ——
    中间这层把编号原样传下去这件事,只有拿真编号走一遍才看得出来。
    """
    payload = _jpeg()
    photo_id = artifacts.register(payload, kind=artifacts.ArtifactKind.PHOTO, original_name="a.jpg")

    content = _改写({"type": "text", "text": "查安全隐患"}, _编号块(OUTCOME_PHOTO, photo_id))

    assert "查安全隐患" in content
    assert "照片编号" in content
    assert _ids_in(content) == [photo_id]
    assert artifacts.resolve(photo_id).read_bytes() == payload


def test_直传的图纸编号拼的是图纸编号那个词() -> None:
    """cad/prompt.md 按「图纸编号」这个词把 id 递给工具,safety 认的是「照片编号」——
    串了的表现是图纸被当照片送去识图(或反过来),而两边都不报错。"""
    content = _改写(
        {"type": "text", "text": "看看有哪些图层"}, _编号块(OUTCOME_DRAWING, DIRECT_DRAWING_ID)
    )

    assert "图纸编号" in content
    assert "照片编号" not in content
    assert _ids_in(content) == [DIRECT_DRAWING_ID]


@pytest.mark.parametrize(
    ("outcome", "该说的话"),
    [
        (OUTCOME_PDF, _PDF_HINT),
        (OUTCOME_UNSUPPORTED, _UNSUPPORTED_HINT),
        (OUTCOME_PHOTO_TOO_LARGE, _PHOTO_TOO_LARGE_HINT),
        (OUTCOME_DRAWING_TOO_LARGE, _DRAWING_TOO_LARGE_HINT),
    ],
)
def test_四种不能用的结局各自那句话原样出现(outcome: str, 该说的话: str) -> None:
    """四句提示各归各的,**而且互斥**。

    上传端点对这四种一律回 200 + 一个 outcome、`user_msg` 是空的(它那边有用例钉着),
    人话在这一层拼 —— 所以这四句要是没出来,工友那边就是**一个字都没有**:
    他传了东西、对话里只字未提,会以为传上去了然后等一个永远不来的答复。

    互斥那一半同样要紧:传 PDF 的人同时看到"请转成 JPG 或 PNG"会以为自己两件事
    都做错了,而传 PDF 本来就是那个按钮允许的操作。
    """
    content = _改写({"type": "text", "text": "看看"}, _编号块(outcome))

    assert 该说的话 in content
    assert "看看" in content, "原来的文字不能丢"
    assert _ids_in(content) == [], "没收下的附件不许留编号"
    for 别的结局, 别的话 in _OTHER_HINTS.items():
        if 别的结局 != outcome:
            assert 别的话 not in content, f"{outcome} 顺带说了 {别的结局} 那句话"


@pytest.mark.parametrize(
    ("说法", "编号"),
    [
        ("短一位", "a" * 31),
        ("长一位", "a" * 33),
        ("掺了非 hex 的字母", "z" * 32),
        ("掺了标点", "a" * 31 + "-"),
        ("空串", ""),
        ("压根没这个键", None),
        ("一段来路不明的文本", "../../etc/passwd"),
    ],
)
def test_编号长得不对就按格式不支持处理(说法: str, 编号: str | None) -> None:
    """🔴 **这个值是客户端发上来的,必须校验**(理由在 `_ARTIFACT_ID_RE` 的 docstring)。

    落地表现不是漏洞(`artifacts.resolve` 自己防路径穿越),而是**脏数据进对话**:
    模型看见一个假编号,拿它去调工具,然后如实报"找不到那张照片",
    而工友明明刚传过 —— 他只会觉得这系统在瞎说。

    所以这里当成「这张没传上去」处理,**绝不把那段来路不明的文本拼进对话**。
    下面第二条断言钉的就是后半句。
    """
    content = _改写({"type": "text", "text": "看看"}, _编号块(OUTCOME_PHOTO, 编号))

    assert _UNSUPPORTED_HINT in content
    assert "看看" in content
    assert _ids_in(content) == []
    if 编号:
        assert 编号 not in content, f"{说法}:来路不明的编号被原样拼进了对话"


def test_大写十六进制的编号是被归一化而不是被拒() -> None:
    """⚠️ 这条记的是**现状与它的理由**,不是"应该拒掉"。

    `_rewrite` 在校验之前先 `.strip().lower()`,所以 `A…F` 这种写法会被**收下并转成小写**。
    这是对的:`artifacts.register` 出的编号本来就是小写 hex,大写只是同一个值的另一种
    写法,拒掉等于让"复制编号时手滑按了大写锁"变成一次莫名其妙的失败。

    写成用例是为了让下一个人看见这是**决定**:哪天有人为了"更严格"把 `.lower()` 拿掉,
    这条会红,而不是等到线上有人抱怨编号明明是对的却传不上去。
    """
    content = _改写(_编号块(OUTCOME_PHOTO, DIRECT_PHOTO_ID.upper()))

    assert _ids_in(content) == [DIRECT_PHOTO_ID], "大写编号应当被归一化成小写后收下"
    assert _UNSUPPORTED_HINT not in content


@pytest.mark.parametrize(
    ("说法", "outcome"),
    [
        ("凭空冒出来的词", "banana"),
        ("拼错了一个字母", "photo_too_larg"),
        ("大小写不对", "PHOTO"),
        ("空串", ""),
        ("压根没这个键", None),
    ],
)
def test_认不出的结局也归格式不支持而不是静默丢掉(说法: str, outcome: str | None) -> None:
    """🔴 **不许静默丢掉。**

    静默丢的表现是:工友传了东西,对话里**一个字都没提**。他会以为传上去了,
    然后等一个永远不来的答复 —— 这比报个错难查得多,因为没有任何一侧有信号。

    「大小写不对」那条单列:`artifact_id` 会被 `.lower()` 归一化,而 `outcome` **不会**,
    所以它是大小写敏感的。前端哪天把 outcome 大写发上来,表现就是所有附件一律
    「格式不支持」—— 这条用例把这个不对称写在明面上。
    """
    content = _改写({"type": "text", "text": "看看"}, _编号块(outcome, DIRECT_PHOTO_ID))

    assert _UNSUPPORTED_HINT in content, f"{说法}被静默丢掉了"
    assert "看看" in content


def test_文本在前编号块在后_哪怕消息里的顺序是反的() -> None:
    """拼出来的顺序是契约的一部分:supervisor 读的是这句话,而「查安全隐患」这种
    意图词得排在编号前面,否则第一眼看到的是一串十六进制。

    刻意把编号块放在文本块**前面**发进去 —— `_rewrite` 是先收齐 texts 再拼编号,
    所以顺序由拼装决定、不由块的先后决定。哪天有人改成"按块的顺序拼",这条会红。
    """
    content = _改写(_编号块(OUTCOME_PHOTO, DIRECT_PHOTO_ID), {"type": "text", "text": "查安全隐患"})

    assert content.index("查安全隐患") < content.index(DIRECT_PHOTO_ID)


def test_只传附件不打字时也给一句开场白() -> None:
    """用户可能只传图不打字。空正文会让 supervisor 无从判断该派给谁。

    ⚠️ 这句开场白**是后端编的**,不是用户打的字 —— `lang-lib.ts` 的 `userTypedText()`
       靠"头段必须以换行结尾"把它认出来并排除掉(没换行 = 后端编的那句)。
       所以这里连**没有前导换行**这件事一起钉住:哪天有人给它补一个 `\\n`,
       前端会把这句话当成用户打的字去投简体票,而传附件不打字的港人本该落默认语种。
    """
    content = _改写(_编号块(OUTCOME_PHOTO, DIRECT_PHOTO_ID))

    assert content.startswith("看看这张照片。")
    assert "\n" not in content, "开场白那一档不许有前导换行(它是判语种的判据)"


def test_编号块与老的image块进同一批照片编号() -> None:
    """🔴 「两条路殊途同归到同一批 photo_ids」是 uploads.py 那句注释的守卫。

    老路(base64 进消息)一行都没删:老客户端、别的调用方、以及直传接口挂掉时的兜底
    都还走它。所以一条消息里同时出现两种块是**真会发生**的形态(比如页面没刷新、
    前端半新半旧)。

    两条路要是各拼各的,消息里会出现**两句「照片编号:」** —— safety 的提示词按
    那个词取编号,第二句大概率被无视,于是"我传了两张,它只看了一张",而没有任何报错。
    """
    payload = _jpeg((30, 30, 200))

    content = _改写(
        {"type": "text", "text": "这两张都看下"},
        _编号块(OUTCOME_PHOTO, DIRECT_PHOTO_ID),
        _image_part(payload),
    )

    assert content.count("照片编号") == 1, "两条路各拼了一句 —— 第二张会被静默无视"
    ids = _ids_in(content)
    assert len(ids) == 2
    assert ids[0] == DIRECT_PHOTO_ID, "直传那张排在前面(它在 content 里就在前面)"
    assert artifacts.resolve(ids[1]).read_bytes() == payload, "老路那张也得真登记"


@pytest.mark.parametrize(
    ("说法", "filename", "mime", "该判成", "落盘名"),
    [
        ("一张 JPG", "", "image/jpeg", OUTCOME_PHOTO, "upload.jpg"),
        ("一张 DXF(浏览器没给类型)", "首层平面图.dxf", "", OUTCOME_DRAWING, "首层平面图.dxf"),
        ("一份 PDF", "规范.pdf", "application/pdf", OUTCOME_PDF, None),
        ("一张 GIF", "动图.gif", "image/gif", OUTCOME_UNSUPPORTED, None),
    ],
)
def test_分类的四种常规输入(
    说法: str, filename: str, mime: str, 该判成: str, 落盘名: str | None
) -> None:
    """`classify_attachment` 是纯函数(不碰磁盘),直接调。

    ⚠️ 判定**顺序不能换**(它的 docstring 写明了):DXF 先判且只信后缀 → PDF 次之
    → 再按 mime 认图片 → 都不是才算格式不支持。把 PDF 挪到 DXF 前面的表现是
    一份 `.dxf` 却被浏览器报成 pdf 的图纸被劝去"资料归档面板",而它本该当场能看。
    """
    verdict = classify_attachment(1, filename=filename, mime=mime)

    assert verdict.outcome == 该判成, 说法
    assert verdict.register_name == 落盘名, f"{说法}:落盘名不对"


@pytest.mark.parametrize(
    ("说法", "哪一档", "filename", "mime", "该判成"),
    [
        ("照片超上限", "photo", "", "image/jpeg", OUTCOME_PHOTO_TOO_LARGE),
        ("图纸超上限", "drawing", "首层平面图.dxf", "", OUTCOME_DRAWING_TOO_LARGE),
    ],
)
def test_分类的两种超上限(说法: str, 哪一档: str, filename: str, mime: str, 该判成: str) -> None:
    """两条上限各判各的(照片 10 MB / 图纸 100 MB,唯一入口是 config.py)。

    ⚠️ 别把这两个结局与 `upload_api` 的 413 混为一谈:那条端点另有一道**硬上限**
    (= 两条里大的那条),只管"别把内存撑爆";这两个结局管的是"这张能不能用",
    而且必须走 200 —— 因为只有走到这一层,上面那两句人话才拼得出来。
    """
    verdict = classify_attachment(_超上限的字节数(哪一档), filename=filename, mime=mime)

    assert verdict.outcome == 该判成, 说法
    assert verdict.register_name is None, f"{说法}:没收下就不该有落盘名"


def test_分类判据与老路那两个解码函数给出同一结论(tmp_path) -> None:
    """🔴 `classify_attachment` 的 docstring 明写:「判据逐条对着 `_decode_dxf_part` /
    `_decode_image_part` 抄的,**两条路必须给出同一个结论**」。

    漂开的表现是**同一张图「点上传按钮」和「走旧路」结果不同** —— 比如新路收下了、
    老路说格式不支持,而两边各自的用例都绿。这条把那句注释变成可执行的对照。
    """
    payload = _jpeg()
    dxf = _dxf_bytes(tmp_path)

    # 甲、一张 JPG:老路解得出扩展名,新路判成 photo,落盘名带的是同一个扩展名
    解码结果 = _decode_image_part(_ui_image_part(payload))
    assert 解码结果 is not None
    照片 = classify_attachment(len(payload), filename="现场.jpg", mime="image/jpeg")
    assert 照片.outcome == OUTCOME_PHOTO
    assert 照片.register_name == f"upload{解码结果[1]}"

    # 乙、一张 GIF:老路解不出(None),新路判"格式不支持"—— 两条都拒,而且是同一个理由
    assert _decode_image_part(_ui_image_part(payload, "image/gif")) is None
    assert classify_attachment(1, filename="", mime="image/gif").outcome == OUTCOME_UNSUPPORTED

    # 丙、一张 DXF:MIME 不可靠,两条路都只信 .dxf 后缀,连落盘名都得一样
    解出的图纸 = _decode_dxf_part(_ui_dxf_part(dxf, mime="application/octet-stream"))
    assert 解出的图纸 is not None
    图纸 = classify_attachment(len(dxf), filename="首层平面图.dxf", mime="application/octet-stream")
    assert 图纸.outcome == OUTCOME_DRAWING
    assert 图纸.register_name == 解出的图纸[1] == "首层平面图.dxf"

    # 丁、拿不到文件名时两条路的兜底名也必须一样,否则落盘就没有 .dxf 后缀,
    #     而 cad 按后缀判格式 —— 一条「传上去了但永远打不开」的静默死路。
    兜底 = _decode_dxf_part(_ui_dxf_part(dxf, filename="", mime="image/vnd.dxf"))
    assert 兜底 is not None
    assert classify_attachment(1, filename="", mime="image/vnd.dxf").register_name == 兜底[1]
