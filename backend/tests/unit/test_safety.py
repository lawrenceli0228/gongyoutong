"""Safety Agent 的单元测试。

覆盖三层:
  1. 纯函数(_extract_json_object / _as_violation_list / _summarize)—— 输入输出直给
  2. 工具 analyze_site_photo 的每一条失败出口与成功路径 —— 全程不联网
  3. 组装与登记(build_safety_agent / AGENT_REGISTRY)

有几条测试是**锁行为**用的,不是凑覆盖率,删之前先读它们的 docstring:
  · test_受控词表外的违规项原样透传_不做过滤
  · test_label非法时不当作工具失败
  · test_视觉调用传了max_retries为0
这三条锁住的都是「刻意为之、但看起来像 bug」的设计,没有测试守着,
下一个人很容易顺手"修好"它们,而修好之后坏掉的东西要到评测或账单上才看得见。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from gyt.agents.safety import SAFETY_AGENT_NAME, build_safety_agent
from gyt.agents.safety.tools import (
    CACHE_EXTRA,
    MIME_BY_EXT,
    VISION_MAX_ATTEMPTS,
    _as_violation_list,
    _extract_json_object,
    _summarize,
    analyze_site_photo,
)
from gyt.config import ALLOWED_IMAGE_EXT
from gyt.core import artifacts, llm
from gyt.core.artifacts import ArtifactKind
from gyt.core.errors import ErrorCode
from gyt.core.llm import LLMCallError


def _make_jpeg(
    width: int = 64, height: int = 48, color: tuple[int, int, int] = (110, 130, 90)
) -> bytes:
    """造一张**真的**小 JPEG。

    以前这里是一串手写的 JPEG 魔数(b"\xff\xd8\xff\xe0..."),因为那时工具只看扩展名、
    不解码图片。加了 _prepare_image 的真伪校验之后那串字节会被正确地判成损坏 ——
    夹具必须跟着变成真图片,否则测的就不是业务逻辑而是"假数据被拦住了"。
    (它被拦住这件事本身,由 test_改名成jpg的非图片文件会被挡下 单独守着。)
    """
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


FAKE_JPEG: bytes = _make_jpeg()


def _register_photo(data: bytes = FAKE_JPEG, name: str = "photo.jpg") -> str:
    """在本用例独占的 tmp 数据目录里登记一张假照片,返回 artifact_id。"""
    return artifacts.register(data, kind=ArtifactKind.PHOTO, original_name=name)


class _RecordingModel:
    """假模型。只用来被 llm.ainvoke 接收,本身不会被调用(ainvoke 也被打了桩)。"""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _patch_llm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: str | BaseException,
) -> dict[str, Any]:
    """把 get_chat_model 与 ainvoke 双双换成假的,并把调用参数录下来。

    返回的 dict 会被就地填充(调用发生在测试体之后),字段:
        model_kwargs: get_chat_model 收到的 (purpose, overrides)
        messages:     ainvoke 收到的消息列表
        cache_extra:  ainvoke 收到的 cache_extra
    """
    recorded: dict[str, Any] = {}

    def fake_get_chat_model(purpose: str = "text", **overrides: Any) -> _RecordingModel:
        recorded["purpose"] = purpose
        recorded["overrides"] = overrides
        return _RecordingModel(**overrides)

    async def fake_ainvoke(
        model: Any,
        messages: Any,
        *,
        cache_extra: str = "",
        is_usable: Any = None,
        max_attempts: int | None = None,
    ) -> AIMessage:
        recorded["model"] = model
        recorded["messages"] = messages
        recorded["cache_extra"] = cache_extra
        recorded["max_attempts"] = max_attempts
        # 把闸门函数也录下来:调用方**有没有传**这道闸,本身就是要断言的行为
        # (不传就会把解析不出的回答写进缓存,那句「请再试一次」永远不会成功)。
        recorded["is_usable"] = is_usable
        if isinstance(reply, BaseException):
            raise reply
        return AIMessage(content=reply)

    monkeypatch.setattr(llm, "get_chat_model", fake_get_chat_model)
    monkeypatch.setattr(llm, "ainvoke", fake_ainvoke)
    return recorded


async def _call(artifact_id: str) -> dict[str, Any]:
    """走真实的工具调用入口(BaseTool.ainvoke),而不是绕过装饰器直接调函数体。

    刻意不走 .coroutine/.func 捷径:@tool + @tool_guard 这两层本身就是被测对象的一部分,
    绕过去测等于没测到「异常会不会变成信封」这条最重要的保证。
    """
    result = await analyze_site_photo.ainvoke({"artifact_id": artifact_id})
    assert isinstance(result, dict), f"工具应当返回信封 dict,实际拿到 {type(result).__name__}"
    return result


# ---------------------------------------------------------------------------
# 一、纯函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"label":"compliant","violations":[]}', {"label": "compliant", "violations": []}),
        # 模型很爱套 ```json 外壳,提示词里已经要求不要加,但不能只靠它守规矩
        (
            '```json\n{"label":"not_site","violations":[]}\n```',
            {"label": "not_site", "violations": []},
        ),
        ('```\n{"label":"not_site","violations":[]}\n```', {"label": "not_site", "violations": []}),
        # 前后夹了说明文字 —— 靠正则抠出最外层 {...}
        (
            '好的,判断如下:\n{"label":"violation","violations":["未戴安全帽"]}\n以上。',
            {"label": "violation", "violations": ["未戴安全帽"]},
        ),
    ],
)
def test_能从各种包装里抠出JSON对象(raw: str, expected: dict[str, Any]) -> None:
    parsed = _extract_json_object(raw)
    assert parsed is not None
    for key, value in expected.items():
        assert parsed[key] == value


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "这张照片看起来没什么问题",  # 纯文字,压根没 JSON
        "{不是合法的 JSON}",
        '["未戴安全帽"]',  # 顶层是数组:取不到 label,当解析失败处理
    ],
)
def test_抠不出JSON对象时返回None(raw: str) -> None:
    assert _extract_json_object(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ([], []),
        (["未戴安全帽"], ["未戴安全帽"]),
        (["未戴安全帽", "未穿反光衣"], ["未戴安全帽", "未穿反光衣"]),
        # 模型有时不给数组,给一个用分隔符连起来的串。中英文标点都要接住。
        ("未戴安全帽;未穿反光衣", ["未戴安全帽", "未穿反光衣"]),
        ("未戴安全帽;未穿反光衣", ["未戴安全帽", "未穿反光衣"]),
        ("未戴安全帽、未穿反光衣", ["未戴安全帽", "未穿反光衣"]),
        ("未戴安全帽", ["未戴安全帽"]),
        ("  ", []),
        (["  ", "未戴安全帽"], ["未戴安全帽"]),  # 空白项要丢掉
        # 模型偶尔会把这个字段填成完全不搭的类型。不能让它一路穿到下游变成
        # TypeError —— 归一成字符串,后面 scorers 自然会判错并报出「不在受控词表里」。
        (123, ["123"]),
        ({"a": 1}, ["{'a': 1}"]),
    ],
)
def test_违规项归一化(raw: Any, expected: list[str]) -> None:
    assert _as_violation_list(raw) == expected


def test_一句话结论覆盖三态() -> None:
    assert "不像工地" in _summarize("not_site", [])
    assert "没看到明显" in _summarize("compliant", [])
    assert "2 处" in _summarize("violation", ["未戴安全帽", "未穿反光衣"])
    # label 不在三态里时也要给一句话,不能返回空串让 Agent 无话可说
    assert _summarize("", []).strip()


def test_MIME表覆盖图片白名单() -> None:
    """白名单加了新格式却忘了配 MIME,会在构造 data URI 时才 KeyError ——

    那是**演示当天**才炸的时点,前面所有校验都会放行。tools.py 里有一条 import 期
    断言挡着,这条测试是它的显式副本,免得有人把那条断言当死代码删掉。
    """
    assert not ALLOWED_IMAGE_EXT - MIME_BY_EXT.keys()


# ---------------------------------------------------------------------------
# 二、工具的失败出口
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["", "   ", "not-a-hex", "abc123", "F" * 32, "a" * 31, "a" * 33])
async def test_编号不合法直接拒绝(bad_id: str) -> None:
    """32 位小写 hex 之外一律拒,含大写(ARTIFACT_ID_RE 只认小写)。"""
    result = await _call(bad_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.INVALID_INPUT.value
    assert "编号" in result["user_msg"]


async def test_产物不存在时返回没找到() -> None:
    result = await _call("0" * 32)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.NOT_FOUND.value


async def test_不是图片格式时明确拒绝() -> None:
    """.txt 不在图片白名单里。注意它**能**被登记成产物 —— artifacts 只管落盘不管语义,
    所以把关必须发生在这里,否则会把一份文本 base64 之后发给视觉模型。"""
    artifact_id = artifacts.register(b"hello", kind=ArtifactKind.DOCUMENT, original_name="a.txt")
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.FILE_UNSUPPORTED.value


async def test_空文件当作损坏处理() -> None:
    artifact_id = _register_photo(data=b"")
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.FILE_CORRUPT.value


async def test_文件读不出来时当作损坏处理(monkeypatch: pytest.MonkeyPatch) -> None:
    """产物登记过、路径也在,但磁盘读失败(坏道 / 权限 / 挂载点掉了)。

    只对图片本身下手,放行 .json 元数据 —— 否则 artifacts.resolve() 会先炸在
    读 sidecar 上,测到的就变成另一条路径了。
    """
    artifact_id = _register_photo()
    real_read_bytes = Path.read_bytes

    def selective_boom(self: Path) -> bytes:
        if self.suffix == ".jpg":
            raise OSError("模拟磁盘坏道")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", selective_boom)
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.FILE_CORRUPT.value


async def test_超过体积上限时给出可操作的提示(monkeypatch: pytest.MonkeyPatch) -> None:
    """报错要告诉工人**怎么办**(用相册的压缩后发送),而不是只说"太大了"。"""
    monkeypatch.setenv("GYT_PHOTO_MAX_MB", "0.001")  # 1KB 上限
    from gyt.config import get_settings

    get_settings.cache_clear()
    artifact_id = _register_photo(data=b"\xff\xd8\xff" + b"\x00" * 4096)
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.FILE_TOO_LARGE.value
    assert "压缩" in result["user_msg"]


async def test_模型调用失败时原样转述中文文案(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLMCallError 的 user_msg 已经是人话,不要在这里另编一句盖掉它。"""
    boom = LLMCallError("现在用的人太多,正在排队,请过一会儿再试。", ErrorCode.RATE_LIMITED)
    _patch_llm(monkeypatch, reply=boom)
    artifact_id = _register_photo()
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.RATE_LIMITED.value
    assert result["user_msg"] == "现在用的人太多,正在排队,请过一会儿再试。"


