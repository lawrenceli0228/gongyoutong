"""监理处置直连接口。**本模块头注是 ``/supervision/*`` 请求与响应契约的唯一真相。**

隐患台账与状态机的唯一真相在 ``gyt/db/hazards.py``,不在这里 —— 那边是存储与状态合法性,
这边是协议与业务判断(该走哪条路、期限算不算得出来、话怎么说给工地上的人听)。

===========================================================================
🔴 挂载:不 import 进 ``backend/webapp.py`` 就是全部 404,而且没有任何报错
===========================================================================
``langgraph.json`` 的 ``http.app`` **只能有一个**,现在指的是 ``./webapp.py:app``。
本模块导出 ``SUPERVISION_ROUTES``,由 webapp.py 铺进它的 ``routes=[...]``
(与 ``checkin_api.CHECKIN_ROUTES`` 同一手法)。删掉那一铺 = 监理端点整个消失,
而现象是 404、**不是启动报错**(langgraph 不知道有谁本该在)。

公网那一侧还要 ``Caddyfile`` 里 ``/api/supervision*`` 那条 route,
且**必须排在 ``handle_path /api/*`` 之前** —— handle 系列按书写顺序择一匹配,
排后面 = 永不生效且无报错。

===========================================================================
为什么写入动作全部走 HTTP,一个 LLM 都不经过(方案 §5.1)
===========================================================================
签发《监理通知单》《工程暂停令》是**法律行为**,不能由概率性系统单方面触发。
supervision Agent 只能查、只能建议(``list_hazards`` / ``get_hazard`` /
``suggest_disposal``),**任何产出文书或改状态的动作都不是 Agent 工具**。
与 W7「打卡写入不走对话链」同构 —— 那次理由是防编造和幂等,这次更硬。

===========================================================================
端点一览(路径不含 ``/api``:那一段由 Caddy 剥掉,与打卡链同规矩)
===========================================================================
**查询两条**(W10),``GET``,没有请求体 —— 参数走查询串与路径::

    GET /supervision/hazards?scope=…&project_id=…
        隐患清单。它是界面上那块**常驻操作台**的数据源:面板不再从聊天流里取数
        (supervisor 的 ``output_mode="last_message"`` 会把子 Agent 的工具返回整个丢掉,
        于是三张卡一次都没渲染出来过 —— 完整根因在
        ``docs/W10_界面取不到工具返回_方案.md``)。

        · ``scope``:``在办`` / ``待确认`` / ``超期`` / ``全部`` **四选一**,缺省「在办」。
          词表外一律 **400**,不许静默回落到某一档 —— 少给的清单在界面上看不出来少了
          (同 ``agents/supervision/scoping.SCOPES`` 头注:``in_scope`` 认不出的词会
          静默落在「在办」,所以拦野词是**调用方的责任**,别指望它替你报错)。

        · ``project_id`` **三态,一态都不许丢**(D6:未归属是空串,不是 NULL)::

              参数不出现                → 不筛工地(全部工地) → list_rows(project_id=None)
              ?project_id=(有键无值)   → 只看未归属          → list_rows(project_id="")
              ?project_id=P-xxx         → 只看这个工地        → list_rows(project_id="P-xxx")

          三态与 ``db/hazards.list_rows`` 的 ``project_id: str | None`` **1:1 对上**
          (那边的 docstring:None 是不筛项目,"" 是只看未归属,两者语义完全不同)。
          starlette 里"键在不在"天然分得开,**中间任何一处写 ``or ""`` 都会把两态合并**,
          表现是「全部工地」悄悄变成「只有未归属」,而界面上看不出少了什么。

    GET /supervision/hazards/{hazard_no}
        单条详情 + **证据链**:每份文书的下载信息(``artifact_id`` / ``filename``)、
        每次复查的照片编号与结论。查不到编号回 404。

**写入八条**,全部 ``POST``、``Content-Type: application/json``、请求体是一个 JSON 对象::

    POST /supervision/confirm            {"hazard_nos": ["GYT-H-…", …]}   ← 也收单条 hazard_no
        pending → open(D17 的人工确认闸)。**支持批量,每条一个事务、互不牵连。**

    POST /supervision/reject             {"hazard_no": …}
        否决一条**待确认**的隐患(状态机图里 pending 那条否决支),整行从库里删掉。
        已经确认过的一律拒(409):留档与证据链不能因为一次误点消失 ——
        ``db.delete_pending`` 的 ``WHERE status='pending'`` 是硬守卫,这里只是先说人话。
        🔴 回执里的 ``status`` 是 ``"deleted"``,**全项目唯一一个不属于 ``db.STATUSES``
        八档的状态值** —— 那一行已经从库里删掉了,没有状态可报(见 ``_STATUS_DELETED``)。

    POST /supervision/dismiss            {"hazard_no": …, "reason": "白色安全帽,现场核过",
                                          "issued_by": "陈大文"}
        「这条不是隐患 / 已当场整改」——**不出任何文书**把 ``open`` 关掉。
        2026-08-21 加。在它之前 ``open`` 只有两条出口而两条都要签发法律文书,
        于是一条识别错了的隐患**关不掉**:唯一的出路是为一个不存在的隐患真的签一份
        《监理通知单》,再拍张照登记「复查合格」——**纠错的代价是往证据链里塞假文书**。

        与 ``reject`` 的分工(别搞混):
            reject   pending → 整行**删掉**,什么都不留(还没人确认过,没有留档价值)
            dismiss  open    → 行还在、编号还在、照片还在,标成 closed 并**写明理由**

        🔴 ``reason`` 必填(至少 4 个字),原样进 ``hazards.closed_reason`` ——
        它是事后唯一能回答「这条为什么关的」的地方,也是它与「删掉」的本质区别。
        ⚠️ **状态闸排在理由闸前面**:「这条路你根本走不通」比「你的理由太短」更根本。
        签过文书的、还没确认的一律 409(后者会另外指一句「请用否决」)。

    POST /supervision/grade              {"hazard_no": …, "grade": "一般"|"严重"}
        人工定级,清 needs_grading。**只允许在还没签过任何文书时改**
        (状态 pending / open;理由见 ``_GRADABLE_STATUSES``)。

    POST /supervision/notice             {"hazard_no": …, "due_phrase": "下周三"}
        《监理通知单》一份。grade=严重 **拒**(硬拦①)。

    POST /supervision/suspend            {"hazard_no": …, "due_phrase": "明天"}
        《通知单》+《工程暂停令》+《致建设单位报告》**三份原子产出**。
        grade≠严重 **拒**(硬拦②)。

    POST /supervision/reinspect-result   {"hazard_no": …, "result": "pass"|"fail",
                                          "after_photo_id": "<32位hex>"}
        复查结论。**结论由人下**(D11),``after_photo_id`` 必填,而且必须是
        **真实存在的一张照片**:kind=PHOTO + 正文文件还在(三道校验见 ``_require_photo``)。
        那个编号从下面 ``POST /supervision/photo`` 换来 —— 别让人去聊天记录里
        抄一串 hex(真人测试的原话:「照片不能是编号意义不明」)。
        它**不出文书**:只往 hazard_docs 挂一条 ``reinspect`` 记录
        (那一行的 ``doc_no`` 长相刻意不像文书编号,见 ``_REINSPECT_NO_MARK``)。

    POST /supervision/resume             {"hazard_no": …}
        《工程复工令》。**只发给 status=resuming**(停过工 + 复查已合格)。

    POST /supervision/escalate           {"hazard_no": …}
        《监理报告》报主管部门,正文附 hazard_docs 完整证据链。只从 reinspect_failed 进。

**上传一条**,``POST``,请求体是**原始图片字节** —— 不是 multipart、不是 JSON::

    POST /supervision/photo
      X-Api-Key:    <与其余十条同一把锁>
      Content-Type: 随便填(``image/jpeg`` 之类)—— **服务端一个字都不信它**
      body:         图片原始字节

      → 200 {"ok": true,
             "data": {"photo_id": "<32位hex>", "filename": "复查照片.jpg"},
             "user_msg": "照片收到了。", "error_code": null}

        把一张照片登记成 ``ArtifactKind.PHOTO`` 产物,回一个 ``photo_id``,
        给上面 ``reinspect-result`` 的 ``after_photo_id`` 用。

        **为什么非有这条不可:** 「登记复查结论」要填一个 32 位十六进制编号,
        而后端此前**没有任何通用的传图口子** —— PHOTO 产物只能经由聊天的
        ``core/uploads.py`` 产生。于是做复查的人手机里刚拍完那张照片,却得先发进
        聊天框、再把图底下那串 hex 抄回表单。真人测试的反馈原话是
        「照片不能是编号意义不明」,这条端点就是那句话的解法。

        协议手法整套照抄 ``checkin_api.post_checkin``(它已经跑通过一整轮上线,
        连 Caddy 的请求体闸都是按它调的),**别自己发明第二套**:
          · **raw body 而不是 multipart** —— starlette 的 multipart 解析器
            ``spool_max_size = 1MB``,超过就自动滚去 /tmp(原图落盘),而且要
            解析完整请求才知道多大、掐不住流(理由原文在 checkin_api 头注);
          · 大小上限取 ``settings.photo_max_mb``(**与打卡同一个旋钮**,当前 10MB),
            边收边数、超限当场断,回 **413**(码与打卡那条一字不差);
          · 🔴 **类型按魔数判,不信 ``Content-Type``** —— 那是客户端自述的。
            认 JPEG / PNG / WebP 三种(见 ``_sniff_image_ext``),认不出回 400。
            这一道不是洁癖:这张照片接下来会被 ``_require_photo`` 当成**复查证据**,
            而复查合格是**销项**的唯一通道 —— 只信 Content-Type 的话,一个 .txt
            改个头就能把隐患销掉,留档文书上写着「隐患已消除」。

        ⚠️ 它**不是动作端点、不出文书**,所以 ``data`` 里**没有** ``documents`` 键 ——
        那不是漏了,见下面「Envelope 的 data 形状是冻结的」一节的说明。

响应码(十一条端点同一套,**GET 与上传也一样**):

    200  ok=True
    400  INVALID_INPUT   缺字段 / 级别或结论不在词表 / **筛子不在四个词里** /
                         **期限解析不出** / 照片编号不对 /
                         **传上来的 body 是空的、或者魔数认不出是图片**
    401  UNAUTHORIZED    handler 自查令牌不过(纵深防御,同 checkin_api)。
                         **两条 GET 与上传那条同样过这道闸** —— 隐患清单里有工地、
                         有违规项、有照片编号,是要登录才看得到的东西,不是公开数据;
                         而上传口不拦就是给全网一个往这台机器写文件的入口。
    404  NOT_FOUND       隐患编号查不到 / 复查照片查不到
    409  CONFLICT        三条硬拦、状态机不允许、并发把状态改掉了、
                         否决一条已经确认过的隐患
    413  FILE_TOO_LARGE  **只有 ``POST /supervision/photo``**:流式读到超过
                         ``photo_max_mb`` 就当场断,不等收完
    500  INTERNAL        兜底;编号摇不出来也落这里。user_msg 是人话,细节只进日志

===========================================================================
🔴 三条硬拦(全在服务端代码里,不靠提示词。Codex#3 / Codex#11)
===========================================================================
1. ``notice`` 对 ``grade='严重'`` **拒绝**,提示去走 ``suspend``;
2. ``suspend`` 对 ``grade!='严重'`` 拒绝;
3. **任何签发**(notice / suspend / resume / escalate)对 ``needs_grading=1`` 拒绝。

第 1 条与第 2 条是**两个方向相反的事故**,两向都要拦:
「严重隐患只发了通知单」= 该停工的没停,「一般隐患签了暂停令」= 平白停一片人的工。
第 3 条是 Codex#11:只标注不拦截的话,未知风险能按一般隐患走完整闭环并被销项。
解锁的唯一通道是 ``POST /supervision/grade``(人定级 + 清旗子)。

===========================================================================
🔴 ``due_phrase`` 收用户原话,交 ``agents/schedule/dates.py`` 算,解析不出就 fail
===========================================================================
**不许留空**(Codex#12):``due_date`` 为空的隐患永远进不了超期清单,
也就**永远不会被升级** —— 一条没人催的隐患躺在库里,报表上还显示「在办」。

``dates.py`` 用 314 行证明了模型换算中文日期不可靠,红线是「模型只传原话,代码来算」。
本模块**一行日期换算都不许自己写**,今天是哪天取自香港时间权威
(``attendance/receipt.py``,D7),不看宿主时区。

===========================================================================
🔴 Envelope 的 ``data`` 形状是冻结的(Codex#16),前端按 ``documents`` 渲染下载卡
===========================================================================
::

    {"ok": true, "data": {
        "hazard_no": "GYT-H-…",
        "status": "suspended",                 ← 动作完成后库里的真实状态
        "documents": [                         ← 顺序固定,一份一张卡
          {"doc_type": "notice",       "doc_no": "GYT-TZ-…", "artifact_id": "…",
           "filename": "监理通知单_GYT-TZ-….docx"},
          {"doc_type": "suspension",   "doc_no": "GYT-ZT-…", "artifact_id": "…", "filename": "…"},
          {"doc_type": "owner_report", "doc_no": "GYT-JS-…", "artifact_id": "…", "filename": "…"}]},
     "user_msg": "…", "error_code": null}

定死数组形状是为了**避免静默丢件**:三份文书里少出一份,若形状是三个独立的键,
前端少渲一张卡没有任何异常;是数组的话「N 张卡」与「N 份文书」天然对齐。

三处例外,都在下面写明:
  · ``confirm`` 是批量动作,``data`` = ``{"confirmed": [...], "failed": [{…}]}``;
  · ``grade`` 与 ``reinspect-result`` 不出文书,``documents`` 恒为 ``[]``。
    **复查记录不进 ``documents``** —— 它没有 artifact_id,进去就是一张点不开的卡。
  · ``reject`` **属于这一类**(不出文书,``documents`` 恒为 ``[]``),但 ``status``
    是那个 ``"deleted"``:三个键一个不少,前端那套归一化照旧能用,只是它拿到的
    「状态」不在八档里 —— 该刷新列表、把这一条划掉,而不是去查这个状态怎么处置。

**两条 GET 与上传那条不在这份冻结形状里**,它们各有自己的 ``data``:

  · 清单 = ``{scope, project_id, today, hazards[], total, pending, overdue,
    unassigned, truncated}``;
  · 详情 = 「清单那一行的全部字段 + found_at / closed_at / reinspected / documents[]」;
  · **上传 = ``{photo_id, filename}``,就两个键,没有 ``documents``、也没有
    ``hazard_no`` / ``status``**(它压根不认识任何一条隐患,只是把字节存下来)。

理由:冻结那份形状是为了「N 张下载卡对齐 N 份文书」,只对**动作**成立 ——
查询回的是**表格**,上传回的是**一个编号**,硬套一个 ``documents`` 恒空的壳
只会让前端多一层拆包。三者与那八条动作共同的只有四键信封。
⚠️ 所以下一个人看到 ``POST /supervision/photo`` 的 ``data`` 里没有 ``documents``
**不是漏了**,别"顺手补齐" —— 补上去等于在界面上多一块恒空的下载卡位。

🔴 **详情里 ``documents[*].filename`` 与签发那条路给的必须一模一样。**
两处都走 ``_filename()``,谁也不许现拼第二份 —— 不同源的表现是界面上下载卡的文件名
和实际落盘的对不上,**而不会有任何报错**。
🔴 **``documents[*].photo_id`` 是 ``hazard_docs.photo_id``(这一次复查那张),
不是 ``hazards.photo_id``(首次发现那张)。** 混起来 = 拿发现时的照片当"整改后"的
证据,而「复查必须挂照片」这条红线的全部意义就是事后追责时分得清这两张。

===========================================================================
🔴 文件先落盘、库后写(方案 §6.4,Codex#7)
===========================================================================
::

    ① 三份 docx 渲染到内存(docgen 是纯函数,不落盘)
    ② artifacts.register ×3 → 三个 artifact_id   ← 文件写失败:整个操作 fail,库一行没动
    ③ 一个事务:写 hazard_docs ×3 + 改 status     ← 库写失败:留三个孤儿文件,可接受

取舍:**孤儿文件(有文件无记录)比孤儿记录(有记录无文件)安全得多** ——
后者会让证据链里出现一份点不开的「文书」,而那是要拿去追责的材料。
孤儿文件的清理器本批不做(方案 §8 明记)。

事务盖不住 ``artifacts.register`` 的文件与 sidecar 写入,所以顺序是唯一的保证;
写库真的失败时**状态一定没变**(db 层一个事务里改状态 + 挂文书,见 ``_run_transition``)。

===========================================================================
鉴权、限流、并发
===========================================================================
- **鉴权两道**:第一道是 langgraph 的 ``enable_custom_route_auth``(``langgraph.json``
  里那个键漏了就整条裸奔且无报错);第二道是本模块 handler 自查,判定与 ``auth.py``
  完全同源(都用 ``core/access.py``:空 / 占位符 / 过短 = 未配置 = 放行)。
- **不设限流桶。** 与打卡不同:这些端点在登录闸 + ``X-Api-Key`` 之后,而且状态机本身
  就是闸(同一条隐患签过一次,第二次会被迁移守卫拒掉),刷不出量来。
  哪天要加,照抄 ``checkin_api.get_global_limiter`` 那只桶的写法,连测试夹具一起抄。

  ⚠️ **``POST /supervision/photo`` 让这条理由缺了一角,写在这儿免得下一个人照着"反正
  有状态机兜着"往下推。** 上传口**不经过任何状态机** —— 一个已登录的人循环传 10MB
  照片,每次都真落一份产物到盘上,量只由 Caddy 的请求体闸和磁盘决定。
  暂时接受的账:① 它在登录闸之后,不是公网敞开的口;② 落的是定长的 ``photo_max_mb``,
  不是无界流;③ 本批要的是"能传照片"而不是"抗刷"。
  真要收紧,该加的是**一只按字节计的全局桶**(不是按次数),而且 checkin 那边已经有
  同型的先例可抄。**别把这条理由继续当成"整个 supervision 都不用限流"的结论。**
- **并发**:「先读一次做判断」与「写」之间有窗口,真正说了算的是 db 层
  ``UPDATE … WHERE status IN (…)`` 的 rowcount —— 本模块拿到 False 一律回 409
  「状态刚被改过」,**绝不信先读的那份快照**。
- 阻塞活(sqlite、docx 渲染、落盘)一律 ``run_in_threadpool``:这些路由和图跑在同一个
  事件循环里,langgraph 的 blockbuster 会把同步 IO 判成违规并抛 BlockingError
  (schedule / cad 都踩过同款坑)。

===========================================================================
文书正文已经切出去了 —— 切在 ``agents/supervision/documents.py``
===========================================================================
2026-08-16 切的。原来「文书正文写什么」那两百行(元信息表 / 证据链表 /
五种文书各自的节 / 渲染出口)全部搬去了 ``agents/supervision/documents.py``,
本文件只留「协议 + 判断 + 编排」:

    documents.py       每种文书**写什么字**(正文措辞、证据链表的列)
    docgen.py          文书**长什么样**(标题、表格样式、签字栏、免责句版式)
    supervision_api.py 鉴权、四道闸、状态机、期限解析、编号重试、落盘顺序、话术

接缝在那儿的判据是**依赖方向**:正文那一段只依赖 ``docgen`` 与一个
``DocContext``,与鉴权、状态机、原子性一点关系都没有;而且它大半是中文正文
本身、不是逻辑,读这个文件的人不需要一路翻过它才看到下一个判断。
再加两种文书(F4 的定期汇总、专项报告)也只动 documents.py,不碰这里。

⚠️ **别把 ``docgen`` 直接 import 回本文件。** 本文件只 import ``documents``,
经它拿 ``render_doc``。绕过去在这儿再拼一份正文 = 正文有了两个真相,
而两份文书措辞不一样是**没有任何报错**的事故(免责句串档、编号没印上去,
都要等有人把两份纸并排看才发现)。
"""

