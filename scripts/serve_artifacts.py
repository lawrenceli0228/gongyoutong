#!/usr/bin/env python3
"""产物只读静态服务:既能按路径取件,也能**只凭 32 位编号**取件。

    python3 scripts/serve_artifacts.py [--port 8788] [--directory <产物目录>]

替代原来 Makefile 里那句 ``python3 -m http.server``。多出来的那件事只有一个端点:

    GET /<日期>/<文件名>      ← 老路径,原样保留(巡检记录卡片用的就是它)
    GET /by-id/<32位编号>     ← 新增,前端手里只有编号时用这条

为什么非要加 /by-id/:
    上传的照片**不在消息历史里**。core/uploads.py 的 ingest_uploads 是 supervisor 的
    pre_model_hook,它返回 ``{"messages": [RemoveMessage(id=last.id), rewritten]}``——
    把带图的那条 HumanMessage 从 state 里**永久删掉**,换成一句纯文本
    「看看这张照片。(照片编号:<32位hex>、<32位hex>)」。必须永久换而不是只改模型输入,
    因为子 Agent 与 supervisor 共享同一份 messages,留着 image 块会让文本档模型再炸一次 400。
    于是界面上要把工友刚发的那张照片显示回来,唯一的线索就是那串编号 —— 而图在盘上是

        data/artifacts/<UTC日期 YYYYMMDD>/<32位小写hex>.<扩展名>

    带**日期段**的。日期段走的是 ``datetime.now(UTC)``(core/artifacts.py 的 register),
    前端既拿不到它、也不能按本地当天日期去猜:晚上演示时 UTC 已经是"明天",猜必错。
    所以这一跳只能由服务端做:拿编号在 ``<根>/*/`` 下 glob 一次。

只绑 127.0.0.1,而且**不给 --host 参数**(见 BIND_HOST 那条注释)。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Final

# --- 模块级常量:禁止在函数里散落魔法值 ---------------------------------------

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

# 默认产物目录按**本文件位置**推导仓库根,不按进程工作目录 —— 与
# backend/src/gyt/config.py 的 _default_data_dir() 同一套推导法。
# 那边踩过的坑照样适用于这里:写成相对路径 Path("data/artifacts") 的话,
# 从仓库根跑和从 backend/ 下跑会指到两个不同的目录,而现象只是"卡片点开一片空白",
# 不报错、不抛异常,最难查的那一类。
DEFAULT_DIR: Final[Path] = REPO_ROOT / "data" / "artifacts"

# 端口号有三处同源:这里、Makefile 的 ARTIFACTS_PORT、tool-calls.tsx 的 ARTIFACT_BASE。
# 要改一起改 —— 只改一处的现象是"卡片点开一片空白",不报错,属于最难发现的那一类。
DEFAULT_PORT: Final[int] = 8788

# 绑定地址**故意不做成命令行参数**,也故意不做成环境变量。
# 这里面是工地现场照片和巡检记录(含可识别人脸,见 TODO-22),在宿主机上绑 0.0.0.0
# 等于把它们发给整个局域网 —— 与 docker-compose.yml 的 `127.0.0.1:2024:2024` 是同一条红线。
# 给个旋钮迟早有人为了"方便用手机看"拧到 0.0.0.0,而且不会有任何东西拦他。
#
# 但容器里必须绑 0.0.0.0,否则同一个 compose 网络里的 caddy 根本连不上它
# (两个容器不共享 network namespace,127.0.0.1 只是它自己)。
# 所以这里按**是不是在容器里**自动判,而不是给人一个参数:
#
#   宿主机  → 127.0.0.1,没有任何办法改。要在别的机器上看,走 ssh 端口转发。
#   容器内  → 0.0.0.0,但它只在该容器的网络命名空间内。
#             想让它真的对外,必须有人给这个服务写 `ports:` ——
#             而那件事是**能被检查的**:docker-compose.vps.yml 里 artifacts 一个 ports 都没有,
#             `scripts/preflight_vps.sh` 第 ⑤ 组会数「发布端口总数」,多一条就露馅。
#
# 换句话说:保证没有变弱,只是从"写死在这个文件里"挪到了"编排里可被自检的不变量"上。
_IN_CONTAINER: Final[bool] = Path("/.dockerenv").exists()
BIND_HOST: Final[str] = "0.0.0.0" if _IN_CONTAINER else "127.0.0.1"  # noqa: S104 —— 见上面整段

BY_ID_PREFIX: Final[str] = "/by-id/"

# 编号必须是纯 32 位小写 hex —— 与 core/artifacts.py 的 ARTIFACT_ID_RE 同一条判据
# (那边的 id 由 uuid4().hex 生成,天然满足)。
#
# 为什么光这一条就够堵死路径穿越:这个字符集里**没有** "/"、"\"、"."、"\0",
# 所以拼不出 ".."、拼不出绝对路径、跨不出目录;也没有 "*"、"?"、"["，
# 所以把它直接插进 glob 模式(下面 _find_blob 就是这么干的)不会变成通配符。
# 再加上长度固定 32,连"前缀撞另一个编号"都做不到。校验放在 unquote **之后**,
# 于是 %2e%2e%2f 这种编码过的穿越串在解码后照样撞在这条正则上。
#
# 用 fullmatch 而不是 match(r"^...$"):Python 的 "$" 允许尾随一个 \n,
# `re.match(r"^[0-9a-f]{32}$", "a"*32 + "\n")` 是**能匹配**的(已实测),
# 而 "\n" 完全可以由 URL 里的 %0a 解码出来。fullmatch 要求整串被消费,不吃这一套。
ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")

# 按编号取件时**绝不**吐出去的后缀。
#
# 为什么用"拒绝清单"而不是"白名单":产物目录不是谁都能写的,唯一的写入口是
# core/artifacts.register(),它在落盘那一刻就过了一遍扩展名白名单(_ALLOWED_EXT =
# 图片/文档/CAD)。写入侧已经是硬闸,读出侧再抄一份白名单只会带来漂移 ——
# 哪天有人往 config.ALLOWED_IMAGE_EXT 加个 .gif,这边忘了跟,卡片就静默 404。
# 写入侧白名单**唯独管不住**的正好是下面这两样,所以只拦这两样:
#   .json —— sidecar 元数据。它被故意排除在 _ALLOWED_EXT 之外(正文永远不可能重名
#            覆盖它),于是它就大喇喇躺在正文旁边。里面有 original_name(工友手机里的
#            原始文件名)、sha256、size,前端一样都用不着,泄出去纯亏。
#   .tmp  —— register 的 _write_atomic 先写 <名字>.tmp 再 rename。请求正好撞进这个
#            窗口的话,吐出去的是**写了一半**的文件:不报错,只是图裂开。
DENY_SUFFIXES: Final[frozenset] = frozenset({".json", ".tmp"})

# Content-Type 表。系统 mimetypes 库靠不住:同一份代码在本机
# python3.8 / python3.11 上 ".md" 都返回 None(已实测),真吐出去浏览器会当二进制下载。
# 覆盖 config.py 三张白名单(ALLOWED_IMAGE_EXT / ALLOWED_DOC_EXT / ALLOWED_CAD_EXT)的全部取值。
# ⚠️ 这张表是那三张白名单的第二份拷贝:往那边加扩展名,记得回来加一行,
# 否则文件照样吐得出去,只是类型退化成 application/octet-stream(浏览器会下载而不是内嵌显示)。
MIME_BY_EXT: Final[dict] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".dxf": "image/vnd.dxf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}
DEFAULT_MIME: Final[str] = "application/octet-stream"

# 面向人的中文提示。这几条可能被人直接在浏览器地址栏里打开看到,不许出现堆栈、类名、内部路径。
TEXT_BAD_ID: Final[str] = (
    "编号不对。\n"
    "按编号取件的地址长这样:/by-id/后面跟 32 位编号(只能是 0-9 和小写 a-f)。\n"
    "编号就是聊天里那句「(照片编号:…)」括号里的那一串。\n"
)
TEXT_NOT_FOUND: Final[str] = (
    "没找到这份文件。\n"
    "编号格式没问题,但盘上翻遍了也没有对应的文件。常见原因:\n"
    "  1. 这个编号是别的机器上传的,本机没有这份文件;\n"
    "  2. 数据目录被清空过(比如重跑验收前删过台账);\n"
    "  3. 服务起在了另一个数据目录上 —— 看启动时打印的那行「根目录」对不对。\n"
)
TEXT_NO_LISTING: Final[str] = (
    "这里不列目录。\n"
    "本服务只按两种地址取文件:\n"
    "  /<日期>/<文件名>      例如 /20260807/xxxx.jpg\n"
    "  /by-id/<32位编号>     只有编号时走这条\n"
    "关掉目录列表是有意的:列一次就等于把工地现场的照片全都点名一遍。\n"
    "想看盘上有什么,在本机直接 ls 数据目录。\n"
)
TEXT_METHOD_NOT_ALLOWED: Final[str] = "这个服务只读,只接 GET 和 HEAD。\n"


def _is_body_file(path: Path, artifact_id: str) -> bool:
    """判断这条 glob 命中是不是「该编号的正文文件」。

    落盘名只有两种长相(见 core/artifacts.register):``<编号><扩展名>``,
    以及扩展名被白名单挡掉时的``<编号>``(无扩展名 —— 现在盘上没有这种,但代码路径在)。
    所以除了前缀,还得卡住"前缀之后要么什么都没有、要么是一个点"。
    """
    if not path.is_file():
        return False
    if path.name == artifact_id:  # 无扩展名的正文
        return True
    if not path.name.startswith(artifact_id + "."):
        return False
    return path.suffix.lower() not in DENY_SUFFIXES


def _find_blob(root: Path, artifact_id: str) -> Path | None:
    """在 ``<root>/*/`` 里按编号找正文文件,找不到返回 None。

    调用方**必须**先用 ID_RE 校验过 artifact_id —— 这里直接把它拼进 glob 模式,
    安全性完全依赖那条正则(理由见 ID_RE 的注释)。

    命中多个时取排序后的第一个,也就是**日期目录最早**的那一份。
    为什么是"最早"而不是"最新":core/artifacts.py 的 _sidecar_path 用的就是
    ``sorted(...)`` 取首个。两边必须挑同一份,否则同一个编号后端 resolve() 读到 A、
    界面显示 B —— 报告里写的和图上看到的对不上,而且没有任何报错。
    正常情况下永远只有一份(编号是 uuid4().hex,register 也只写一个正文),
    真出现两份说明有人手工往产物目录里恢复过旧备份,所以往 stderr 吼一声。
    """
    hits = root.glob("*/" + artifact_id + "*")
    matches = sorted(p for p in hits if _is_body_file(p, artifact_id))
    if not matches:
        return None
    if len(matches) > 1:
        names = "、".join(str(p.relative_to(root)) for p in matches)
        warning = f"[警告] 编号 {artifact_id} 命中了多份文件,按最早的日期目录取:{names}"
        print(warning, file=sys.stderr)
    return matches[0]


