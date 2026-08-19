"""打卡链的全部用户可见中文文案(D12:文案集中,最后要整体换简繁)。

⚠️ **换简繁时是两个地方**:本文件(直连写入路径的话)+ ``agents/attendance/prompt.md``
(查询 Agent 给模型看的话,D 泳道在写)。两边头注互相指认,漏一边就是简繁混排。

⚠️ **姓名是例外中的例外:原样存、原样画 —— 后端这一侧任何情况下不进任何转换。**
本文件只提供姓名**周围**的模板字;模板与姓名的拼接点全都收在这里,
将来换简繁只动模板字、不碰人给的值。地盤名同理(它也是人给的)。

**2026-08-17(W12)这条改过口径,现在是「按层」而不是「一律」:**
前端渲染层做繁體答话时,聊天里显示的姓名字形会跟着转(方案 C)——
因为散文里的姓名没有任何标记能让转换器认出来。**后端这一侧不受影响**:
本文件画进水印的、``db/attendance.py`` 存进库的、docx 里印的,全都是原文。
放宽只到屏幕为止 —— 存储层转了名字就跟身份证对不上,那是真事故。
完整推演见 ``docs/W12_三语切换_方案.md`` §9.3 与
``agents/attendance/prompt.md`` 头注(两边同口径,改一处两处一起)。

为什么单独一个文件而不是散在 handler 里:打卡这条链上用户可见的中文不少
(409、限流、超大、缺字、字体部署错误、水印上的每一行…),散在十几个分支的
f-string 里,换简繁漏一条就是混排,**没有任何测试会发现** —— 集中之后
「要换的字」有名有姓。放 ``attendance/`` 而不是 ``agents/attendance/`` 是
依赖方向决定的(W7 §3.12):写入路径一个 LLM 都不经过,不能反过来背 Agent 包。

只放本泳道(A:接口与台账)真用到的句子,不为将来囤货 —— 比如
「凭证图已过期清理」是前端渲染 NULL artifact_id 时的话,归 checkin-lib.ts。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

CHECKIN_OK: Final[str] = "打卡成功,凭证已经出好,往下翻「最近打卡」就能看到。"
"""打卡落账成功。"""

CHECKIN_REPLAYED: Final[str] = "这次打卡之前已经记上了,给你的还是原来那张凭证,不会记成两次。"
"""同键同指纹的重发:返回原凭证。响应的形状与打卡成功完全相同(契约刻意如此,
前端不用写两条处理路径),但话要说清「没有重复记账」—— 断网重发的工友最担心的就是这个。"""

CHECKIN_CONFLICT: Final[str] = (
    "这次打卡的信息和之前那次对不上,没有覆盖原来的记录。要重新打卡的话,请重新拍照再交一次。"
)
"""409:同 event_id 但指纹不同。核心句「这次打卡的信息和之前那次对不上」是
W7 方案 §3.2 定死的口径,改措辞别把它改没了。"""

RATE_LIMITED: Final[str] = "打卡太频繁了,请等几秒再试一次。"
"""429。比 core/errors.py 那句「用的人太多」更贴场景 —— 打卡被限住几乎只有
「手抖连点」和「脚本刷」两种,前者看到「等几秒」就够了,后者不配更多解释。"""

PHOTO_EMPTY: Final[str] = "没收到照片,请重新拍一张再交。"
"""body 为空。"""

PHOTO_NOT_JPEG: Final[str] = "传上来的不是照片文件,请用页面上的拍照按钮重拍一张。"
"""魔数不对。判据是 JPEG 文件头,不是 Content-Type(浏览器给的 MIME 不可靠,
DXF 那条线已经证过一次)。"""

PHOTO_UNREADABLE: Final[str] = "这张照片打不开,可能传的时候坏了,请重拍一张再交。"
"""魔数对但解不开(截断、伪造尾部等)。"""

BAD_REQUEST: Final[str] = "这次打卡的信息没传对,请刷新页面重试;还不行就把这句话告诉管理员。"
"""400 的统一人话:header 缺失 / 格式错 / 坐标越界 / 枚举越词表,全用这一句。
具体是哪个字段错了走 ``detail=`` 只进日志 —— 这类错要么是前端 bug 要么是
手写客户端,细节对工友没用,对试探接口的人反而有用。"""

DENY: Final[str] = "访问被拒绝,请联系发你链接的人。"
"""401。与 backend/auth.py 的 DENY_MESSAGE **一字不差** —— 那边的头注解释了
为什么所有拒绝必须同一句话(任何差异都是送给爆破脚本的信号)。两份拷贝是
刻意的:auth.py 不在 gyt 包里,反向 import 不可靠(core/access.py 头注)。
改任何一边要三处一起改(auth.DENY_MESSAGE / 这里 / errors.DEFAULT_USER_MSG
的 UNAUTHORIZED),这条同源关系要在汇合时登记进 CLAUDE.md。"""

FONT_BROKEN: Final[str] = "系统这边的字体出了问题,暂时出不了凭证,请把这句话转给管理员。"
"""500:候选字体一个都找不到。这是部署错误不是用户错误,工友唯一能做的就是
把话带到 —— 所以句子里写明「转给管理员」,而路径、字体名等细节只进日志。"""


def photo_too_large(max_mb: float) -> str:
    """413:照片超上限。带上具体兆数 —— 「太大」必须能回答「多大才行」。

    正常主路径(canvas 截帧)出的图远小于上限,能撞到这条的基本是降级路径
    从相册选了原图,所以指路「用页面上的拍照按钮」而不是叫人自己去压缩。
    """
    return f"照片太大了(超过 {max_mb:.0f}MB),直接用页面上的拍照按钮重拍一张就行。"


def missing_glyphs(chars: Sequence[str]) -> str:
    """400:字体画不出某些字,**必须点名是哪些字**(W7 §3.4)。

    「水印失败」和「你名字里的『𠮶』字打不出来」是完全不同的两句话 ——
    后者工友自己换个写法(同音字)就能解决,前者只会让他反复重试反复失败。
    字符原样回显、不做任何转换(它们多半正是姓名里的字)。
    """
    shown = "".join(chars)
    return f"「{shown}」这几个字画不进凭证,请换个写法(比如用同音字)再打一次卡。"


# ---------------------------------------------------------------------------
# 水印文字行 —— 内容全部出自本文件,watermark.render_attendance_photo 只管画
# ---------------------------------------------------------------------------

GEO_STATUS_LABELS: Final[dict[str, str]] = {
    "denied": "定位:未授权",
    "timeout": "定位:超时没取到",
    "unsupported": "定位:设备不支持",
    "error": "定位:获取出错",
    "absent": "定位:未提供",
}
"""非 ok 状态画在凭证上的说明。键与 ``db/attendance.py`` 的 ``GEO_STATUSES``
同源(少一个 ok —— ok 画的是坐标本身,见 ``_geo_line``)。
**不许把几种合并成一句「无定位」**:六种状态在审计里意义完全不同
(前两种是用户行为,后面是我们自己的问题),凭证上得分得开。
test_checkin_api.py 有一条守门测试直接比对这两份词表。"""


def _geo_line(
    geo_status: str, lat: float | None, lon: float | None, accuracy_m: float | None
) -> str:
    """定位那一行。ok 画坐标(6 位小数 ≈ 0.1 米,精度取整米 —— 这是**展示**,
    幂等指纹用的是 header 原始串,两者互不影响);其余状态查词表。
    词表兜底那句理论上到不了(handler 已按 GEO_STATUSES 校验过),
    真到了宁可画一句看得懂的话,也不许整张凭证因为一行文案炸掉。"""
    if geo_status == "ok" and lat is not None and lon is not None:
        acc = f"(±{accuracy_m:.0f}米)" if accuracy_m is not None else ""
        return f"定位:{lat:.6f}, {lon:.6f}{acc}"
    return GEO_STATUS_LABELS.get(geo_status, "定位:状态未知")


def watermark_lines(
    *,
    worker_name: str,
    site_name: str | None,
    display_time: str,
    receipt_no: str,
    geo_status: str,
    lat: float | None,
    lon: float | None,
    accuracy_m: float | None,
) -> list[str]:
    """拼凭证水印的文字行(姓名 / 地盤 / 时间 / 凭证编号 / 定位说明)。

    行内容集中在这里是 D12 的要求:``render_attendance_photo`` 的契约写明
    「内容由调用方拼,本函数只管画」。姓名与地盤名**原样插值**。
    地盤没填就不画那一行 —— 画一行「地盤:(未填)」只是把空白说了一遍。
    """
    lines = [f"姓名:{worker_name}"]
    if site_name:
        lines.append(f"地盤:{site_name}")
    lines.append(f"时间:{display_time}")
    lines.append(f"凭证:{receipt_no}")
    lines.append(_geo_line(geo_status, lat, lon, accuracy_m))
    return lines


__all__ = [
    "BAD_REQUEST",
    "CHECKIN_CONFLICT",
    "CHECKIN_OK",
    "CHECKIN_REPLAYED",
    "DENY",
    "FONT_BROKEN",
    "GEO_STATUS_LABELS",
    "PHOTO_EMPTY",
    "PHOTO_NOT_JPEG",
    "PHOTO_UNREADABLE",
    "RATE_LIMITED",
    "missing_glyphs",
    "photo_too_large",
    "watermark_lines",
]