from __future__ import annotations

import hmac
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import date
from typing import Any, Final, NamedTuple

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from gyt.agents.schedule.dates import DueParseError, format_display, parse_due

# 筛子四词、超期判定、八档状态中文名、``hazard_item()`` 的对外形状 —— 全部在 scoping.py,
# **对话链(agents/supervision/tools.py)与本模块共用同一份判据**。判据抄第二份的表现是
# 端点筛出来的清单与对话里报的条数悄悄对不上,而两边测试都绿(TODO-45 A 组那类漂移)。
# ⚠️ **按模块名 import,调用点写 ``scoping.xxx()``,不许 ``from … import today_hk``**:
#    测试把「今天」钉死靠的是 monkeypatch ``scoping.today_hk`` 这一个点,按名 import
#    会在导入那一刻把函数对象绑死,桩打不进去(理由原文在那个函数的 docstring)。
from gyt.agents.supervision import scoping

# 文书正文(每种文书写什么字)整段在 agents/supervision/documents.py,本模块只管
# 协议 + 判断 + 编排。**别把 docgen 直接 import 回来** —— 那等于在这里再拼一份正文,
# 而正文的唯一真相只能有一处(免责句串档、编号没印上去,都是不报错的事故)。
from gyt.agents.supervision.documents import UNASSIGNED_PROJECT_ZH, DocContext, render_doc
from gyt.attendance.receipt import TimeSnapshot, make_snapshot
from gyt.config import ALLOWED_IMAGE_EXT, get_settings
from gyt.core import artifacts
from gyt.core.access import _api_key_from_headers, effective_access_token
from gyt.core.artifacts import ArtifactKind, ArtifactNotFound
from gyt.core.doc_no import DOC_TITLE_ZH, DocKind, DocNoExhaustedError, generate_unique
from gyt.core.errors import Envelope, ErrorCode, fail, ok
from gyt.db import hazards
from gyt.db import projects as projects_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 受控词表与常量(禁止在函数体里散落字面量)
# ---------------------------------------------------------------------------

# 八档状态的中文名**曾经在这里有一份拷贝**(``_STATUS_ZH``),2026-08-16(W10 S2)删掉了,
# 改用 ``scoping.STATUS_ZH``。删之前 AST 逐条比对过:两份的键、值、顺序、内嵌注释**逐字相同**。
#
# 🔴 为什么原来非有两份不可、而现在这个理由不成立了 —— 这一段是给下一个想"顺手再抄一份"的人看的:
#   旧理由是「本模块被 langgraph 按**文件路径**加载,而它反过来 import 本包的 docgen /
#   documents,让它再 import ``agents/supervision/tools.py`` 就成环」。那个理由**只对
#   tools.py 成立**:tools.py 会拉起 langchain,并且它自己就在这条依赖链上。
#   而 ``scoping.py`` 是 2026-08-16(S1)专门切出来的**零依赖叶子模块** —— 它只 import
#   ``db.hazards`` / ``schedule.dates`` / ``attendance.receipt`` 与标准库,一个包内模块都不 import
#   (那条约束由 ``tests/unit/test_supervision_scoping.py`` 用 AST 扫 import 语句钉着)。
#   现有的边全是向下的:``supervision_api → scoping``、``supervision_api → documents → scoping``、
#   ``tools → scoping``,谁都到不了本模块,成不了环。
#
#   拷贝少一份买到的是什么:那道导入期守卫**只数键、不比值**,它拦得住「db 加了一档而这里漏配」,
#   拦不住「两份拷贝的中文名漂开」—— 后者只能靠拷贝数量本身减少来防(TODO-45 A 组)。
#   ⚠️ 剩下的那份跨语言镜像 ``scripts/frontend-overrides/supervision-lib.ts`` 收敛不掉,
#   改中文名要手工带上它(CLAUDE.md 同源清单有登记)。
#
# 导入期「八档都有名字」那道硬失败守卫随中文名一起搬去了 scoping.py,不在本模块再写一遍。

_REINSPECT_DOC_TYPE: Final[str] = "reinspect"
"""``hazard_docs`` 里那条**不是文书**的行:复查留痕,没有 artifact_id,也没有编号类型段。"""

_DOC_TYPE_ZH: Final[dict[str, str]] = {
    **{kind.name.lower(): DOC_TITLE_ZH[kind] for kind in DocKind if kind is not DocKind.HAZARD},
    _REINSPECT_DOC_TYPE: "复查记录",
}
"""``hazard_docs.doc_type`` → 中文名。详情端点拿它填 ``doc_type_display``,
``_filename()`` 拿它拼文件名。

**文书那五档是从 ``core/doc_no.DOC_TITLE_ZH`` 派生的,不是手抄** —— 那边改了名字这边自动跟上。
两个孤儿在 ``doc_no.DocKind`` 的头注里写明,这里正好对上:
  · ``DocKind.HAZARD`` 是隐患自己的身份号、不是文书,所以从派生里剔掉;
  · ``reinspect`` 在方案 §6.3 的编号表里没有类型段,所以 ``DocKind`` 里没有它,单独补一行。
**别写成"六档一一对应"的循环** —— 两头各有一个孤儿,循环写出来必错一头。

⚠️ ``agents/supervision/tools.py`` 有一份**同样派生**的表(``_DOC_TYPE_ZH``),
两份唯一手写的格子是「reinspect → 复查记录」这一行。为什么没收敛成一份:
  · 不能 import tools.py —— 它拉 langchain,而本模块是被 langgraph 按文件路径加载的 HTTP 层;
  · 也不能下沉到 ``scoping.py`` —— 那个模块的 import 白名单里**没有** ``core/doc_no``,
    而白名单是被 ``test_supervision_scoping.py`` 用 AST 钉死的(见那边头注)。
真要收敛,该动的是 ``core/doc_no.py``(给它一档 reinspect 并同步 ``DOC_TITLE_ZH``),
那是另一件事。在那之前,``test_supervision_api.py`` 有一条断言把两份钉成逐字相同。
"""

_UNNAMED_DOC_TYPES: Final[tuple[str, ...]] = tuple(
    t for t in hazards.DOC_TYPES if t not in _DOC_TYPE_ZH
)
if _UNNAMED_DOC_TYPES:  # pragma: no cover —— 只在两张词表漂了时触发
    raise RuntimeError(
        f"文书类型 {_UNNAMED_DOC_TYPES} 没有中文名 —— db/hazards.py 的 DOC_TYPES 加了档,"
        "supervision_api.py 的 _DOC_TYPE_ZH 要跟上。做成导入时硬失败是刻意的:漏配的表现是"
        "详情面板上冒出一个英文类型词,而那不会有任何报错"
    )

_STATUS_DELETED: Final[str] = "deleted"
"""``POST /supervision/reject`` 回执里的 ``status``。

🔴 **全项目唯一一个不属于 ``db.STATUSES`` 八档的状态值。** 它不是状态机里新加的一档 ——
那一行已经从库里**删掉**了,压根没有状态可报,而回执的形状又是冻结的(``status`` 这个键
必须在)。做成命名常量而不是就地写字面量,理由同 ``_REINSPECT_NO_MARK``:
将来有人给状态机加档时,能一眼看见这个值是**刻意**在八档之外的。

下面那道导入期硬失败守着「哪天真有人往 ``STATUSES`` 里加一档叫 deleted」——
撞上了的表现是前端分不清「这条被否决了」和「这条处于 deleted 状态」,
而两种情况该做的事完全相反(刷新划掉 vs 继续处置)。
``test_supervision_api.py`` 里另有一条把这个约束写成人看得见的形式。
"""

if _STATUS_DELETED in hazards.STATUSES:  # pragma: no cover —— 只在有人加了同名状态时触发
    raise RuntimeError(
        f"db/hazards.py 的 STATUSES 里出现了 {_STATUS_DELETED!r},"
        "它和 supervision_api.py 用来表示「这条已被否决删除」的哨兵值撞了 —— "
        "换一个状态名,或者给 reject 的回执另挑一个哨兵值"
    )

# 会签发的五种文书 ``ISSUING_KINDS`` 与「它们的 doc_type 都在 hazards.DOC_TYPES 里」
# 那条导入时校验,随正文一起搬去了 ``agents/supervision/documents.py``:那张表同时管着
# 正文构造函数的完整性和证据链表的中文名反查,跟正文分不开。本模块 import documents,
# 所以那条校验照样在**导入时**炸,不是等到真签发那一刻。

_GRADABLE_STATUSES: Final[frozenset[str]] = frozenset(hazards.GRADABLE_STATUSES)
"""允许人工改级的状态:**还没签过任何文书的那两档**。

⚠️ **从 db 那一份派生,这里不许再手抄一遍。** ``db/hazards.py`` 的 ``_SET_GRADE_SQL``
拿同一份拼 ``WHERE status IN (…)``(写前状态守卫);两份漂开的表现是端点先放行、库再拒,
工友拿到「状态刚被改过」这种驴唇不对马嘴的提示,而两边看各自的代码都觉得自己没错。

**这里这道判断只为说人话**(它能说清「这条现在是已签发通知单」),真正说了算的是
``set_grade`` 的 rowcount —— 先读与写之间有并发窗口,拿到 False 一律 409。

为什么定成"签发前才可改":本批**不做重签**。通知单已经按「一般」出了稿,库里却改成「严重」,
证据链当场自相矛盾,而那份纸还在工地上贴着。
真定错了,走复查/升级流程,或按新证据另立一条隐患(新照片、新编号)。
"""