class ArtifactsHandler(SimpleHTTPRequestHandler):
    """只读产物服务。在 SimpleHTTPRequestHandler 之上做三件事:

    1. 拦下 /by-id/<编号>,自己找文件、自己吐;
    2. 关掉目录列表;
    3. GET/HEAD 之外一律 405。

    继承而不是重写,是为了「保住现有行为」这条硬要求:``/<日期>/<文件名>`` 这条老路径
    (前端覆盖件 tool-calls.tsx 的巡检记录卡片就指着它)走的还是父类那套
    send_head/translate_path,一个字节都没动。
    """

    # 父类 guess_type() 会先查 extensions_map、查不到才回落到系统 mimetypes,
    # 所以把表塞进来,老路径和 /by-id/ 就吃同一份 Content-Type,不会一个对一个错。
    # 用 dict(父类的, **本表) 造**新字典**而不是原地 update —— 别改到父类的类属性上去。
    extensions_map = dict(SimpleHTTPRequestHandler.extensions_map, **MIME_BY_EXT)

    server_version = "GytArtifacts/1.0"

    # --- 请求分发 -------------------------------------------------------

    # do_GET / do_HEAD 的大写名字是 BaseHTTPRequestHandler 定的分发约定
    # (它按 "do_" + 请求动词 反射),不是本仓的命名风格跑偏,别改成 do_get。
    def do_GET(self) -> None:
        if self._try_by_id(write_body=True):
            return
        if self._deny_by_suffix(write_body=True):
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self._try_by_id(write_body=False):
            return
        if self._deny_by_suffix(write_body=False):
            return
        super().do_HEAD()

    def _deny_by_suffix(self, write_body: bool) -> bool:
        """按扩展名拦掉 sidecar / 半成品;拦下了返回 True。

        ⚠️ 这一段是补的。DENY_SUFFIXES 原来只在 ``_is_body_file`` 里生效,
        而那个函数**只被 /by-id/ 那条路调用** —— 直接按路径请求
        ``/<日期>/<编号>.json`` 会一路落到父类 SimpleHTTPRequestHandler,
        它根本不认这张表,于是 sidecar 元数据照发不误。
        2026-08-11 公网上线做产物路由自检时实测到:``/by-id/`` 那条 404,
        同一份文件换成直接路径回 200。**两条路进同一个目录,却只有一条设了闸。**

        回 404 不回 403:403 等于告诉对方"这儿确实有个东西,只是不给你",
        而这个服务对外只该承认正文的存在。/by-id/ 那条路找不到正文时也是 404,
        两条路的对外语义保持一致。
        """
        path = urllib.parse.urlsplit(self.path).path
        # 先 unquote 再取扩展名 —— 否则 ``%2ejson`` 能绕过去。
        suffix = PurePosixPath(urllib.parse.unquote(path)).suffix.lower()
        if suffix not in DENY_SUFFIXES:
            return False
        self._send_text(HTTPStatus.NOT_FOUND, TEXT_NOT_FOUND, write_body)
        return True

    def __getattr__(self, name: str):
        """GET/HEAD 之外的动词一律 405。

        BaseHTTPRequestHandler 靠 ``hasattr(self, "do_" + 请求动词)`` 分发,查不到给 501。
        这里用 __getattr__ 兜住所有没实现的 do_*,好处是不用维护一张
        POST/PUT/DELETE/PATCH/OPTIONS 的清单 —— 清单总会漏,而漏掉的动词会退回 501,
        语义是"服务器不会这个方法",跟"这个资源只读"不是一回事。

        前缀卡得很死(只认 do_):__getattr__ 是兜底钩子,放宽了会把属性名打错这类
        真 bug 变成"返回一个方法",查起来能查一整天。
        """
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _reject_method(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self._finish_text(TEXT_METHOD_NOT_ALLOWED, write_body=True)

    # --- /by-id/ ---------------------------------------------------------

    def _try_by_id(self, write_body: bool) -> bool:
        """认领并处理 /by-id/ 请求;不是这条路径就返回 False 交回父类。"""
        # 先切掉 ?查询串 和 #锚点,再 unquote。顺序不能反:
        # 校验必须发生在解码**之后**,不然 %2e%2e%2f 会在校验通过之后才变成 ../。
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith(BY_ID_PREFIX):
            return False

        artifact_id = urllib.parse.unquote(path[len(BY_ID_PREFIX) :])
        if not ID_RE.fullmatch(artifact_id):
            # 不回显用户传进来的原串:这行会被浏览器直接渲染,回显等于给自己开一个反射面。
            self._send_text(HTTPStatus.BAD_REQUEST, TEXT_BAD_ID, write_body)
            return True

        blob = _find_blob(Path(self.directory), artifact_id)
        if blob is None:
            self._send_text(HTTPStatus.NOT_FOUND, TEXT_NOT_FOUND, write_body)
            return True

        self._send_blob(blob, write_body)
        return True

    def _send_blob(self, blob: Path, write_body: bool) -> None:
        """把正文文件吐出去。打开失败一律按 404 处理,不把系统错误漏给前端。"""
        try:
            handle = blob.open("rb")
        except OSError:
            self._send_text(HTTPStatus.NOT_FOUND, TEXT_NOT_FOUND, write_body)
            return
        with handle:
            stat = os.fstat(handle.fileno())
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", MIME_BY_EXT.get(blob.suffix.lower(), DEFAULT_MIME))
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Last-Modified", self.date_time_string(int(stat.st_mtime)))
            self.end_headers()
            if write_body:
                shutil.copyfileobj(handle, self.wfile)

    # --- 响应工具 ---------------------------------------------------------

    def _send_text(self, status: HTTPStatus, text: str, write_body: bool) -> None:
        self.send_response(status)
        self._finish_text(text, write_body)

    def _finish_text(self, text: str, write_body: bool) -> None:
        """补完一条已经 send_response 过的纯文本响应(HEAD 只发头不发身)。"""
        body = text.encode("utf-8")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if write_body:
            self.wfile.write(body)

    def end_headers(self) -> None:
        # nosniff:白名单里没有 html,但兜底类型是 application/octet-stream,
        # 加这一行免得浏览器自己"猜"成 HTML 再执行里面的东西。
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def list_directory(self, path):  # noqa: ANN001, ANN201 (签名跟父类)
        """关掉目录列表。

        取舍:``python3 -m http.server`` 是**有**列表的,这里去掉了。
        代价是排查时不能在浏览器里翻文件了(改成在本机 ls,提示里也这么写了);
        收益是打开 http://127.0.0.1:8788/ 不再等于把每一张工地照片点名一遍 ——
        前端两条取件路径(卡片按路径、照片按编号)都不需要列表,留着纯是白送的暴露面。
        返回 None 是父类 send_head 约定的"我已经把响应发完了"。
        """
        self._send_text(HTTPStatus.NOT_FOUND, TEXT_NO_LISTING, write_body=self.command != "HEAD")
        return None


def _parse_args(argv: list | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="工友通产物只读静态服务(只绑 127.0.0.1)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIR, help="产物根目录")
    # 没有 --host / --bind:见 BIND_HOST 的注释。
    return parser.parse_args(argv)


def main(argv: list | None = None) -> int:
    args = _parse_args(argv)
    root = args.directory.expanduser().resolve()

    if root.exists() and not root.is_dir():
        print(f"[错误] 产物根目录被一个同名文件占着:{root}", file=sys.stderr)
        return 1
    # 目录不存在也照起 —— 后端第一次登记产物时才会创建它,
    # 而演示流程里静态服务常常先起。原 Makefile 配方里那句 mkdir -p 就是干这个的。
    root.mkdir(parents=True, exist_ok=True)

    def build(*handler_args, **handler_kwargs):
        return ArtifactsHandler(*handler_args, directory=str(root), **handler_kwargs)

    # 先真正绑上端口,**再**打印横幅。反过来写的话,端口被占时屏幕上会先出现
    # 一句「[静态服务] http://127.0.0.1:8788/」再跟一句报错 —— 而人只会记住第一句,
    # 然后拿着那个地址去点,点出一片空白,再回头怀疑是前端坏了。
    try:
        server = ThreadingHTTPServer((BIND_HOST, args.port), build)
    except OSError as exc:
        # 最常见的就是端口被占(上一次没关干净,或者另一个人也起了一份)。
        print(f"[错误] {args.port} 端口起不来:{exc}", file=sys.stderr)
        print("       换个端口:--port 8799;或者先把占着这个端口的进程关掉。", file=sys.stderr)
        return 1

    base = f"http://{BIND_HOST}:{args.port}"
    # flush=True 不能省。原来 Makefile 里这几行是 shell 的 @echo,永远立刻出现;
    # 换成 python 的 print 之后,只要输出不是终端(重定向进日志、被 tee 接走、
    # 跑在进程管理器里),stdout 就变成块缓冲 —— 横幅要攒够 8KB 才吐,
    # 而这个进程会一直跑下去,于是"根目录"那行**永远看不到**。
    # 而那行恰好是排查"卡片点开一片空白"时第一个要看的东西(服务是不是起错目录了)。
    print(f"[静态服务] {base}/", flush=True)
    print(f"           根目录:{root}", flush=True)
    print(f"           按编号取件:{base}/by-id/<32位编号>", flush=True)
    # 这行不能两种模式共用一句话。在容器里说"本机之外访问不到"是**假的**
    # (同一个 compose 网络里的 caddy 就访问得到,那正是它存在的意义),
    # 而运维照着这句话去排查"caddy 连不上 artifacts",方向会全错。
    if _IN_CONTAINER:
        print(f"           容器内绑 {BIND_HOST} —— 只有同一个 compose 网络里的服务能连,", flush=True)
        print("           对外仍然只有 caddy 那一个入口(本服务不许有 ports)。", flush=True)
    else:
        print(f"           只绑 {BIND_HOST},本机之外访问不到。Ctrl-C 停止。", flush=True)

    # 多线程:一张巡检记录 docx 下到一半,不该把同页那十几张照片全堵住。
    # daemon_threads 让 Ctrl-C 能立刻退,不用等所有连接自然结束。
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[静态服务] 已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