async def test_模型输出不是JSON时算工具失败(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, reply="我觉得这张照片挺好的,没什么问题。")
    artifact_id = _register_photo()
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.UPSTREAM_ERROR.value
    # detail 只进日志,绝不能出现在返回值里(会连原始输出一起泄给 LLM 和用户)
    assert "detail" not in result


async def test_Kimi密钥没配时把可操作的中文原样告诉用户(monkeypatch: pytest.MonkeyPatch) -> None:
    """**演示日最可能发生的一类故障**,冒烟时真踩到过。

    Agent 本体走 text 档,建图只校验 DeepSeek 的 Key —— Kimi 的 Key 要到真调工具
    那一刻才检查。所以「图起得来」不代表「识图能用」,这条路径必须自己把话说清楚。

    MissingAPIKeyError 的消息本身就是一句可操作的中文(指到 .env 的哪一行),
    要是让 tool_guard 兜底,就会变成 INTERNAL 的「系统开小差了」——
    演示当场没人知道该去填 Key。这条测试就是防这个退化的。
    """
    from gyt.core.llm import MissingAPIKeyError

    def no_key(*_args: Any, **_kwargs: Any) -> None:
        raise MissingAPIKeyError("Kimi(月之暗面)", "GYT_MOONSHOT_API_KEY")

    monkeypatch.setattr(llm, "get_chat_model", no_key)
    result = await _call(_register_photo())

    assert result["ok"] is False
    # 必须指到具体的环境变量名,而不是笼统的「系统开小差了」
    assert "GYT_MOONSHOT_API_KEY" in result["user_msg"]
    assert "系统开小差" not in result["user_msg"]