_MAX_CONFIRM_BATCH: Final[int] = 200
"""一次批量确认的条数上限。防的是一个巨大的 JSON 数组把一次请求拖成几百个事务。
200 远大于一次巡检能出的隐患数,够用且有边界。"""

_MAX_REASON_ECHO: Final[int] = 2
"""全批都没确认成时,user_msg 里最多复述几条原因。多了就成一堵墙,工友读不完。"""

_REINSPECT_TAIL_BYTES: Final[int] = 4
"""复查行 ``doc_no`` 的随机尾字节数(8 位十六进制)。"""

_REINSPECT_NO_MARK: Final[str] = "#FC-"
"""复查记录行的编号分隔标记 —— 长相刻意**不像文书编号**(``GYT-XX-…``)。

``hazard_docs.doc_no`` 是 NOT NULL UNIQUE,复查行也得占一格;而 ``core/doc_no.py``
的 ``DocKind`` **没有 reinspect 这一档**(方案 §6.3 的编号表里复查行没有类型段),
它的头注同时警告「别在调用点就地拼一个文书编号的样子:就地拼的格式没人给它写正则,
将来挂守卫时认不出来」。

所以这里给的是 ``<隐患编号>#FC-<8位hex>``:全局唯一(隐患编号本身唯一 + 随机尾),
而且**一眼看得出不是文书编号** —— 没人会拿它去对 ``PATTERNS`` 里任何一条正则。
⚠️ 哪天复查要发正式编号,回 ``core/doc_no.py`` 加一档 ``DocKind`` 并同步
``DOC_TITLE_ZH``,**别把这个格式改成 ``GYT-XX-`` 的样子**。
"""

_SIGN_RETRY_MAX: Final[int] = 2
"""文书编号撞库后,整轮(重摇号 + 重渲染 + 重落盘)最多再来几次。

为什么必须重渲染而不是只换库里那一列:**编号印在文书正文里**(元信息表第一行)。
只换库里的号,纸上的号和台账就对不上 —— 那份文书自己证伪自己
(与 ``checkin_api._persist_checkin`` 的「换编号从水印起重来」同一条理由)。
上一轮已落盘的文件成孤儿,交清理器,§6.4 认了这笔账。

为什么是 2 而不是「转到成功」:4 位随机尾 + 秒级时刻,连撞多次几乎一定不是运气问题,
一直转只会把一次可诊断的失败拖成一个挂死的请求(同 ``doc_no.DEFAULT_ATTEMPTS`` 的账)。
"""

_MSG_DOC_NO_EXHAUSTED: Final[str] = (
    "这次没能给文书排上编号,请过几秒再点一次。文书没有出,隐患状态也没变。"
)
"""编号摇不出来时的人话。

后半句是关键:**明确告诉工友"什么都没发生"**,否则他会以为文书已经出了一半,
去翻列表找一份并不存在的文书。``DocNoExhaustedError`` 的原文只进日志
(它的头注:异常文本别原样透给用户)。
"""

_MSG_RACED: Final[str] = (
    "这条隐患的状态或级别刚被改过(可能有人同时在处理),什么都没签出去。刷新一下再看看。"
)
"""db 层 rowcount=0 时的人话 —— 先读的那份快照说可以、写的时候不行,就是并发。

2026-08-21 扩了两处措辞:
  · 加「或级别」—— 那天给 ``_NOTIFY_SQL`` / ``_SUSPEND_SQL`` 补了 ``AND grade = ?``
    的写前守卫,于是 rowcount=0 又多了一种成因。只写「状态」会让人去翻状态、翻不出东西。
  · 加「什么都没签出去」—— 这条路上盘里其实已经渲了文书文件(``_sign`` 的头注:
    409 时「库一行没动,盘上留孤儿文件」)。不说清楚的话,监理会去列表里找一份
    并不存在的文书,和 ``_MSG_DOC_NO_EXHAUSTED`` 当初要防的是同一种误会。
"""


# ---------------------------------------------------------------------------
# 端点允许的起始状态 —— 声明在这里,导入时对着 db 的迁移表校验
# ---------------------------------------------------------------------------


def _sources(target: str, *sources: str) -> frozenset[str]:
    """声明"某个端点允许的起始状态",并当场校验它是 ``ALLOWED_TRANSITIONS`` 的子集。

    手法照搬 ``db/hazards.py`` 的 ``_sources_for``(那个是私有的,不跨模块 import)。
    为什么端点这一侧还要自己声明一遍、而不是直接问 ``can_transition``:
    端点往往**比迁移表更窄**。``notified → closed`` 是合法边(复查合格直接销项),
    但《复工令》端点只该接 ``resuming`` —— 拿 ``can_transition`` 当判据的话,
    一条 ``notified`` 的隐患会先在这里被放行,再被 db 的 WHERE 拦下,
    工友拿到的是「状态刚被改过」这种驴唇不对马嘴的提示。

    **但永远不许更宽**:更宽的那一条就是绕过状态机的后门。这里在**导入时**炸掉,
    进程起不来 —— 与 db 层同一手法:少了同步的东西不会有任何运行期报错,那就让它连起都起不来。
    """
    illegal = tuple(
        s for s in sources if target not in hazards.ALLOWED_TRANSITIONS.get(s, frozenset())
    )
    if illegal:  # pragma: no cover —— 只在有人改错状态机表时触发
        raise RuntimeError(
            f"端点声明的起始状态 {illegal} 在 db 的迁移表里到不了 {target};"
            "supervision_api 与 db/hazards.py 的状态机漂了,先对齐那张表"
        )
    return frozenset(sources)


_NOTICE_FROM: Final = _sources(hazards.STATUS_NOTIFIED, hazards.STATUS_OPEN)
_SUSPEND_FROM: Final = _sources(hazards.STATUS_SUSPENDED, hazards.STATUS_OPEN)
# 复工令只接 resuming:它是给**停过工**的隐患收尾的。
_RESUME_FROM: Final = _sources(hazards.STATUS_CLOSED, hazards.STATUS_RESUMING)
# 升级只从 reinspect_failed 进:举证链是「我通知过 + 期限到了 + 复查过 + 他没改」,
# 没复查过就升级 = 拿一份建立在自己记乱账上的材料去指控施工方。
_ESCALATE_FROM: Final = _sources(hazards.STATUS_ESCALATED, hazards.STATUS_REINSPECT_FAILED)
_REINSPECT_FAIL_FROM: Final = _sources(
    hazards.STATUS_REINSPECT_FAILED,
    hazards.STATUS_NOTIFIED,
    hazards.STATUS_SUSPENDED,
    hazards.STATUS_REINSPECT_FAILED,
)
# 复查合格有两条出口(closed / resuming),由 db 按 was_suspended 挑边 —— 这里取并集:
# 端点只负责判断「现在能不能复查」,**绝不自己挑边**(Codex#6:挑错的两种后果分别是
# 漏发复工令和滥发复工令)。
_REINSPECT_PASS_FROM: Final = _sources(
    hazards.STATUS_CLOSED, hazards.STATUS_NOTIFIED, hazards.STATUS_REINSPECT_FAILED
) | _sources(hazards.STATUS_RESUMING, hazards.STATUS_SUSPENDED, hazards.STATUS_REINSPECT_FAILED)


# ---------------------------------------------------------------------------
# 响应外壳与"被拒"这件事的表达
# ---------------------------------------------------------------------------


class _Result(NamedTuple):
    """一次处置动作的结果:HTTP 状态码 + 四键信封。线程池里的同步活返回它。"""

    status: int
    envelope: Envelope


class _Refused(Exception):
    """这次动作被拦下了,带着要回给前端的状态码与信封。

    为什么用异常而不是 union 返回值:签发是「查 → 四道闸 → 算期限 → 渲染 → 落盘 → 写库」
    一条直线,每道闸都可能拦。用返回值表达的话每步后面都要跟一个 isinstance 分支,
    真正的业务顺序会被淹掉。而这个异常**不出本模块** —— 每个动作都在 ``_guarded``
    里被接住转成 ``_Result``,不存在"拿异常当控制流还漏到外面"的风险。
    """

    def __init__(self, status: int, envelope: Envelope) -> None:
        super().__init__(envelope["user_msg"])
        self.status = status
        self.envelope = envelope


def _refuse(status: int, code: ErrorCode, user_msg: str, *, detail: str = "") -> _Refused:
    """拼一个 ``_Refused``。``detail`` 只进日志(``fail()`` 保证它不进返回值)。"""
    return _Refused(status, fail(code, user_msg, detail=detail))


def _respond(env: Envelope, status: int) -> JSONResponse:
    """Envelope → JSON 响应。进了 handler 之后所有出口都走这里,一个都不许裸拼 dict。"""
    return JSONResponse(env, status_code=status)


def _zh(status: str) -> str:
    """状态 → 中文。词表(``scoping.STATUS_ZH``)在导入时已校验覆盖八档,
    这里的 ``.get`` 兜底只为不让展示层炸掉 —— **不是**允许漏配的意思。"""
    return scoping.STATUS_ZH.get(status, status)


def _filename(doc_type: str, doc_no: str) -> str:
    """一份文书落盘时的文件名:``<中文名>_<编号>.docx``。

    🔴 **签发那条路(``_issue_documents``)与详情那条路(``_hazard_doc_payload``)共用它,
    谁都不许现拼第二份。** 不同源的后果是界面上下载卡的文件名和实际落盘的对不上 ——
    **而且不会有任何报错**:卡片照渲、文件照下,只是名字换了一个,
    等有人拿着文件名去盘上对账那天才发现。``test_supervision_api.py`` 有一条
    真去比对两条路产出的文件名。

    只接**已签发的五种文书**的 doc_type。复查记录行不走这里(它没有文件,
    ``filename`` 给 None)—— 硬喂进来会拼出「复查记录_GYT-H-…#FC-….docx」这种
    并不存在的文件名。判据写在调用点:**有没有 artifact_id**,不是看 doc_type。
    """
    return f"{_DOC_TYPE_ZH[doc_type]}_{doc_no}.docx"


# ---------------------------------------------------------------------------
# 鉴权:纵深防御的第二道(与 checkin_api._deny_if_token_bad 同源同判据)
# ---------------------------------------------------------------------------


def _deny_if_token_bad(request: Request) -> JSONResponse | None:
    """handler 自查令牌。放行返回 None,不过返回 401。

    第一道在 langgraph 的鉴权中间件(``langgraph.json`` 的
    ``enable_custom_route_auth: true``);这道防的是那个键被漏配 —— 漏配时第一道
    **整条消失且没有任何报错**,只有这道自查兜得住,也只有它能被单元测试钉死。

    判定与 ``auth.py`` / ``checkin_api.py`` 完全同源(同一份 ``core/access.py``):
    空 / 占位符 / 过短 = 未配置 = 放行 —— ``make dev`` 与真机验收不发令牌头,
    「未配置也拦」等于当场打死本机联调。

    ⚠️ 每次请求现取(``effective_access_token`` 内部走 ``get_settings()``),
    **不许在 import 时读成常量** —— 那样测试换环境变量测不到,生产换口令要重启才生效。

    拒绝文案取 ``DEFAULT_USER_MSG[UNAUTHORIZED]``,与 ``auth.DENY_MESSAGE`` /
    ``attendance/messages.DENY`` 一字不差(同源清单里那条:任何差异都是送给爆破脚本的信号)。
    """
    expected = effective_access_token()
    if not expected:
        return None
    presented = _api_key_from_headers(request.headers)
    # compare_digest:恒定时间比对,不给逐位试探留统计量;encode 成 bytes 防非 ASCII
    # 口令把 401 变成 500(理由原样见 auth.authenticate)。
    if presented and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        return None
    logger.warning(
        "监理接口鉴权失败:path=%s 原因=%s",
        request.url.path,
        "请求头里没有 X-Api-Key(或为空)" if not presented else "令牌不匹配",
    )
    return _respond(fail(ErrorCode.UNAUTHORIZED), 401)


# ---------------------------------------------------------------------------
# 请求体解析
# ---------------------------------------------------------------------------


async def _json_body(request: Request) -> dict[str, Any]:
    """收一个 JSON 对象。坏 JSON / 不是对象一律 400,不猜。"""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 —— 坏 JSON 的异常类型随实现变,一律收敛成 400
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "这次提交的内容后台没读懂,刷新一下页面再试一次。",
            detail=f"请求体不是合法 JSON:{exc}",
        ) from exc
    if not isinstance(body, dict):
        raise _refuse(400, ErrorCode.INVALID_INPUT, "这次提交的内容格式不对,刷新一下再试。")
    return body


def _text(body: dict[str, Any], key: str) -> str:
    """取一个字符串字段并去掉首尾空白;缺失 / 非字符串都归一成空串,由各处自己说人话。"""
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


_ISSUED_BY_MAX_LEN: Final[int] = 40
"""签发人名字的长度上限。姓名 + 职务(「陳大文 總監理工程師」)绰绰有余,
而它会原样进库、将来会出现在导出的台账里,不设上限等于开一个塞任意长文本的口子。"""


def _issued_by(body: dict[str, Any]) -> str | None:
    """签发人**自己报的名字**。空 → None(允许,理由见下)。

    ===========================================================================
    为什么不做成必填
    ---------------------------------------------------------------------------
    做成必填会有一个很难看的后果:界面上没填名字 → 400 → 而这时候文书**已经渲染
    落盘了**(``_sign`` 的孤儿文件那条)。为了一个补充性的审计字段去挡住法律文书的
    签发,取舍不划算。

    所以规矩是:**界面负责问、后端负责记**。界面上那颗确认按钮会把名字带上来
    (``supervision.tsx`` 的签发人输入框),没带就记 NULL —— 与 2026-08-21 之前
    那批旧数据同一个形状,查的人一眼看得出「这条没记到人」,而不是看到一个编的名字。

    🔴 **它不是认证身份**,别拿它做任何权限判断。完整推演在
    ``db/hazards.DocDraft.issued_by`` 的红字。

    超长直接截断而不是报错:同上,不值得为它挡住签发。截断留档比拒绝签发安全。
    """
    name = _text(body, "issued_by")
    return name[:_ISSUED_BY_MAX_LEN] or None


# ---------------------------------------------------------------------------
# 四道闸:隐患存在 → 已定级 → 级别方向 → 状态机
# ---------------------------------------------------------------------------


def _require_hazard(hazard_no: str) -> hazards.HazardRow:
    """取隐患行;编号为空或查不到都在这里拦下。**写入那八条端点共用它。**

    ⚠️ 查询那条路(``_work_get_hazard``)刻意**不走这里**,它的 404 那句抄的是
    ``agents/supervision/tools.get_hazard``:面板上点开一条隐患和在对话里问同一条,
    是同一个动作,两处说法不一样会让人以为查的是两个台账。这里这句在"动作"的语境里说
    (「签发时没找到这条隐患」),两句都是人话,不是漏改。
    """
    if not hazard_no:
        raise _refuse(400, ErrorCode.INVALID_INPUT, "没说是哪条隐患(缺隐患编号)。")
    row = hazards.fetch(hazard_no)
    if row is None:
        raise _refuse(404, ErrorCode.NOT_FOUND, f"没找到隐患「{hazard_no}」,核对一下编号。")
    return row


def _require_graded(row: hazards.HazardRow) -> None:
    """硬拦③:``needs_grading=1`` 的隐患,**任何签发都拒**(Codex#11)。

    必须排在级别方向那两道闸**之前**:没定级的行 ``grade`` 是映射表给的默认值(一般),
    先报「这条是一般隐患,不能签暂停令」会把人带去改级别以外的地方,而真正要做的是定级。
    """
    if row.needs_grading:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」还没定级(现场判的是「{row.severity}」),"
            "先在界面上定成一般隐患或严重隐患,再签文书。",
        )


def _refuse_severe_notice(row: hazards.HazardRow) -> None:
    """硬拦①:严重隐患**不许只签通知单**,得走三文书那条路。"""
    if row.grade == hazards.GRADE_SEVERE:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」是严重隐患,只发一份通知单不够,"
            "要走「签发暂停令」(通知单 + 暂停令 + 致建设单位报告,一次三份)。",
        )


def _refuse_normal_suspend(row: hazards.HazardRow) -> None:
    """硬拦②:一般隐患**不许签暂停令** —— 平白停一片人的工。"""
    if row.grade != hazards.GRADE_SEVERE:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」是{row.grade}隐患,不能签暂停令,"
            "走「签发监理通知单」就够了。确实要停工,先把它改定为严重隐患。",
        )


def _require_status(row: hazards.HazardRow, allowed: frozenset[str], action: str) -> None:
    """状态机闸:当前状态不在这个动作的起始状态集里就拦下,并把当前状态说成人话。

    ⚠️ 这只是**给人话用的前置判断**。真正说了算的是 db 层 UPDATE 的 rowcount ——
    先读与写之间有并发窗口,拿到 False 一律回 409(见 ``_MSG_RACED``)。
    """
    if row.status not in allowed:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{row.hazard_no}」现在是「{_zh(row.status)}」,这一步({action})做不了。",
        )


def _resolve_due(due_phrase: str, today: date) -> tuple[str, str]:
    """用户原话 → ``(ISO 日期, 「8月20日(周四)」)``。**解析不出就拒,绝不留空。**

    换算全部交给 ``agents/schedule/dates.py``(Codex#12 点名复用),本模块一行日期
    数学都不写。``DueParseError`` 的文本本来就是给工地师傅看的人话(还附了正确写法),
    原样透出去比重新包装一层有用得多。
    """
    if not due_phrase:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "得写明整改期限(比如「明天」「3天后」「下周三」「月底」)。"
            "没有期限的隐患不会进超期清单,也就永远不会有人来催。",
        )
    try:
        resolved = parse_due(due_phrase, today=today)
    except DueParseError as exc:
        raise _refuse(400, ErrorCode.INVALID_INPUT, str(exc)) from exc
    if resolved is None:  # pragma: no cover —— 空串上面已拦,这里纯防御
        raise _refuse(400, ErrorCode.INVALID_INPUT, "这个期限没看懂,换个说法,比如「下周三」。")
    return resolved.isoformat(), format_display(resolved)


def _require_photo(after_photo_id: str) -> str:
    """复查照片必填,而且必须是**真实存在的一张照片**(方案 §5.2 红线)。

    §5.2 那条红线的原话是「拿不到照片就没有任何路径能把状态改成 closed」——
    所以"拿到的"必须真是**照片**,而且正文文件真的**还在**。三道各拦一种事故:

    ① 空 → 400。只校验非空不够,但连非空都不校验就更没边。
    ② 编号取不到 sidecar → 404。编号打错一位照样非空,而复查合格是能把隐患销项的:
       「隐患已消除」被写进留档文书,现场却原样没动。
    ③ **``kind`` 不是 PHOTO → 400。** 只问 ``read_meta`` 拿得到拿不到的话,随手抓一个
       我们自己生成的**文书** artifact_id(签发通知单时回给前端的那几个,就摆在界面上)
       就能通过校验、把隐患销项 —— 复查证据成了「我们自己出的那张纸」,一张现场照片都没有。
    ④ **正文文件不在 → 404。** sidecar 与正文是两个文件,清理器 / 手工删档 / 落盘半截
       都可能只剩 sidecar;``resolve`` 会去 ``is_file()`` 问一次。
       拿一个"只剩元数据"的产物销项,等于证据链里挂着一张点不开的照片。

    ③ 与 ④ 的人话必须**不一样**:拿错编号(该去重找那张照片)和文件丢了(该重新传一张)
    是两回事,给同一句话的话工友会一直核对一个本来就没错的编号。
    """
    if not after_photo_id:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "复查要挂一张整改后的现场照片:先把照片传上来,再拿它的编号来下结论。",
        )
    try:
        meta = artifacts.read_meta(after_photo_id)
    except ArtifactNotFound as exc:
        raise _refuse(
            404,
            ErrorCode.NOT_FOUND,
            "没找到这张复查照片,核对一下照片编号,或者重新传一张。",
            detail=f"复查照片 {after_photo_id!r} 取不到:{exc}",
        ) from exc

    kind = str(meta.get("kind", ""))
    if kind != ArtifactKind.PHOTO.value:
        # 不透 kind 的英文枚举值(REPORT / DRAWING 不是工地上的话),只说"不是照片"。
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "这个编号指的不是照片(像是文书或图纸)。复查要挂的是整改后的现场照片,"
            "先把照片传上来,再拿照片的编号来下结论。",
            detail=f"复查照片 {after_photo_id!r} 的 kind={kind!r},不是 PHOTO",
        )

    try:
        artifacts.resolve(after_photo_id)
    except ArtifactNotFound as exc:
        raise _refuse(
            404,
            ErrorCode.NOT_FOUND,
            "这张照片的文件已经找不到了(可能被清理掉了),重新传一张再来下结论。",
            detail=f"复查照片 {after_photo_id!r} 的正文文件缺失:{exc}",
        ) from exc
    return after_photo_id


# ---------------------------------------------------------------------------
# 文书:正文归 documents(写什么字)、版式归 docgen(长什么样),这里只管出口形状
# ---------------------------------------------------------------------------


class _IssuedDoc(NamedTuple):
    """一份已经落盘、等着写库的文书。``documents`` 数组里的一项就是它。"""

    doc_type: str
    doc_no: str
    artifact_id: str
    filename: str

    def as_payload(self) -> dict[str, str]:
        """冻结契约里那四个键,顺序即字段顺序。"""
        return {
            "doc_type": self.doc_type,
            "doc_no": self.doc_no,
            "artifact_id": self.artifact_id,
            "filename": self.filename,
        }


# ---------------------------------------------------------------------------
# 签发编排:① 渲染 → ② 落盘 → ③ 一个事务写库(§6.4),撞号则整轮重来
# ---------------------------------------------------------------------------


def _draw_doc_no(kind: DocKind, snap: TimeSnapshot, taken: set[str]) -> str:
    """摇一个文书编号,并保证**本次动作内**不自撞。

    ⚠️ ``exists`` 只看本次已摇出来的号,**不查全表** —— ``db/hazards.py`` 没有
    「这个 doc_no 占了没」的查询入口,而绕过存储层直连库是更坏的选择(S4 不动 db/)。
    真正的兜底本来就是建表时那条 ``doc_no TEXT NOT NULL UNIQUE`` + 下面
    ``_sign`` 对 ``sqlite3.IntegrityError`` 的整轮重试 —— ``generate_unique`` 的头注
    第 3 条写得很清楚:它只是把绝大多数碰撞挡在写库之前,不是替代唯一索引。

    这一层挡的是**三份文书自己撞上自己**:三份共用一个时间快照(编号里的时刻必须一致,
    否则同一次签发在纸面上变成三个时间点),差别只剩 4 位随机尾。
    """
    doc_no = generate_unique(kind, taken.__contains__, snap=snap)
    taken.add(doc_no)
    return doc_no


def _issue_documents(
    kinds: Sequence[DocKind], ctx: DocContext, snap: TimeSnapshot
) -> list[_IssuedDoc]:
    """① 全部渲染到内存,② 再逐份落盘。**两步严格分开。**

    合成一步(渲一份存一份)的后果:第二份渲染炸掉时第一份已经在盘上,而它既没进库
    也没人认识它 —— 而分开之后,渲染阶段任何异常都发生在一个字节都还没落盘之前。
    """
    taken: set[str] = set()
    drafted = [(kind, _draw_doc_no(kind, snap, taken)) for kind in kinds]
    rendered = [(kind, doc_no, render_doc(kind, doc_no, ctx)) for kind, doc_no in drafted]

    issued: list[_IssuedDoc] = []
    for kind, doc_no, payload in rendered:
        # ``kind.name.lower()`` 就是 hazard_docs 的 doc_type(``doc_no.DocKind`` 头注),
        # 而文件名从**同一个** doc_type 经 ``_filename`` 拼 —— 详情端点回头拿库里那一列
        # 也走这条路,两边才咬得死。五种签发文书必在 ``_DOC_TYPE_ZH`` 里:
        # ``documents.ISSUING_KINDS`` 有一条导入期校验盯着「它们的 doc_type 都在
        # hazards.DOC_TYPES 里」,而本模块的 ``_UNNAMED_DOC_TYPES`` 盯着「DOC_TYPES 都有中文名」。
        doc_type = kind.name.lower()
        filename = _filename(doc_type, doc_no)
        # kind=REPORT:与巡检记录同类(系统生成的 docx 产物)。**不用 DOCUMENT** ——
        # 那一档在 webapp.py 里是"用户上传的规范/任务书",混进去资料库会把监理文书
        # 列成规范。也不用 ATTENDANCE:清理器只删那一类,文书要长期留档。
        artifact_id = artifacts.register(payload, kind=ArtifactKind.REPORT, original_name=filename)
        issued.append(
            _IssuedDoc(
                doc_type=doc_type,
                doc_no=doc_no,
                artifact_id=artifact_id,
                filename=filename,
            )
        )
    return issued


def _sign(
    kinds: Sequence[DocKind],
    ctx: DocContext,
    snap: TimeSnapshot,
    apply: Callable[[list[hazards.DocDraft]], bool],
    *,
    issued_by: str | None,
) -> list[_IssuedDoc]:
    """签发 N 份文书并落库。返回 ``documents`` 数组的素材(顺序 = ``kinds`` 的顺序)。

    ``apply`` 是 db 层那一个事务(``mark_notified`` / ``mark_suspended`` / …):
    状态迁移 + 挂文书一起写,迁移没命中就一行都不写。

    三种结局:
      · 正常     → 返回落盘 + 落库都成了的那几份;
      · 撞号     → ``sqlite3.IntegrityError``(``doc_no UNIQUE``),整轮重来
                   (重摇号 + **重渲染**,因为编号印在正文里),旧文件成孤儿;
      · 状态被抢 → ``apply`` 回 False,409。**库一行没动**,盘上留孤儿文件(§6.4 认了)。

    ``issued_by`` 是签发人**自己报的名字**(2026-08-21 加,语义见
    ``db/hazards.DocDraft.issued_by`` 的红字)。**必填形参、可以是 None** ——
    做成关键字必填是为了让新加的签发路径必须停下来想一句「这条路谁签的」,
    而不是默默地又落一批查不出人的文书。
    """
    for attempt in range(1 + _SIGN_RETRY_MAX):
        issued = _issue_documents(kinds, ctx, snap)
        drafts = [
            hazards.DocDraft(
                doc_type=d.doc_type,
                doc_no=d.doc_no,
                artifact_id=d.artifact_id,
                issued_by=issued_by,
            )
            for d in issued
        ]
        try:
            moved = apply(drafts)
        except sqlite3.IntegrityError as exc:
            # doc_no 撞了库里已有的号。db 层刻意不吞这个异常(它的头注:吞了就等于把
            # "该重试"变成静默失败),重试就在这里。
            logger.warning("文书编号撞库(第 %d 次),换随机尾重来:%s", attempt + 1, exc)
            continue
        if not moved:
            raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)
        return issued
    raise _refuse(
        500,
        ErrorCode.INTERNAL,
        _MSG_DOC_NO_EXHAUSTED,
        detail=f"文书编号连续 {1 + _SIGN_RETRY_MAX} 轮撞库,疑似随机源或台账异常",
    )


def _context(row: hazards.HazardRow, snap: TimeSnapshot, due_display: str) -> DocContext:
    """凑齐渲染素材:项目名 + 证据链。两次查询,不在循环里逐条查。"""
    project = projects_db.get_project(row.project_id) if row.project_id else None
    return DocContext(
        row=row,
        project_name=project.name if project is not None else UNASSIGNED_PROJECT_ZH,
        signed_display=snap.display,
        due_display=due_display,
        evidence=tuple(hazards.docs_of([row.hazard_no])),
    )


def _issued_payload(
    hazard_no: str, status: str, issued: Iterable[_IssuedDoc], user_msg: str
) -> _Result:
    """冻结契约那三个键 + 四键信封。**所有出文书的动作都从这一个出口走。**

    散在各处拼 dict 的下场是某一处漏掉 ``documents`` 或换了键名,而前端只是少渲一张卡、
    不报错(Codex#16 要冻结形状的原因)。
    """
    return _Result(
        200,
        ok(
            {
                "hazard_no": hazard_no,
                "status": status,
                "documents": [d.as_payload() for d in issued],
            },
            user_msg,
        ),
    )


# ---------------------------------------------------------------------------
# W9 那七个写入动作的同步实现(全部在线程池里跑)。
# W10 的三条(两条查询 + 否决)在下面自己一节,理由与判据都不一样,别混着读。
# ---------------------------------------------------------------------------