async def test_工具内部抛异常也会变成信封(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_guard 的兜底:任何漏网异常都不许向上抛给 Agent。"""

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("模拟一个没预料到的崩溃")

    monkeypatch.setattr(llm, "get_chat_model", explode)
    artifact_id = _register_photo()
    result = await _call(artifact_id)
    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.INTERNAL.value


# ---------------------------------------------------------------------------
# 三、成功路径与「刻意为之」的行为锁
# ---------------------------------------------------------------------------


async def test_成功路径返回三个字段(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        reply='{"label":"violation","violations":["未戴安全帽"],"note":"两人头部裸露"}',
    )
    artifact_id = _register_photo()
    result = await _call(artifact_id)
    assert result["ok"] is True
    assert result["data"] == {
        "label": "violation",
        "violations": ["未戴安全帽"],
        "note": "两人头部裸露",
    }
    assert "未戴安全帽" in result["user_msg"]


async def test_图片以base64_data_uri进消息(monkeypatch: pytest.MonkeyPatch) -> None:
    """月之暗面只认 base64 data URI,**不支持 http/https 图片链接**。

    同时钉住消息结构:system 放视觉提示词,human 是 [text, image_url] 两个 part。
    官方明确警告不要把这个数组序列化成字符串塞进 content。
    """
    recorded = _patch_llm(monkeypatch, reply='{"label":"compliant","violations":[],"note":""}')
    artifact_id = _register_photo(data=FAKE_JPEG, name="工地.jpg")
    await _call(artifact_id)

    messages = recorded["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "受控词表" not in messages[0].content  # 注释块被 load_prompt 剥掉了
    assert "未戴安全帽" in messages[0].content  # 但正文里的词表还在

    human = messages[1]
    assert isinstance(human, HumanMessage)
    assert isinstance(human.content, list), "content 必须是 part 数组,不能是序列化后的字符串"
    parts = {part["type"]: part for part in human.content}
    assert set(parts) == {"text", "image_url"}

    url = parts["image_url"]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == FAKE_JPEG


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.png", "image/png"),
        ("a.webp", "image/webp"),
    ],
)
async def test_每种支持的格式都给出正确MIME(
    monkeypatch: pytest.MonkeyPatch, name: str, mime: str
) -> None:
    recorded = _patch_llm(monkeypatch, reply='{"label":"compliant","violations":[],"note":""}')
    artifact_id = _register_photo(name=name)
    await _call(artifact_id)
    url = recorded["messages"][1].content[1]["image_url"]["url"]
    assert url.startswith(f"data:{mime};base64,")


async def test_视觉调用走vision档且传了max_retries为0(monkeypatch: pytest.MonkeyPatch) -> None:
    """TODO-10 的行为锁 —— 删掉 max_retries=0 会让账单悄悄翻 4 倍。

    路径乙外面已经有 _invoke_with_retry(1 + llm_max_retries 次),ChatOpenAI 自己
    还带一层 SDK 退避。两层叠乘,一次逻辑调用最坏发 4×4=16 个 HTTP 请求。
    这事**不会**报错、不会变慢到被发现,只会在月底账单上多一个零。
    必须在造模型时传,事后 model_copy 改是无效的(见 llm._for_direct_call)。
    """
    recorded = _patch_llm(monkeypatch, reply='{"label":"compliant","violations":[],"note":""}')
    await _call(_register_photo())
    assert recorded["purpose"] == "vision"
    assert recorded["overrides"] == {"max_retries": 0}


async def test_视觉调用带了防串味的缓存维度(monkeypatch: pytest.MonkeyPatch) -> None:
    """cache_extra 留空的话,同一张图被别的调用方问别的问题会撞进同一条缓存,
    工具会原样取走别人的答案 —— 零次网络调用,日志里只有一行 debug。"""
    recorded = _patch_llm(monkeypatch, reply='{"label":"compliant","violations":[],"note":""}')
    await _call(_register_photo())
    assert recorded["cache_extra"] == CACHE_EXTRA
    assert CACHE_EXTRA

    # 同时必须传写缓存前的闸门,否则解析不出的回答会被永久钉住(见
    # test_解析不出的回答不写缓存_重试真的能成功)。这里只断言"传了且判据正确",
    # 端到端效果由那条测试用真实 ainvoke 验。
    gate = recorded["is_usable"]
    assert gate is not None, "没传 is_usable,坏回答会进缓存"
    assert gate(AIMessage(content='{"label":"compliant","violations":[],"note":""}')) is True
    assert gate(AIMessage(content="这张照片挺好的")) is False


async def test_受控词表外的违规项原样透传_不做过滤(monkeypatch: pytest.MonkeyPatch) -> None:
    """**刻意不过滤**。模型答「未佩戴安全帽」(多一个"佩"字)时原样传出去。

    因为 scorers.score_safety 会算 unknown = got_items - VIOLATION_VOCAB,
    并在报告里写「不在受控词表里,提示词的输出约束没生效」—— 那正是我们要的诊断信号。
    在工具里悄悄纠正,评测就永远发现不了提示词坏了,而线上换模型时随时会复发。
    让错误可见,比让它消失更重要。
    """
    _patch_llm(
        monkeypatch,
        reply='{"label":"violation","violations":["未佩戴安全帽","戴了个草帽"],"note":""}',
    )
    result = await _call(_register_photo())
    assert result["ok"] is True
    assert result["data"]["violations"] == ["未佩戴安全帽", "戴了个草帽"]


async def test_label非法时不当作工具失败(monkeypatch: pytest.MonkeyPatch) -> None:
    """理由同上:label 非法是「模型没守格式」,不是「工具挂了」。

    两者修法完全不同 —— 前者改提示词,后者查代码。fail() 会把它归错类,
    让人对着工具代码找一个根本不在那儿的 bug。
    """
    _patch_llm(monkeypatch, reply='{"label":"有违规","violations":[],"note":"x"}')
    result = await _call(_register_photo())
    assert result["ok"] is True
    assert result["data"]["label"] == "有违规"


async def test_缺字段时补成空值而不是报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """下游(Agent 提示词、scorers)都按三个键取值,缺键会变成 KeyError/None 传染。"""
    _patch_llm(monkeypatch, reply='{"label":"compliant"}')
    result = await _call(_register_photo())
    assert result["ok"] is True
    assert result["data"] == {"label": "compliant", "violations": [], "note": ""}


# ---------------------------------------------------------------------------
# 四、组装与登记
# ---------------------------------------------------------------------------


def test_能组装出safety_agent() -> None:
    agent = build_safety_agent()
    assert agent.name == SAFETY_AGENT_NAME


def test_两份提示词都存在且非空() -> None:
    """prompt.md 给本体、vision_prompt.md 给工具内部那次视觉调用,少一份都跑不起来。"""
    from gyt.agents.safety.tools import SAFETY_DIR, VISION_PROMPT_FILENAME
    from gyt.core.base_agent import PROMPT_FILENAME, load_prompt

    for filename in (PROMPT_FILENAME, VISION_PROMPT_FILENAME):
        assert (Path(SAFETY_DIR) / filename).is_file()
        assert load_prompt(SAFETY_DIR, filename).strip()


def test_受控词表八项都写进了视觉提示词() -> None:
    """三处同源(eval/README.md、scorers.VIOLATION_VOCAB、vision_prompt.md)。

    对不上不会报错,只会让评测分数系统性偏低,而报告把矛头指向模型。
    这条测试是那个「三处一致」约定唯一的自动化守卫。
    """
    from eval.scorers import VIOLATION_VOCAB

    from gyt.agents.safety.tools import SAFETY_DIR, VISION_PROMPT_FILENAME
    from gyt.core.base_agent import load_prompt

    body = load_prompt(SAFETY_DIR, VISION_PROMPT_FILENAME)
    missing = {word for word in VIOLATION_VOCAB if word not in body}
    assert not missing, f"视觉提示词里缺了这些受控词:{sorted(missing)}"


def test_视觉提示词必须写明输出契约的三个字段名() -> None:
    """**这条锁的是一种静默漏报。**

    提示词是给非工程师队友改的。把 vision_prompt.md 里的 `violations`
    顺手写成 `items` 之后:模型照新契约输出 → tools.py 用 KEY_VIOLATIONS
    取到空 → label=violation 的照片返回 violations=[] 且 ok=True →
    Agent 按 prompt.md 的规定对工人说「这张照片里没看到明显的安全问题」。

    **一张明确判了违规的照片被讲成没问题,全程零报错。**
    加这条之前,把字段名改掉不会有任何一条测试转红。
    """
    from gyt.agents.safety.tools import OUTPUT_KEYS, SAFETY_DIR, VISION_PROMPT_FILENAME
    from gyt.core.base_agent import load_prompt

    body = load_prompt(SAFETY_DIR, VISION_PROMPT_FILENAME)
    missing = [key for key in OUTPUT_KEYS if f'"{key}"' not in body]
    assert not missing, (
        f"视觉提示词里没有逐字出现这些字段名:{missing}。"
        "模型会按提示词里写的名字输出,而 tools.py 按 OUTPUT_KEYS 取值,对不上就是静默漏报。"
    )


def test_safety已挂进登记表并带能力说明() -> None:
    from gyt.graph import AGENT_REGISTRY

    spec = next((s for s in AGENT_REGISTRY if s.name == SAFETY_AGENT_NAME), None)
    assert spec is not None, "safety 没挂进 AGENT_REGISTRY,Supervisor 永远派不到它"
    # summary 是 supervisor 判断「这活派给谁」的唯一依据(D18 路由门槛 0.90 靠它)
    assert "照片" in spec.summary
    # 「并给了照片编号」曾经写在 summary 里,supervisor 会把它读成路由前提 ——
    # 而 routing.csv 里 expected_agent=safety 的行原文都不带编号,于是全判错。
    # 缺编号的追问归 safety/prompt.md 管,不该在路由这一层重复把关。
    assert "并给了照片编号" not in spec.summary


def test_safety在路由评测集的合法取值里() -> None:
    """AGENT_REGISTRY 与 scorers.ROUTING_AGENTS 必须对得上,
    否则 routing.csv 里写 safety 会被判成非法取值,当场炸。"""
    from eval.scorers import ROUTING_AGENTS

    assert SAFETY_AGENT_NAME in ROUTING_AGENTS


# ---------------------------------------------------------------------------
# 五、走**真实** llm.ainvoke 的集成测试(上面那些把 ainvoke 整个打了桩)
#
# 上面的用例换掉的是 llm.ainvoke 本身,所以覆盖不到它内部的缓存与重试 ——
# 而那两件事恰恰是「坏了也完全看不出来」的类型:答案照出、测试照绿,
# 只是每次都在花钱、或者每次都在多睡 7 秒。这一节只换**模型**,不换 ainvoke。
# ---------------------------------------------------------------------------


class _ScriptedModel:
    """假模型:按剧本逐次返回或抛出,并数自己被调了几次。

    形状照抄 tests/unit/test_llm.py 里的假模型 —— llm.ainvoke 只需要
    model_name(算缓存键用)和 ainvoke 两样东西。
    """

    model_name = "fake-vision-model"

    def __init__(self, script: list[Any]) -> None:
        self._script = tuple(script)
        self.calls = 0

    async def ainvoke(self, messages: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        item = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_model_only(monkeypatch: pytest.MonkeyPatch, model: Any) -> None:
    """只换模型,保留真实的 llm.ainvoke(缓存 + 退避都照跑)。"""
    monkeypatch.setattr(llm, "get_chat_model", lambda *_a, **_k: model)


async def test_同一张照片第二次调用命中缓存不再请求模型(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存断了是**完全静默**的:答案照出、测试照绿,只是每次都在花钱。

    kimi-k3 的 cache hit 比 miss 便宜 10 倍($0.30 vs $3.00 / 1M),
    而演示时会反复对着同几张示例图跑。所以这条链路必须有测试数着
    「内层模型到底被调了几次」,而不是只看返回值对不对。
    """
    reply = AIMessage(content='{"label":"violation","violations":["未戴安全帽"],"note":"n"}')
    model = _ScriptedModel([reply])
    _patch_model_only(monkeypatch, model)

    artifact_id = _register_photo()
    first = await _call(artifact_id)
    assert first["ok"] is True
    assert model.calls == 1

    second = await _call(artifact_id)
    assert second["ok"] is True
    assert second["data"] == first["data"]
    assert model.calls == 1, f"缓存没生效,模型被调了 {model.calls} 次"


async def test_换一张照片不会错误命中上一张的缓存(monkeypatch: pytest.MonkeyPatch) -> None:
    """缓存键里带的是**图片内容**(base64 进了 messages),不是文件路径。

    所以内容不同必然 miss、内容相同必然 hit。这条和上一条是一对,
    只测命中不测隔离的话,一个恒返回同一条缓存的实现也能全绿。
    """
    first_reply = AIMessage(content='{"label":"violation","violations":["未戴安全帽"],"note":"a"}')
    second_reply = AIMessage(content='{"label":"compliant","violations":[],"note":"b"}')
    model = _ScriptedModel([first_reply, second_reply])
    _patch_model_only(monkeypatch, model)

    a = await _call(_register_photo(data=FAKE_JPEG))
    b = await _call(_register_photo(data=_make_jpeg(color=(30, 60, 200))))  # 换一张,内容不同

    assert model.calls == 2, "两张不同的照片应当各请求一次"
    assert a["data"]["label"] == "violation"
    assert b["data"]["label"] == "compliant"


async def test_解析不出的回答不写缓存_重试真的能成功(monkeypatch: pytest.MonkeyPatch) -> None:
    """**这条锁的是一个真出过的 blocker。**

    不加 is_usable 那道闸时:抠不出 JSON 的回答照样被写进缓存,于是那句
    「请再试一次」永远不可能成功 —— 第二次直接命中坏缓存,0 次网络调用、
    0.005 秒、一字不差的同一句失败,这张照片对该 prompt_version 被永久钉死。
    重试次数再多都没用,因为根本没发出请求。

    更糟的是缓存键算在**消息内容**上(图片以 base64 进 messages),所以让用户
    「重新传一次同一个文件」也逃不掉:新 artifact_id、同一个缓存键。
    只有重新拍一张(字节不同)才行。

    而唯一的清理手段(clear_cache / 换 prompt_version)会烧掉**整份**缓存,
    与 TODO-11「演示日必须预热缓存」正面冲突 —— 为救一张图得毁掉全部预热。
    """
    bad = AIMessage(content="我觉得这张照片挺好的,没什么问题。")  # 纯文本,抠不出 JSON
    good = AIMessage(content='{"label":"violation","violations":["未戴安全帽"],"note":"n"}')
    model = _ScriptedModel([bad, good])
    _patch_model_only(monkeypatch, model)

    artifact_id = _register_photo()

    first = await _call(artifact_id)
    assert first["ok"] is False
    assert first["error_code"] == ErrorCode.UPSTREAM_ERROR.value
    assert model.calls == 1

    # 第二次必须**真的**再问一遍模型,而不是取走上一次那条坏回答
    second = await _call(artifact_id)
    assert model.calls == 2, "坏回答被缓存了,「请再试一次」永远不可能成功"
    assert second["ok"] is True
    assert second["data"]["violations"] == ["未戴安全帽"]


@pytest.mark.parametrize(
    ("raw", "expected_label"),
    [
        # 模型在答案后面补一句带花括号的说明 —— 贪婪正则会把整段吃掉判成失败
        (
            '{"label":"compliant","violations":[],"note":"x"}\n参考格式:{"label":"violation"}',
            "compliant",
        ),
        # 引子里带花括号 —— 只试第一段的话会卡在 {规范} 上
        ('根据 {规范} 判断:\n{"label":"not_site","violations":[],"note":"y"}', "not_site"),
        # note 里含花括号,整体仍是**一个**对象,不能被切开
        (
            '{"label":"violation","violations":["未戴安全帽"],"note":"见 {GB50720} 第3条"}',
            "violation",
        ),
        # note 里带转义引号,**且整段不是合法 JSON**(前面有引子)——
        # 这样才会真的走到配平扫描。扫描必须认得 \\" 不是字符串结束,
        # 否则会误判成"出了字符串",note 里那个 { 就被当成新对象的开头,整段解析失败。
        (
            '判断如下:\n{"label":"violation","violations":["未戴安全帽"],'
            '"note":"他说\\"忘带了\\",{待复核}"}',
            "violation",
        ),
    ],
)
def test_模型爱加的各种壳都能剥掉(raw: str, expected_label: str) -> None:
    """每多剥掉一种壳,就少一次「答案其实是好的、却被当成失败」。

    这直接省钱也省事故:解析失败会触发重试(视觉档 $3/M、一次几十秒),
    而在修好 is_usable 之前它还会被永久缓存。
    """
    parsed = _extract_json_object(raw)
    assert parsed is not None, f"这段输出本来是可用的,却没解析出来:{raw[:60]}"
    assert parsed["label"] == expected_label


async def test_可重试错误耗尽后返回中文信封(monkeypatch: pytest.MonkeyPatch) -> None:
    """必须打桩 _sleep:真实退避是 1+2+4=7 秒,不打桩这一条就要让整个测试套多跑 7 秒。

    同时锁住重试次数 = 1 + llm_max_retries。要是哪天有人把 max_retries=0
    从工具里删了,SDK 那层会再叠一轮退避,这里数出来的次数不变、但真实
    HTTP 请求会翻几倍 —— 所以这条测不了那个,那个由
    test_视觉调用走vision档且传了max_retries为0 守着,两条缺一不可。
    """
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(llm, "_sleep", _fake_sleep)
    model = _ScriptedModel([ConnectionError("演示日断网")])
    _patch_model_only(monkeypatch, model)

    from gyt.config import get_settings

    result = await _call(_register_photo())

    assert result["ok"] is False
    assert result["data"] is None
    # 视觉档刻意压到 VISION_MAX_ATTEMPTS(2)而不是默认的 1 + llm_max_retries(4):
    # 重试次数是**乘在超时上**的,超时 150 秒 × 4 次 = 10 分钟对着空界面干等。
    assert model.calls == VISION_MAX_ATTEMPTS
    assert VISION_MAX_ATTEMPTS < 1 + get_settings().llm_max_retries
    # 退避曲线应当是递增的,而不是每次都睡同样长
    assert delays == sorted(delays) and len(set(delays)) == len(delays)
    # 给工人的必须是中文人话,不能是 openai SDK 的英文异常
    assert result["user_msg"]
    assert not result["user_msg"].isascii()


# ---------------------------------------------------------------------------
# 六、图片预处理(_prepare_image)—— 降采样 / 挡动图 / 验真伪
#
# 这三件事只能靠**解码图片本身**来做,光看扩展名一件都做不到。
# ---------------------------------------------------------------------------


def _png_bytes(width: int, height: int, *, frames: int = 1, fmt: str = "PNG") -> bytes:
    """造一张测试图。frames>1 时造动图(GIF/WebP),用来验证动图会被挡下。"""
    from PIL import Image

    buffer = io.BytesIO()
    base = Image.new("RGB", (width, height), (120, 140, 90))
    if frames > 1:
        seq = [Image.new("RGB", (width, height), (i * 40, 100, 100)) for i in range(frames)]
        base.save(buffer, format=fmt, save_all=True, append_images=seq, duration=100, loop=0)
    else:
        base.save(buffer, format=fmt)
    return buffer.getvalue()


def test_没超限的图原样返回_不做重编码() -> None:
    """重编码只会白白损失画质,而「有没有戴安全帽」经不起反复有损压缩。

    这条同时锁住:返回的 MIME 必须跟着**原扩展名**走,不能一律说成 jpeg。
    """
    from gyt.agents.safety.tools import _prepare_image

    raw = _png_bytes(800, 600)
    out, mime = _prepare_image(raw, ".png")
    assert out is raw or out == raw, "没超限却被重新编码了"
    assert mime == "image/png"


def test_超过长边上限的图会被等比缩小(monkeypatch: pytest.MonkeyPatch) -> None:
    """视觉 token 按**像素面积**计。实测 4000×2430 的工地照片端到端 59.7 秒,
    而 1600×1067 的同类只要 10.1 秒 —— 手机原图正好是前者那个量级。"""
    from PIL import Image

    from gyt.agents.safety.tools import _prepare_image
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_PHOTO_COMPRESS_MAX_EDGE_PX", "512")
    get_settings.cache_clear()

    out, mime = _prepare_image(_png_bytes(2000, 1200), ".png")
    with Image.open(io.BytesIO(out)) as image:
        assert max(image.size) == 512
        assert image.size == (512, 307), "必须等比缩放,不能拉伸变形"
    # 缩放后统一转成 JPEG(体积最小),MIME 要跟着改,否则上游按 png 解会失败
    assert mime == "image/jpeg"


def test_动图会被挡下() -> None:
    """**这是只看扩展名绝对挡不住的一类。**

    animated gif / webp 的 MIME 与静态图完全相同,格式白名单必然放行;
    而月之暗面可能把动图当**视频**解码计费 —— 账单是静态图的几十倍,且完全静默。
    """
    from gyt.agents.safety.tools import ImageRejected, _prepare_image

    with pytest.raises(ImageRejected) as caught:
        _prepare_image(_png_bytes(200, 200, frames=3, fmt="WEBP"), ".webp")
    assert caught.value.code == ErrorCode.FILE_UNSUPPORTED
    assert "动图" in caught.value.user_msg


def test_改名成jpg的非图片文件会被挡下() -> None:
    """在加这道校验之前,只看扩展名 —— 一个改名成 .jpg 的文本文件
    照样会被 base64 发给视觉模型,白花一次钱换回一句听不懂的话。"""
    from gyt.agents.safety.tools import ImageRejected, _prepare_image

    with pytest.raises(ImageRejected) as caught:
        # 前 4 字节是 JPEG 魔数,后面是纯文本 —— 靠魔数骗过扩展名检查,但解不开
        _prepare_image(b"\xff\xd8\xff\xe0 not really an image, just named .jpg", ".jpg")
    assert caught.value.code == ErrorCode.FILE_CORRUPT
    assert "打不开" in caught.value.user_msg


def test_体积超标时逐档降质量(monkeypatch: pytest.MonkeyPatch) -> None:
    """尺寸没超但体积超(高质量大图)也要压,否则 base64 之后请求体会很大。"""
    from gyt.agents.safety.tools import _prepare_image
    from gyt.config import get_settings

    monkeypatch.setenv("GYT_PHOTO_COMPRESS_TARGET_MB", "0.02")  # 20KB
    get_settings.cache_clear()

    # 造一张噪声图:纯色图压完太小,测不出降质量这条路径
    from PIL import Image

    noise = Image.effect_noise((900, 900), 60).convert("RGB")
    buffer = io.BytesIO()
    noise.save(buffer, format="PNG")
    raw = buffer.getvalue()

    out, mime = _prepare_image(raw, ".png")
    assert len(out) < len(raw), "体积超标却没被压"
    assert mime == "image/jpeg"


async def test_预处理失败会变成中文信封而不是异常(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端:ImageRejected 要被接住转成信封,不能让它冒到 tool_guard 变成
    「系统开小差了」—— 那句话对工人毫无帮助,他不知道该换张图还是重传。"""
    _patch_llm(monkeypatch, reply='{"label":"compliant","violations":[],"note":""}')
    artifact_id = _register_photo(data=b"\xff\xd8\xff\xe0 not an image at all")

    result = await _call(artifact_id)

    assert result["ok"] is False
    assert result["error_code"] == ErrorCode.FILE_CORRUPT.value
    assert "系统开小差" not in result["user_msg"]