def _work_confirm(body: dict[str, Any]) -> _Result:
    """pending → open 的人工确认闸(D17)。**支持批量,每条一个事务、互不牵连。**

    没有这一步,safety 看一眼照片就能开启法律流程 —— 自动登记的东西必须有人认过。
    """
    raw = body.get("hazard_nos")
    if raw is None:
        single = _text(body, "hazard_no")
        raw = [single] if single else []
    if not isinstance(raw, list):
        raise _refuse(400, ErrorCode.INVALID_INPUT, "隐患编号要放在一个列表里。")
    # 去重保序:界面上双击会把同一条发两遍,不去重的话第二条会被报成「不用再确认」,
    # 看起来像出了错,其实什么问题都没有。
    nos = list(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))
    if not nos:
        raise _refuse(400, ErrorCode.INVALID_INPUT, "没说要确认哪几条隐患。")
    if len(nos) > _MAX_CONFIRM_BATCH:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            f"一次最多确认 {_MAX_CONFIRM_BATCH} 条,分几次来。",
        )

    confirmed: list[str] = []
    failed: list[dict[str, str]] = []
    for no in nos:
        # 先试着确认(db 的 WHERE status='pending' 是硬守卫),**失败了才回查**去解释原因 ——
        # 反过来先查再改就是每条两次库,而 db 层本来就要求"每条一个事务"。
        if hazards.confirm(no):
            confirmed.append(no)
            continue
        row = hazards.fetch(no)
        reason = (
            "查不到这条隐患,核对一下编号。"
            if row is None
            else f"这条现在是「{_zh(row.status)}」,不用再确认。"
        )
        failed.append({"hazard_no": no, "reason": reason})

    if not confirmed:
        # 全军覆没:``fail()`` 的 data 恒为 None(信封契约),原因只能进 user_msg,
        # 所以最多复述两条 —— 再多就成一堵墙,工友读不完。
        echo = "".join(
            f"{item['hazard_no']}:{item['reason']}" for item in failed[:_MAX_REASON_ECHO]
        )
        more = (
            f"(还有 {len(failed) - _MAX_REASON_ECHO} 条)" if len(failed) > _MAX_REASON_ECHO else ""
        )
        raise _refuse(409, ErrorCode.CONFLICT, f"这 {len(failed)} 条都没能确认。{echo}{more}")

    tail = f",另有 {len(failed)} 条没确认成(可能已经确认过了)" if failed else ""
    return _Result(
        200,
        ok(
            {"confirmed": confirmed, "failed": failed},
            f"已确认 {len(confirmed)} 条隐患{tail}。确认后就能签发文书了。",
        ),
    )


def _work_grade(body: dict[str, Any]) -> _Result:
    """人工定级:改 grade 并清掉 ``needs_grading`` —— 未定级隐患唯一的解锁通道。"""
    hazard_no = _text(body, "hazard_no")
    grade = _text(body, "grade")
    if grade not in hazards.GRADES:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            f"级别只能填「{hazards.GRADE_NORMAL}」或「{hazards.GRADE_SEVERE}」。",
        )
    row = _require_hazard(hazard_no)
    if row.status not in _GRADABLE_STATUSES:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{hazard_no}」已经签过文书(现在是「{_zh(row.status)}」),不能改级别 —— "
            "改了的话已发出去的那份文书就和台账对不上了。定错了请走复查或升级流程。",
        )
    # 上面那道 ``_GRADABLE_STATUSES`` 判断用的是**先读的快照**,读完到这一行之间有窗口:
    # 另一个请求可能已经把暂停令签了(status → suspended、三份文书已落盘),也可能把这条
    # pending 否决删掉了。真正说了算的是 db 那条 ``UPDATE … WHERE status IN (…)`` 的 rowcount
    # —— 拿到 False 一律 409,**绝不信先读的那份快照**(没有这道守卫,库里会留下
    # 「一般隐患 + 已出暂停令」这种永久矛盾,而且一声不吭)。
    if not hazards.set_grade(hazard_no, grade):
        raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)

    hint = (
        "可以签发《工程暂停令》了(一次出三份文书)。"
        if grade == hazards.GRADE_SEVERE
        else "可以签发《监理通知单》了。"
    )
    return _Result(
        200,
        ok(
            {
                "hazard_no": hazard_no,
                "status": row.status,
                "grade": grade,
                "needs_grading": False,
                "documents": [],  # 定级不出文书,但形状保持一致,前端不用分叉
            },
            f"隐患「{hazard_no}」已定为{grade}隐患。{hint}",
        ),
    )


def _work_notice(body: dict[str, Any]) -> _Result:
    """《监理通知单》一份(grade=一般 那条路)。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③ —— 必须排在级别方向之前
    _refuse_severe_notice(row)  # 硬拦①
    _require_status(row, _NOTICE_FROM, "签发监理通知单")
    due_date, due_display = _resolve_due(_text(body, "due_phrase"), snap.stamp.date())

    ctx = _context(row, snap, due_display)
    issued = _sign(
        (DocKind.NOTICE,),
        ctx,
        snap,
        lambda drafts: hazards.mark_notified(
            row.hazard_no, due_date, expected_grade=row.grade, docs=drafts
        ),
        issued_by=_issued_by(body),
    )
    logger.info("隐患 %s 已签发通知单 %s(期限 %s)", row.hazard_no, issued[0].doc_no, due_date)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_NOTIFIED,
        issued,
        f"《监理通知单》已出稿(编号 {issued[0].doc_no}),整改期限 {due_display}。"
        "这份文书要总监理工程师签字盖章后才是正式文件。",
    )


def _work_suspend(body: dict[str, Any]) -> _Result:
    """三份文书原子产出(grade=严重 那条路):通知单 → 暂停令 → 致建设单位报告。

    顺序是冻结契约的一部分(前端按数组渲染下载卡),也是文书之间的引用顺序。
    """
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③
    _refuse_normal_suspend(row)  # 硬拦②
    _require_status(row, _SUSPEND_FROM, "签发工程暂停令")
    due_date, due_display = _resolve_due(_text(body, "due_phrase"), snap.stamp.date())

    ctx = _context(row, snap, due_display)
    issued = _sign(
        (DocKind.NOTICE, DocKind.SUSPENSION, DocKind.OWNER_REPORT),
        ctx,
        snap,
        lambda drafts: hazards.mark_suspended(
            row.hazard_no, due_date, expected_grade=row.grade, docs=drafts
        ),
        issued_by=_issued_by(body),
    )
    logger.info(
        "隐患 %s 已出具暂停令三文书:%s(期限 %s)",
        row.hazard_no,
        "、".join(d.doc_no for d in issued),
        due_date,
    )
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_SUSPENDED,
        issued,
        "三份文书已出稿:《监理通知单》《工程暂停令》《致建设单位报告》,"
        f"整改期限 {due_display}。"
        # 🔴 措辞红线:出稿 ≠ 停工。签字盖章之前不得据以停工(D15 的免责句同一条边界)。
        "⚠️ 暂停令要总监理工程师签字盖章后才能据以停工,现在只是出了稿。",
    )


def _work_reinspect_result(body: dict[str, Any]) -> _Result:
    """复查结论(D11:模型给建议,**人下结论**,照片是证据)。不出文书,只留一条复查记录。

    合格之后落到哪个状态**由 db 按 ``was_suspended`` 挑边**(closed / resuming),
    本模块绝不自己挑 —— 挑错的两种后果分别是漏发复工令和滥发复工令(Codex#6)。
    """
    hazard_no = _text(body, "hazard_no")
    result = _text(body, "result")
    if result not in hazards.DOC_RESULTS:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "复查结论只能是合格(pass)或不合格(fail),得由人来下。",
        )
    photo_id = _require_photo(_text(body, "after_photo_id"))
    row = _require_hazard(hazard_no)
    passed = result == "pass"
    _require_status(
        row,
        _REINSPECT_PASS_FROM if passed else _REINSPECT_FAIL_FROM,
        "登记复查结论",
    )

    for attempt in range(1 + _SIGN_RETRY_MAX):
        draft = hazards.DocDraft(
            doc_type="reinspect",
            doc_no=f"{row.hazard_no}{_REINSPECT_NO_MARK}{secrets.token_hex(_REINSPECT_TAIL_BYTES)}",
            photo_id=photo_id,
            result=result,
            # 复查结论**由人下**(D11),所以它同样该留下是谁下的这个结论 ——
            # 「复查合格」是整条链上离销项最近的一步,而它不出文书、没有编号,
            # 在这之前是全链唯一一个连时间以外什么都不记的动作。
            issued_by=_issued_by(body),
        )
        try:
            if passed:
                landed = hazards.pass_reinspection(row.hazard_no, docs=[draft])
            else:
                landed = (
                    hazards.STATUS_REINSPECT_FAILED
                    if hazards.mark_reinspect_failed(row.hazard_no, docs=[draft])
                    else None
                )
        except sqlite3.IntegrityError as exc:  # pragma: no cover —— 8 位随机尾撞库,天文数字
            logger.warning("复查记录编号撞库(第 %d 次),换随机尾重来:%s", attempt + 1, exc)
            continue
        if landed is None:
            raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)
        logger.info("隐患 %s 复查结论 %s → %s", row.hazard_no, result, landed)
        return _Result(
            200,
            ok(
                {
                    "hazard_no": row.hazard_no,
                    "status": landed,
                    # 复查记录**不进 documents**:它没有 artifact_id,进去就是一张点不开的卡。
                    "documents": [],
                    "result": result,
                    "after_photo_id": photo_id,
                },
                _reinspect_msg(landed, row.item),
            ),
        )
    raise _refuse(  # pragma: no cover
        500,
        ErrorCode.INTERNAL,
        _MSG_DOC_NO_EXHAUSTED,
        detail="复查记录编号连续撞库,疑似随机源异常",
    )


def _reinspect_msg(landed: str, item: str) -> str:
    """复查落到哪一站,就说哪一句 —— 三条路各有下一步,含糊一句话工友就不知道还要不要干活。"""
    if landed == hazards.STATUS_CLOSED:
        return f"复查合格,隐患「{item}」已销项。"
    if landed == hazards.STATUS_RESUMING:
        return f"复查合格。这条停过工,还要签发《工程复工令》才算完,隐患「{item}」暂不销项。"
    return f"已记为复查不合格,隐患「{item}」继续跟踪;逾期不改可以上报主管部门。"


def _work_resume(body: dict[str, Any]) -> _Result:
    """《工程复工令》—— 只发给 ``resuming``(停过工 + 复查已合格)。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③ 同样适用:复工令也是签发
    _require_status(row, _RESUME_FROM, "签发工程复工令")

    ctx = _context(row, snap, due_display="")  # 复工令不设新期限
    issued = _sign(
        (DocKind.RESUMPTION,),
        ctx,
        snap,
        lambda drafts: hazards.mark_resumed(row.hazard_no, docs=drafts),
        issued_by=_issued_by(body),
    )
    logger.info("隐患 %s 已签发复工令 %s", row.hazard_no, issued[0].doc_no)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_CLOSED,
        issued,
        f"《工程复工令》已出稿(编号 {issued[0].doc_no}),这条隐患已销项。"
        "文书要总监理工程师签字盖章后才是正式文件。",
    )


def _work_escalate(body: dict[str, Any]) -> _Result:
    """《监理报告》报主管部门 —— 只从 ``reinspect_failed`` 进,正文附完整证据链。"""
    snap = make_snapshot()
    row = _require_hazard(_text(body, "hazard_no"))
    _require_graded(row)  # 硬拦③
    _require_status(row, _ESCALATE_FROM, "上报主管部门")

    ctx = _context(row, snap, due_display="")
    issued = _sign(
        (DocKind.AUTHORITY_REPORT,),
        ctx,
        snap,
        lambda drafts: hazards.mark_escalated(row.hazard_no, docs=drafts),
        issued_by=_issued_by(body),
    )
    logger.info("隐患 %s 已升级上报,监理报告 %s", row.hazard_no, issued[0].doc_no)
    return _issued_payload(
        row.hazard_no,
        hazards.STATUS_ESCALATED,
        issued,
        f"《监理报告》已出稿(编号 {issued[0].doc_no}),附了这条隐患的完整处置经过。"
        "文书要总监理工程师签字盖章后再报建设主管部门。",
    )


# ---------------------------------------------------------------------------
# 查询两条 + 否决一条(W10)—— 同样全部在线程池里跑
#
# 为什么会有这三条:界面上那块处置面板原先挂在**聊天流里的工具返回**上,而 supervisor 的
# ``output_mode="last_message"`` 会把子 Agent 的工具返回整个丢掉 —— 于是面板一次都没打开过
# (连 W3/W5 的巡检记录卡也一样)。修法是照 W7 打卡面板的先例:面板改成界面上的**常驻操作台**,
# 数据自己直连 HTTP,一个字不经过聊天流。完整根因在 ``docs/W10_界面取不到工具返回_方案.md``。
#
# 🔴 **筛子与超期判据一个字都不许在这儿重写**,全部走 ``scoping``(与对话链同一份)——
#    方案 §3.1 标红的那条:同一套判据已经有三份实现,别再制造第四份。
# ---------------------------------------------------------------------------

_PROJECT_ID_KEY: Final[str] = "project_id"
"""清单端点那个**三态**查询参数的键名。三态里有两态靠"这个键在不在"分辨,所以它得有个名字。"""


def _optional_project_id(params: dict[str, Any]) -> str | None:
    """三态取值,返回值**直接喂给 ``db.list_rows(project_id=…)``,语义 1:1**::

        参数不出现             → None  不筛工地(全部工地)
        ?project_id=(有键无值)→ ""    只看未归属(D6:未归属是空串,不是 NULL)
        ?project_id=P-xxx      → "…"  只看这个工地

    🔴 **不许写成 ``params.get(key, "")``,也不许在后面接 ``or ""``。** 那样「参数不出现」
    会塌成空串,于是「全部工地」悄悄变成「只有未归属」—— 界面上少了一大半隐患,
    **而没有任何报错**。``db.list_rows`` 的 docstring 有原话:None 是不筛项目,
    "" 是只看未归属,两者语义完全不同,别混用。

    首尾空白照 ``_text`` 的规矩去掉:``?project_id=%20`` 是个打错的参数,当成「未归属」
    比拿一个带空格的工地号去查(必然一条都查不到、还看不出为什么)更接近人的本意。
    """
    raw = params.get(_PROJECT_ID_KEY)
    return raw.strip() if isinstance(raw, str) else None


def _row_payload(row: hazards.HazardRow, *, today_iso: str) -> dict[str, Any]:
    """清单/详情里的一行 = ``scoping.hazard_item()`` 那九个键,**再加三个**。

    加的三个都是"表格要、对话不要"的东西,这个差别是刻意的:
      · ``severity`` —— **未定级的隐患对人念的是它,不是 ``grade``**(那时 grade 是映射表
        给的默认档「一般」,不是有人判过的结论;照着念就是「这条是一般隐患」,
        而硬拦③ ``_require_graded`` 拦的正是这句话);
      · ``project_id`` —— 操作台会跨工地看(``?project_id`` 不出现 = 全部工地),
        不给这一格就分不出哪条属于谁。
      · 🔴 ``photo_id`` —— **发现这条隐患的那张现场照片**(2026-08-17 补)。
        补之前操作台上一条隐患只有文字:监理要在**看不到照片**的情况下判
        一般/严重,还要判是不是「识错了」而按下否决 —— 而「否决」这个判断
        **完全依赖看照片**(帽子到底戴没戴)。定级又是签发文书的前置。
        真人当时的反馈只有三个字:「没有照片」。
        ⚠️ 它**不是**复查照片:那张在 ``hazard_docs.photo_id``,一次复查一张;
        这张是首次发现那张,一条隐患只有一张。两者混起来就是拿发现时的照片
        当"整改后"的证据,而那条红线的全部意义就是事后追责时分得清。

    为什么 ``scoping.hazard_item()`` 自己不给这两个键:它那份**每一行都要进模型上下文**,
    少给少错(它的 docstring 里点名剔掉了 ``project_id``);这边是一张表格,多两列不花钱。
    ⚠️ 反过来也成立:**别把这两个键加回 ``hazard_item`` 去** —— 那会让对话链的每一行凭空变长,
    而且那个函数的键集合被 ``test_supervision_scoping.py`` 整个钉死了。
    """
    return {
        **scoping.hazard_item(row, today_iso=today_iso),
        "severity": row.severity,
        "project_id": row.project_id,
        "photo_id": row.photo_id,
    }


def _work_list_hazards(params: dict[str, Any]) -> _Result:
    """GET /supervision/hazards —— 隐患清单。一次取全后在内存里筛(禁在循环里逐条查)。"""
    scope = _text(params, "scope") or scoping.SCOPE_ACTIVE
    if scope not in scoping.SCOPES:
        # 🔴 明确拒绝,**不许静默回落到某一档**:``scoping.in_scope`` 认不出的词会落在
        #    「在办」而且一声不吭(它的 docstring 点名要求调用方自己拦野词),
        #    而少给的清单在界面上看不出来少了。措辞与 ``tools.list_hazards`` 那句同款 ——
        #    同一件事在面板上和在对话里得是同一句话。
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            f"查隐患只能按这几种来:{'、'.join(scoping.SCOPES)}。你说的「{scope}」我这儿没有。",
        )

    project_id = _optional_project_id(params)
    rows = hazards.list_rows(project_id=project_id)

    # 未归属一共几条(D6)。**它不过筛子** —— 回答的是「有没有一批隐患没人看得见」,
    # 拿筛子筛过反而会把它藏起来(口径与 ``tools.list_hazards`` 一致)。
    # 三态各有各的算法,但答的是同一个数:
    #   None    → rows 就是全部,**在内存里数,不再打第二次库**:同一份快照数出来的数
    #             不会和上面那次查询之间被人插一条,而且省一次 IO;
    #   ""      → rows 本身就是未归属那一堆;
    #   具体工地 → 它们不在 rows 里,只能另查一次(**只数条数**,不把明细混进本工地清单)。
    if project_id is None:
        unassigned = sum(1 for r in rows if not r.project_id)
    elif not project_id:
        unassigned = len(rows)
    else:
        unassigned = len(hazards.list_rows(project_id=""))

    today_iso = scoping.today_hk().isoformat()
    matched = [r for r in rows if scoping.in_scope(r, scope=scope, today_iso=today_iso)]
    # **与对话链共用同一个旋钮**(``supervision_list_max_rows``,当前 50)。
    # 嫌小是改环境变量的事,不是在这儿加第二个常量 —— 加了之后面板与对话会在
    # 不同的条数上截断,而两边都显示"就这么多"。
    limit = get_settings().supervision_list_max_rows
    shown = [_row_payload(r, today_iso=today_iso) for r in matched[:limit]]
    truncated = len(matched) > limit

    # 三个计数都在**本次筛出来的全部行**上算(不是截断后那批):监理要的是「一共还有几条」,
    # 截断只影响列出来几行。口径照抄 ``tools.list_hazards``,两边的数才对得上。
    pending_count = sum(1 for r in matched if r.status == hazards.STATUS_PENDING)
    overdue_count = sum(1 for r in matched if scoping.is_overdue(r, today_iso))

    data: dict[str, Any] = {
        "scope": scope,
        "project_id": project_id,
        # 「今天」原样给出去:超期是按它算的,前端要能解释「为什么这条标了超期」。
        "today": today_iso,
        "hazards": shown,
        "total": len(matched),
        "pending": pending_count,
        "overdue": overdue_count,
        "unassigned": unassigned,
        "truncated": truncated,
    }
    return _Result(
        200,
        ok(
            data,
            _list_user_msg(
                scope,
                project_id=project_id,
                total=len(matched),
                pending=pending_count,
                overdue=overdue_count,
                unassigned=unassigned,
                truncated=truncated,
                limit=limit,
            ),
        ),
    )


def _list_user_msg(
    scope: str,
    *,
    project_id: str | None,
    total: int,
    pending: int,
    overdue: int,
    unassigned: int,
    truncated: bool,
    limit: int,
) -> str:
    """清单的一句概览:几条、其中待确认/超期几条、未归属几条、截断没有。

    **刻意不照抄 ``tools._list_summary``。** 那一份是念给模型听的,带着逐行清单 ——
    对话里没有表格,不逐行念工友就看不见;而操作台自己会把 ``data.hazards`` 渲成表格,
    这里再念一遍就是同一份数据在屏幕上出现两次。

    两个计数**只在非零时**出现(这一点与 tools 那份同一个取舍):恒定带上「超期 0 条」
    会让人把它读成一句套话,真有超期那天反而看不见。
    """
    if not total:
        head = f"{scope}的隐患一条都没有。"
    else:
        extras = []
        if scope != scoping.SCOPE_PENDING and pending:
            extras.append(f"待确认 {pending} 条")
        if scope != scoping.SCOPE_OVERDUE and overdue:
            extras.append(f"超期 {overdue} 条")
        middle = f"(其中{'、'.join(extras)})" if extras else ""
        head = f"{scope}的隐患 {total} 条{middle}。"
        if truncated:
            head += f"条数太多,这次只给了前 {limit} 条。"
    if not unassigned:
        return head
    if project_id:
        # 看的是某个具体工地:未归属那批**不在**这份清单里,不专门说一句就永远没人看见(D6)。
        return head + f"另外还有 {unassigned} 条隐患没归到任何工地,不在本工地清单里,别漏了。"
    # project_id 是 None(全部工地)或空串(看的就是未归属那一堆):那批**在**本次取数
    # 范围内,说「另外还有」就成了假话,只点明它们的存在与来历。
    return head + f"未归属(界面上没选工地时拍的)隐患共 {unassigned} 条。"


def _hazard_doc_payload(doc: hazards.HazardDocRow) -> dict[str, Any]:
    """证据链里的一项:一份**文书**,或一条**复查记录**。

    两种形状共用这一个函数,差别只在哪几格有值 —— 拆成两个函数各拼各的,前端就得先猜
    自己拿到的是哪一种,而漏判的表现是少渲一块、不报错。
    """
    return {
        "doc_type": doc.doc_type,
        "doc_type_display": _DOC_TYPE_ZH[doc.doc_type],  # 键必存在:导入期已校验覆盖 DOC_TYPES
        "doc_no": doc.doc_no,
        "artifact_id": doc.artifact_id,
        # 文件名与签发那条路**同源**(见 ``_filename``)。判据是**有没有 artifact_id**,
        # 不是看 doc_type:复查行没有文件;而一份 doc_type=notice 却没落上盘的行
        # (§6.4 认下的那种事故)也应当照实说"没有文件",不给一个点开就 404 的名字。
        "filename": _filename(doc.doc_type, doc.doc_no) if doc.artifact_id else None,
        # 🔴 这是 ``hazard_docs.photo_id`` —— **这一次复查**拍的那张(一次复查一张),
        #    **不是** ``hazards.photo_id``(首次发现那张,一条隐患只有一张)。
        #    两者混起来 = 拿发现时的照片当"整改后"的证据,而「复查必须挂照片」这条红线
        #    (方案 §5.2)的全部意义就是事后追责时分得清这两张。文书行没有,是 None。
        "photo_id": doc.photo_id,
        "result": doc.result,
        # 中文名走 ``scoping.RESULT_ZH``(全仓唯一那张表),不许在这儿写
        # ``"合格" if … else "不合格"`` —— 那正是 S1 刚收敛掉的拷贝。
        # **没结论就是 None,不给「—」**:那个占位符是留档文书表格里"这格本来就空"的
        # 写法(空单元格在纸上读起来像漏填),而 JSON 里 null 才是诚实的"没有值",
        # 空格渲成什么由前端决定。
        "result_display": scoping.result_zh(doc.result) if doc.result else None,
        "created_at": doc.created_at,
    }


def _work_get_hazard(params: dict[str, Any]) -> _Result:
    """GET /supervision/hazards/{hazard_no} —— 单条详情 + 证据链。

    证据链一次取回(``docs_of`` 收的是编号列表),不在循环里逐条查 —— 这条与
    「上报主管部门时要一次举证」是同一个 SQL,别在这层退化成 N+1。
    """
    hazard_no = _text(params, "hazard_no")
    if not hazard_no:  # pragma: no cover —— 路由的路径段不可能是空的,纯防御
        raise _refuse(400, ErrorCode.INVALID_INPUT, "没说是哪条隐患(缺隐患编号)。")
    row = hazards.fetch(hazard_no)
    if row is None:
        # 措辞与 ``tools.get_hazard`` 那句**一字不差**,与 ``_require_hazard`` 那句
        # 刻意不同:查询这条路面板与对话是同一个动作(「看看这条隐患」),两处说法不一样
        # 会让人以为查的是两个台账;写入那条路是另一回事(那句在动作的语境里说)。
        raise _refuse(404, ErrorCode.NOT_FOUND, f"台账里没有「{hazard_no}」这条隐患,核对一下编号。")

    docs = hazards.docs_of([row.hazard_no])
    documents = [_hazard_doc_payload(d) for d in docs]
    reinspections = [d for d in documents if d["doc_type"] == _REINSPECT_DOC_TYPE]
    issued = [d for d in documents if d["doc_type"] != _REINSPECT_DOC_TYPE]
    data: dict[str, Any] = {
        **_row_payload(row, today_iso=scoping.today_hk().isoformat()),
        "found_at": row.found_at,
        "closed_at": row.closed_at,
        # 复查过没有 = 证据链里有没有 reinspect 行。**别拿 status 推**:复查不合格之后
        # 还能再复查,而 closed 也可能是复工令签出来的 —— 状态答不了这个问题。
        "reinspected": bool(reinspections),
        "documents": documents,
    }
    return _Result(200, ok(data, _detail_user_msg(row, issued=issued, reinspections=reinspections)))


def _detail_user_msg(
    row: hazards.HazardRow,
    *,
    issued: list[dict[str, Any]],
    reinspections: list[dict[str, Any]],
) -> str:
    """详情的一句人话:现在什么状态、签了几份文书、复查过没有。

    **「这条复查了吗」要能被一眼答上** —— 那是监理翻这一页最常问的一句,
    只把它埋进 ``documents`` 里让人自己数,等于没答。
    """
    parts = [f"隐患「{row.item}」现在是「{_zh(row.status)}」"]
    parts.append(f",已签 {len(issued)} 份文书" if issued else ",还没签过任何文书")
    if not reinspections:
        parts.append(",还没复查过。")
    else:
        last = reinspections[-1]  # docs_of 按 id 升序,最后一条就是最近一次
        # 没结论就如实说没结论:``hazard_docs.result`` 的 CHECK 是 ``IS NULL OR IN (…)``,
        # 历史行 / 补录行真的可能没有结论,而念成「不合格」= 在没有结论的情况下对外
        # 声称施工方复查没过(tools 那边修过一模一样的一处)。
        verdict = last["result_display"] or "没记结论"
        parts.append(f",复查过 {len(reinspections)} 次,最近一次{verdict}。")
    return "".join(parts)


_DISMISS_REASON_MIN_LEN: Final[int] = 4
"""理由的最短长度。四个字("认错了""重复了")是底线,挡的是「.」「1」这种敷衍。

不设更高:真的就是「白帽认成没戴」这么简单的事,逼人写作文只会让人复制粘贴同一句。
"""

_DISMISS_REASON_MAX_LEN: Final[int] = 200


def _work_dismiss(body: dict[str, Any]) -> _Result:
    """POST /supervision/dismiss —— 「这条不是隐患 / 已当场整改」,**不出文书**把它关掉。

    ===========================================================================
    为什么要有这条端点
    ---------------------------------------------------------------------------
    在它之前 ``open`` 只有两条出口,而两条都要签发法律文书。于是一条识别错了的
    隐患(白色安全帽被认成没戴、拍到的是隔壁工地)在系统里**关不掉** ——
    唯一的出路是为一个不存在的隐患真的签一份《监理通知单》,再拍张照登记
    「复查合格」。**纠错的代价是往证据链里塞一份假文书。**

    而「确认」这一下是单向门(``delete_pending`` 只删 pending),确认之后连否决
    都没了。这条端点把那扇门变回可回退的。

    ===========================================================================
    它与「否决」(``_work_reject``)的分工 —— 别搞混
    ---------------------------------------------------------------------------
        否决   pending  → 整行**删掉**,什么都不留(还没人确认过,没有留档价值)
        关掉   open     → 行还在、编号还在、照片还在,只是标成 closed 并**写明理由**

    也就是说:确认之前用否决,确认之后用这条。两条都不出文书,但只有这条留痕。

    ⚠️ **理由必填**,而且会原样进台账。这是它与「删掉」的本质区别 ——
    事后能回答「这几条为什么关的」。空理由在这里拦下(db 层不管这个:
    那层只保证存取保真与状态机合法性)。
    """
    hazard_no = _text(body, "hazard_no")
    row = _require_hazard(hazard_no)
    reason = _text(body, "reason")[:_DISMISS_REASON_MAX_LEN]

    # ⚠️ **状态闸排在理由闸前面**,顺序是刻意的:
    #    「这条路你根本走不通」比「你的理由太短」更根本。反过来的话,一个
    #    pending 的隐患会先被要求补写理由,人认真写完再提交,才被告知
    #    「这条不该走这儿」—— 白费一趟,而且他会以为是理由的问题。
    # 先读只为说人话;真正说了算的是下面那个 rowcount(同 _work_reject 的规矩)。
    if row.status != hazards.STATUS_OPEN:
        extra = (
            "这条还没人确认过,要拿掉请用「否决」。"
            if row.status == hazards.STATUS_PENDING
            else "这条路只给还没签过任何文书的隐患用 —— 纸已经发出去的,"
            "得走复查或上报那条路收尾,不然台账和现场对不上。"
        )
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{hazard_no}」现在是「{_zh(row.status)}」,不能这样关掉。{extra}",
        )

    if len(reason) < _DISMISS_REASON_MIN_LEN:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            "关掉这条隐患要写清为什么(比如「白色安全帽,现场核过」「已当场整改」)。"
            "这句话会留在台账里,是事后唯一能回答「这条为什么关的」的地方。",
        )

    closed = hazards.dismiss(hazard_no, reason=reason, issued_by=_issued_by(body))
    if not closed:
        raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)

    logger.info("隐患 %s 不出文书关掉,理由:%s", hazard_no, reason)
    return _issued_payload(
        hazard_no,
        hazards.STATUS_CLOSED,
        (),
        f"隐患「{hazard_no}」已关掉,没有出文书。理由留在台账里了:{reason}",
    )


def _work_reject(body: dict[str, Any]) -> _Result:
    """POST /supervision/reject —— 否决一条**待确认**的隐患(状态机图里 pending 那条否决支)。

    这是 ``db.delete_pending`` 唯一的调用入口(TODO-45 B 组:W9 落地时它零调用点,
    界面上的「否决」只是把那一行本地划掉,刷新就回来了)。

    🔴 **已经确认过的隐患一律拒。** ``delete_pending`` 的 ``WHERE status='pending'`` 是硬守卫;
    这里这道先读**只为说人话**(它能说清「这条现在是已签发通知单」)—— 真正说了算的是它的
    返回值,**拿到 False 一律 409,绝不信先读的那份快照**(与 ``_work_grade`` 同一条规矩:
    先读与写之间有并发窗口,那一格里另一个人可能刚把它确认掉)。
    """
    hazard_no = _text(body, "hazard_no")
    row = _require_hazard(hazard_no)  # 缺编号 400、查不到 404 都在这里拦下
    if row.status != hazards.STATUS_PENDING:
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{hazard_no}」现在是「{_zh(row.status)}」,不能否决 —— "
            "只有还没人确认过的隐患才能否掉。确认过的隐患挂着留档和证据链,"
            "不该因为一次误点就消失;确实不用再管了,走复查或上报那条路收尾。",
        )
    try:
        deleted = hazards.delete_pending(hazard_no)
    except sqlite3.IntegrityError as exc:
        # ``hazard_docs`` 有外键指着 ``hazards``。pending 的行理论上不该挂着任何文书
        # (所有签发都要求 open 起步),但真撞上了得收敛成人话:让它变成 500 的话,
        # 工友看到的是「系统开小差」,而这其实是一句明确的「这条已经有留档了,删不得」。
        raise _refuse(
            409,
            ErrorCode.CONFLICT,
            f"隐患「{hazard_no}」上面已经挂了留档材料,不能否决。刷新看看它现在到哪一步了。",
            detail=f"delete_pending({hazard_no!r}) 撞外键:{exc}",
        ) from exc
    if not deleted:
        raise _refuse(409, ErrorCode.CONFLICT, _MSG_RACED)

    logger.info("隐患 %s 被否决删除(工地 %s,违规项 %s)", hazard_no, row.project_id or "-", row.item)
    return _Result(
        200,
        ok(
            {
                "hazard_no": hazard_no,
                # 🔴 ``"deleted"`` **不在 db.STATUSES 八档里** —— 那一行已经从库里删掉了,
                #    没有状态可报(理由与守卫都在 ``_STATUS_DELETED``)。
                "status": _STATUS_DELETED,
                "documents": [],  # 否决不出文书,但形状保持一致,前端不用分叉
            },
            f"已否决隐患「{row.item}」({hazard_no}),它从台账里删掉了,不会再出现在清单上。",
        ),
    )


# ---------------------------------------------------------------------------
# 照片直传(``POST /supervision/photo``)—— 后端此前唯一的 PHOTO 产出口在聊天链
#
# 它既不是查询也不是动作:不认识任何一条隐患,只把字节存下来换一个编号。
# 协议整套照 ``checkin_api`` 那条走(raw body + 边收边数 + 魔数判类型),理由见模块头注。
# ---------------------------------------------------------------------------

_BYTES_PER_MB: Final[int] = 1024 * 1024
"""MB → 字节。与 ``checkin_api`` / ``core/uploads.py`` 同一换算,别另起一套。

刻意在本模块再写一份而不是从那两处 import:本模块与 ``checkin_api`` 都是被 langgraph
**按文件路径**加载的 HTTP 层,互相 import 不可靠(``core/access.py`` 的头注写明了这件事:
两者唯一的共同地面是 gyt 包)。为一个 1024×1024 去 core 里开一个新模块不划算。
"""

_PHOTO_BODY_KEY: Final[str] = "payload"
"""``_raw_body`` 把收到的字节塞进 params dict 的键名。

这么绕一道是为了让上传这条端点**共用 ``_serve``** —— 三个外壳的差别只有"参数从哪儿来"
这一件事(见 ``_handle`` / ``_handle_get`` / ``_handle_body`` 三兄弟),
而 ``_Work`` 的签名收的是 ``dict[str, Any]``。
"""

_PHOTO_NAME_STEM: Final[str] = "复查照片"
"""落盘产物的 ``original_name`` 词干,拼上魔数认出来的扩展名。

**给人话名字、不给内部路径**:这个名字会原样出现在回执的 ``filename`` 里,
也会进产物 sidecar,将来下载卡上显示的就是它。
"""

_JPEG_MAGIC: Final[bytes] = b"\xff\xd8\xff"
"""JPEG 文件头(SOI + 下一个 marker 的 0xFF)。与 ``checkin_api.JPEG_MAGIC`` 同一串。"""

_PNG_MAGIC: Final[bytes] = b"\x89PNG\r\n\x1a\n"
"""PNG 的 8 字节签名(含那两组 CRLF/EOF 探测字节,规范 §5.2 的完整签名,不是只认前四位)。"""

_WEBP_RIFF: Final[bytes] = b"RIFF"
_WEBP_TAG: Final[bytes] = b"WEBP"
_WEBP_TAG_AT: Final[int] = 8
"""WebP 是 RIFF 容器:前 4 字节 ``RIFF``、第 4-8 字节是文件长度(内容随文件变)、
第 8-12 字节才是 ``WEBP``。**必须两段都比** —— 只认 ``RIFF`` 的话,WAV / AVI 这些
同为 RIFF 容器的文件会被当成图片收下。"""

_IMAGE_MAGICS: Final[tuple[tuple[bytes, str], ...]] = (
    (_JPEG_MAGIC, ".jpg"),
    (_PNG_MAGIC, ".png"),
)
"""「前缀魔数 → 落盘扩展名」两条。WebP 不在这张表里,因为它要比两段(见 ``_WEBP_TAG_AT``)。

🔴 **扩展名必须落在 ``config.ALLOWED_IMAGE_EXT`` 里**,否则 ``artifacts._safe_ext``
会把它剥成空串、文件落盘时没有后缀 —— 而 ``_require_photo`` 只查 kind 与文件在不在,
照样放行,于是证据链里挂着一张**浏览器打不开**的"照片"。下面有导入期硬失败守着
(同 ``core/uploads.py`` 末尾那道,理由一字不差)。
"""

_WEBP_EXT: Final[str] = ".webp"

_MSG_PHOTO_OK: Final[str] = "照片收到了。"
"""上传成功那句。刻意短:这一步不是终点,工友接着要在界面上点「登记复查结论」。"""

_MSG_PHOTO_EMPTY: Final[str] = "没收到照片,重新拍一张再传一次。"
"""body 是空的。多半是前端拼请求时把文件漏了,但对工友只说"重拍一张"这件他能做的事。"""

_MSG_PHOTO_NOT_IMAGE: Final[str] = (
    "传上来的这个文件不是照片(只认 JPG、PNG、WebP 三种)。用手机拍一张,"
    "或者从相册里选一张照片再传一次。"
)
"""魔数认不出。**点名认哪三种** —— 只说"不是照片"的话,传 HEIC 的人会一直重传同一张。

⚠️ 不许把客户端报的 ``Content-Type`` 复述进这句话:它正是我们不信的那个东西,
念出来只会让人以为"我明明填的是 image/jpeg 啊"。那个值走 ``detail=`` 进日志。
"""


def _msg_photo_too_large(max_mb: float) -> str:
    """413 那句人话。带上具体兆数 —— 「太大」必须能回答「多大才行」
    (判据同 ``attendance/messages.photo_too_large``,那边是打卡链的文案,不跨用)。"""
    return f"照片太大了(超过 {max_mb:.0f}MB),压缩一下,或者直接用手机重拍一张再传。"


def _sniff_image_ext(payload: bytes) -> str | None:
    """按**魔数**认图片,认出来返回落盘扩展名,认不出返回 ``None``。

    🔴 **判据是文件头字节,不是 ``Content-Type``。** 后者是客户端自述的,任何人都能填
    ``image/jpeg`` 再传一份 docx 上来 —— 而这张照片接下来会被 ``_require_photo`` 当成
    **复查证据**,复查合格是**销项**的唯一通道(方案 §5.2)。只信自述 = 拿一个 .txt
    就能把隐患销掉,而留档文书上写着「隐患已消除」。
    「浏览器给的 MIME 不可靠」这件事本仓在 DXF 那条线上已经证过一次(前端只能按后缀认)。

    只认三种够用:JPG / PNG 是手机与截图的常态,WebP 是部分安卓相机与网页另存的默认。
    ⚠️ **HEIC 刻意不认**(iPhone 原生格式):后端没有解码它的依赖,收下来等于存一份
    大多数浏览器打不开的文件,而 ``_require_photo`` 只查"文件在不在"、照样放行 ——
    那正是"证据链里挂着一张点不开的照片"。真要支持,得先有解码/转码那一步。

    ⚠️ 这只是**文件头**校验,不是"这张图解得开"。截断的 JPEG 照样过。再往下(真解码)
    要拉 Pillow,而那是打卡链画水印时才付的成本 —— 这条端点只存字节,不渲染,
    不值得为它把解码搬进来。取舍写明白:能挡住"拿别的文件冒充照片",挡不住"坏图"。
    """
    for magic, ext in _IMAGE_MAGICS:
        if payload.startswith(magic):
            return ext
    if (
        payload.startswith(_WEBP_RIFF)
        and payload[_WEBP_TAG_AT : _WEBP_TAG_AT + len(_WEBP_TAG)] == _WEBP_TAG
    ):
        return _WEBP_EXT
    return None


_SNIFFED_EXTS: Final[tuple[str, ...]] = (*(e for _, e in _IMAGE_MAGICS), _WEBP_EXT)
"""``_sniff_image_ext`` 可能返回的全部扩展名 —— 下面那道导入期守卫拿它对白名单。"""

_UNKNOWN_EXTS: Final[tuple[str, ...]] = tuple(
    ext for ext in _SNIFFED_EXTS if ext not in ALLOWED_IMAGE_EXT
)
if _UNKNOWN_EXTS:  # pragma: no cover —— 只在有人动了白名单时触发
    raise RuntimeError(
        f"_sniff_image_ext 会返回 {_UNKNOWN_EXTS},而它们不在 config.ALLOWED_IMAGE_EXT 里 —— "
        "artifacts._safe_ext 会把扩展名剥成空串,复查照片落盘后没有后缀、浏览器打不开,"
        "而 _require_photo 只查 kind 和文件在不在,照样放行。做成导入时硬失败是刻意的:"
        "漏配的表现是证据链里挂着一张点不开的照片,不会有任何报错"
    )


def _work_upload_photo(params: dict[str, Any]) -> _Result:
    """把一张照片登记成 ``ArtifactKind.PHOTO`` 产物,回它的编号。**落盘是阻塞活,在线程池里跑。**

    两道闸的顺序是**先判类型、再落盘**,不许颠倒:反过来的话每一次"传错文件"都会先在盘上
    留一份垃圾,而回执说的是 400。测试里那条「非图片 → 400 **且没有产物落盘**」钉的就是这个。

    ⚠️ **不做去重、不做幂等。** 同一张照片传两次 = 两个 photo_id、盘上两份字节。
    理由:复查这件事本来就是"一次复查挂一张照片"(``hazard_docs.photo_id``),
    重复上传的成本是几百 KB,而给它上幂等要么多一个客户端要维护的 event_id
    (打卡那条链的账),要么按内容 hash 建索引 —— 两条都比这个问题本身贵。
    """
    payload = params.get(_PHOTO_BODY_KEY)
    # isinstance 这一半是纯防御:喂进来的只可能是 ``_raw_body`` 拼的那个 dict。
    # ``not payload`` 才是真正会发生的那种 —— 前端拼请求时把文件漏了,body 是空的。
    if not isinstance(payload, bytes) or not payload:
        raise _refuse(400, ErrorCode.INVALID_INPUT, _MSG_PHOTO_EMPTY)

    ext = _sniff_image_ext(payload)
    if ext is None:
        raise _refuse(
            400,
            ErrorCode.INVALID_INPUT,
            _MSG_PHOTO_NOT_IMAGE,
            # 只回显前 8 字节的十六进制:够认出"这其实是个 docx(PK\x03\x04)",
            # 又不会把一整个文件灌进日志。
            detail=f"魔数认不出是图片:前 8 字节 {payload[:8].hex()},共 {len(payload)} 字节",
        )

    filename = f"{_PHOTO_NAME_STEM}{ext}"
    photo_id = artifacts.register(payload, kind=ArtifactKind.PHOTO, original_name=filename)
    logger.info("监理复查照片已登记:%s(%s,%d 字节)", photo_id, ext, len(payload))
    # data 只有两个键,**没有 documents** —— 它不是动作端点、不出文书(模块头注那一节)。
    return _Result(200, ok({"photo_id": photo_id, "filename": filename}, _MSG_PHOTO_OK))


# ---------------------------------------------------------------------------
# handler 外壳 —— 十一个端点共用
# ---------------------------------------------------------------------------

_Work = Callable[[dict[str, Any]], _Result]
_Params = Callable[[Request], Awaitable[dict[str, Any]]]


def _guarded(work: _Work) -> _Work:
    """把同步活里两种"预期之内的失败"收敛成 ``_Result``。

    ``DocNoExhaustedError`` 单独接一档:它的头注写明**异常文本别原样透给用户**
    (「连着摇了 5 次…编号都已经被占用」不是工地上的人该读的东西),
    所以原文进 detail、只进日志,用户拿到 ``_MSG_DOC_NO_EXHAUSTED``。
    ⚠️ **绝不许降级成「用最后那个撞了的号继续」** —— 两份不同的文书顶着同一个编号,
    比这次没出成严重得多。
    """

    def _run(body: dict[str, Any]) -> _Result:
        try:
            return work(body)
        except _Refused as refused:
            return _Result(refused.status, refused.envelope)
        except DocNoExhaustedError as exc:
            logger.warning("文书编号摇不出来:%s", exc)
            return _Result(
                500,
                fail(ErrorCode.INTERNAL, _MSG_DOC_NO_EXHAUSTED, detail=str(exc)),
            )

    return _run


async def _get_params(request: Request) -> dict[str, Any]:
    """GET 的入参:查询串 + 路径段,合成一个 dict,喂给与 POST 同一套 ``_Work`` 签名。

    🔴 用整个查询串摊平、**不是逐个 ``.get()`` 出来再拼**:``?project_id=``(有键无值)
    与「压根没这个参数」必须是两种不同的东西(D6 的三态,推演在 ``_optional_project_id``)。
    dict 里"键在不在"天然分得开,而 ``.get()`` 把前者给成空串、后者给成 None,
    中间只要有人接一个 ``or ""`` 两态就合并了。

    路径段放在后面覆盖查询串:``/hazards/{hazard_no}?hazard_no=别的`` 这种请求里,
    真正指定隐患的是**路径**,查询串里那个是噪音(或者干脆是想试探点什么)。
    """
    return {**request.query_params, **request.path_params}


async def _serve(request: Request, work: _Work, params: _Params) -> JSONResponse:
    """十一个端点真正共用的那条链:令牌自查 → 取参数 → 阻塞活挪进线程池 → 兜底 500。

    ⚠️ 阻塞活(sqlite、docx 渲染、落盘)一步都不许留在事件循环里:这些路由和图跑在
    同一个循环上,langgraph 的 blockbuster 会抛 BlockingError(schedule/cad 都踩过)。
    **两条 GET 一样要走线程池** —— 它们照样读 sqlite,"只是查一下"不是豁免理由;
    **上传那条也一样** —— ``artifacts.register`` 是两次真写盘。

    🔴 **令牌自查排在取参数之前,不许调换。** 上传那条端点的"取参数"= 把最多
    ``photo_max_mb`` 的 body 读进内存;放到鉴权之前就等于让没钥匙的人也能把
    这台机器的内存喂满,而回执照样是 401。
    """
    try:
        denied = _deny_if_token_bad(request)
        if denied is not None:
            return denied
        collected = await params(request)
        result = await run_in_threadpool(_guarded(work), collected)
        return _respond(result.envelope, result.status)
    except _Refused as refused:
        # 只有取参数那一步会在线程池之外抛它:``_json_body`` 的请求体解析、
        # 以及 ``_raw_body`` 的流式大小闸(413),两处都在 async 侧。
        return _respond(refused.envelope, refused.status)
    except Exception:
        # 兜底:任何没料到的炸都收敛成 500 信封。堆栈只进日志;user_msg 走
        # DEFAULT_USER_MSG[INTERNAL](已是人话),不在这里另造第二句。
        logger.exception("监理请求处理失败:%s", request.url.path)
        return _respond(fail(ErrorCode.INTERNAL), 500)


async def _handle(request: Request, work: _Work) -> JSONResponse:
    """八个 POST 端点的外壳 —— 参数来自 JSON 请求体。"""
    return await _serve(request, work, _json_body)


async def _handle_get(request: Request, work: _Work) -> JSONResponse:
    """两条 GET 端点的外壳 —— 参数来自查询串与路径段,**没有请求体**。

    🔴 **不许给 GET 硬塞一个空 body 去走 ``_handle``。** GET 本来就可以不带 body,
    ``await request.json()`` 会当场抛,于是它要么被 ``_json_body`` 翻译成
    「这次提交的内容后台没读懂,刷新一下页面再试一次」(工友照着刷新,而根本没有
    任何东西读不懂),要么得在那个只该管解析请求体的函数里加一条「GET 就跳过」的分支。
    三个外壳共用 ``_serve``,差的只有"参数从哪儿来"这一件事,别让它们再分叉出第二处。
    """
    return await _serve(request, work, _get_params)


async def _raw_body(request: Request) -> dict[str, Any]:
    """收**原始字节**请求体,边收边数,超上限**当场**断 —— 不等收完。

    这正是 raw body 协议的意义(模块头注,判据整套抄自 ``checkin_api._read_photo``):
    multipart 要解析完整请求才知道多大,而 ``Request.stream()`` 是真流式
    (逐 ASGI 事件 yield,不预缓冲),读到哪算到哪,超了就不再往内存里攒。

    上限取 ``settings.photo_max_mb``(**与打卡同一个旋钮**)。⚠️ **每次请求现取** ——
    读成模块常量的话测试换环境变量测不到,生产改上限要重启才生效。

    ⚠️ 这里只管"多大",**不管"是不是图片"** —— 那一道在 ``_work_upload_photo`` 里,
    与落盘挨着(先判类型再落盘,顺序不许颠倒)。同理,本函数也不判空:
    空 body 是业务上的"没收到照片",人话归那边说。
    """
    max_mb = get_settings().photo_max_mb
    limit = int(max_mb * _BYTES_PER_MB)
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise _refuse(
                413,
                ErrorCode.FILE_TOO_LARGE,
                _msg_photo_too_large(max_mb),
                detail=f"请求体已收 {received} 字节,超过上限 {limit}",
            )
        chunks.append(chunk)
    return {_PHOTO_BODY_KEY: b"".join(chunks)}


async def _handle_body(request: Request, work: _Work) -> JSONResponse:
    """上传那条端点的外壳 —— 请求体是**原始字节**,不是 JSON。

    🔴 **套不上 ``_handle``**:那条外壳一进来就 ``await request.json()``,
    而图片字节喂给 JSON 解析器必然抛,于是一张完全正常的照片会被
    ``_json_body`` 翻译成「这次提交的内容后台没读懂,刷新一下页面再试一次」——
    工友照着刷新一百次也没用,而日志里只有一行"请求体不是合法 JSON"。
    分法与 ``_handle_get`` 一模一样:三个外壳共用 ``_serve``,
    差别只有"参数从哪儿来"这一件事(``_json_body`` / ``_get_params`` / ``_raw_body``)。
    """
    return await _serve(request, work, _raw_body)


# 十一个 handler。首行都不用反引号:starlette 生成 /docs 时把 docstring 喂给 yaml,
# ` 开头必炸(无害,但每次启动打两条 traceback,查日志的人会被带偏 —— 同 checkin_api)。


async def get_hazards(request: Request) -> JSONResponse:
    """GET /supervision/hazards —— 隐患清单(scope 四选一;project_id 三态,见模块头注)。"""
    return await _handle_get(request, _work_list_hazards)


async def get_hazard_detail(request: Request) -> JSONResponse:
    """GET /supervision/hazards/{hazard_no} —— 单条详情 + 证据链(文书下载信息、复查照片)。"""
    return await _handle_get(request, _work_get_hazard)


async def post_confirm(request: Request) -> JSONResponse:
    """POST /supervision/confirm —— 监理确认(pending → open),支持批量。"""
    return await _handle(request, _work_confirm)


async def post_dismiss(request: Request) -> JSONResponse:
    """POST /supervision/dismiss —— 不出文书关掉一条 open 的隐患(必须写理由)。"""
    return await _handle(request, _work_dismiss)


async def post_reject(request: Request) -> JSONResponse:
    """POST /supervision/reject —— 否决一条待确认的隐患(只删 pending,确认过的拒)。"""
    return await _handle(request, _work_reject)


async def post_grade(request: Request) -> JSONResponse:
    """POST /supervision/grade —— 人工定级,清掉 needs_grading。"""
    return await _handle(request, _work_grade)


async def post_notice(request: Request) -> JSONResponse:
    """POST /supervision/notice —— 签发《监理通知单》(严重隐患拒,走 suspend)。"""
    return await _handle(request, _work_notice)


async def post_suspend(request: Request) -> JSONResponse:
    """POST /supervision/suspend —— 三份文书原子产出(一般隐患拒)。"""
    return await _handle(request, _work_suspend)


async def post_reinspect_result(request: Request) -> JSONResponse:
    """POST /supervision/reinspect-result —— 复查结论,照片必填。"""
    return await _handle(request, _work_reinspect_result)


async def post_resume(request: Request) -> JSONResponse:
    """POST /supervision/resume —— 签发《工程复工令》(仅 resuming)。"""
    return await _handle(request, _work_resume)


async def post_escalate(request: Request) -> JSONResponse:
    """POST /supervision/escalate —— 签发《监理报告》报主管部门(仅 reinspect_failed)。"""
    return await _handle(request, _work_escalate)


async def post_photo(request: Request) -> JSONResponse:
    """POST /supervision/photo —— 直传一张复查照片,换一个 photo_id(契约见模块头注)。

    请求体是**原始图片字节**(不是 multipart、不是 JSON),所以走 ``_handle_body``
    这个外壳而不是 ``_handle``;类型按魔数判、不信 Content-Type。
    """
    return await _handle_body(request, _work_upload_photo)


# ---------------------------------------------------------------------------
# 路由表 —— 真正挂上去的入口是 backend/webapp.py,不是下面那个 app
# ---------------------------------------------------------------------------

SUPERVISION_ROUTES: Final[list[Route]] = [
    # 查询两条(W10)。两条路径长相不同(``/hazards`` 与 ``/hazards/{…}``),
    # starlette 不会拿一条去遮另一条,这里的先后只为好读:先清单、后详情。
    Route("/supervision/hazards", get_hazards, methods=["GET"]),
    Route("/supervision/hazards/{hazard_no}", get_hazard_detail, methods=["GET"]),
    # 写入九条(W9 七条 + W10 的 reject + 2026-08-21 的 dismiss)。
    Route("/supervision/confirm", post_confirm, methods=["POST"]),
    Route("/supervision/reject", post_reject, methods=["POST"]),
    Route("/supervision/dismiss", post_dismiss, methods=["POST"]),
    Route("/supervision/grade", post_grade, methods=["POST"]),
    Route("/supervision/notice", post_notice, methods=["POST"]),
    Route("/supervision/suspend", post_suspend, methods=["POST"]),
    Route("/supervision/reinspect-result", post_reinspect_result, methods=["POST"]),
    Route("/supervision/resume", post_resume, methods=["POST"]),
    Route("/supervision/escalate", post_escalate, methods=["POST"]),
    # 上传一条。单列一组是因为它**既不是查询也不是动作**:不认识任何一条隐患,
    # 只把字节存下来换一个编号(所以回执里没有 hazard_no / status / documents)。
    # 路径是静态的,与上面 ``/hazards/{hazard_no}`` 那条带路径段的不会互相遮挡。
    Route("/supervision/photo", post_photo, methods=["POST"]),
]
"""监理这十一条路由(查询 2 + 写入 8 + 上传 1)。**必须被 ``backend/webapp.py`` 铺进它的 routes**。

``langgraph.json`` 的 ``http.app`` 只能有一个(现在指 webapp.py),所以本项目所有
自定义路由都在那里汇合:项目/图纸/资料管理是它自己的,打卡链是 ``CHECKIN_ROUTES``,
监理这一摊是这一份。

⚠️ webapp.py 那侧删掉这一铺,监理端点就整个消失 —— 而现象是 **404,不是启动报错**
(langgraph 不知道有谁本该在)。改这里的路径同样要去 webapp.py 与 ``Caddyfile``
的 ``/api/supervision*`` 那条 route 一起确认。

W10 加的三条**不用动 Caddyfile**(2026-08-16 实测 ``caddy adapt``):那条 route 的匹配器
是 ``@supervision path /api/supervision /api/supervision/*``,``path`` 只看路径、
**不限制方法**,新加的 GET 与新路径都落在它下面;而且它在展开后的 route 表里仍排在
``handle_path /api/*`` **之前**(handle 系列按书写顺序择一,排后面 = 永不生效且无报错)。

🔴 **``POST /supervision/photo`` 是"哪天有端点要收大 body"的那一天,Caddyfile 必须动。**
那条 route 上挂着 ``request_body { max_size 1MB }``(当初按"只收一个小 JSON"定的),
而这条端点收的是最多 ``photo_max_mb``(10MB)的照片 —— **不改的结果是公网上传照片
一律失败**,而且报错来自 Caddy 的 413(不是信封,更不是那句写好的中文),
查的人会去翻本模块的大小闸,方向全错。本机与 ``make dev`` 不过 Caddy,**照样全绿**。

要加的是**一条更靠前的专用 route**(不是把 ``@supervision`` 整块放宽到 12MB ——
那样其余十条也跟着敞开,而它们本来就只该收一个小 JSON)::

    @supervision_photo path /api/supervision/photo
    handle @supervision_photo {
        request_body {
            max_size 12MB
        }
        uri strip_prefix /api
        reverse_proxy backend:2024
    }

12MB 这个数与 ``/api/checkin*`` 那条同一笔账:``GYT_PHOTO_MAX_MB=10``,走 **raw body**
不过 base64(体积不膨胀),留 2MB 给 header 与误差。**改照片上限要回来重算这一行。**
⚠️ 它**必须排在 ``@supervision`` 之前**(而 ``@supervision`` 又排在 ``handle_path /api/*``
之前):``route{}`` 里 handle 系列按书写顺序择一匹配,写到后面 = 永远不生效,
且没有任何报错 —— ``/api/supervision/photo`` 会先被 ``@supervision`` 吃掉,
照样卡在 1MB,而 ``caddy validate`` 说 Valid。
"""

app = Starlette(routes=list(SUPERVISION_ROUTES))
"""只给本模块的单元测试用(``TestClient(app)``),**线上不走它**。

留着的理由与 ``checkin_api.app`` 一样:测试要能脱开 webapp.py 单独验鉴权与这十个动作,
而 webapp.py 在 backend/ 根、不属于 gyt 包,把它拖进单测会连带整个项目管理栈。
"""


__all__ = [
    "SUPERVISION_ROUTES",
    "app",
    "get_hazard_detail",
    "get_hazards",
    "post_confirm",
    "post_dismiss",
    "post_escalate",
    "post_grade",
    "post_notice",
    "post_photo",
    "post_reinspect_result",
    "post_reject",
    "post_resume",
    "post_suspend",
]
