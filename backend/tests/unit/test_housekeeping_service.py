"""``docker-compose.vps.yml`` 里 housekeeping 服务的守门断言。

这个服务是**生产上唯一的定时活儿**:每天备份台账与对话历史,然后清理到期的
考勤凭证图。它没有任何单元可测的代码 —— 逻辑全在 compose 里那段 shell。
所以这里直接读那份 YAML,把几条「改错了不会有任何东西说话」的约束钉住。

===============================================================================
为什么这几条值得单独钉
-------------------------------------------------------------------------------
① ``--apply``:不带它清理器只演练、**一个字节都不删**,而日志里会照常打印一份
   漂亮的「将要删 N 张」。也就是说服务看着在干活、其实什么都没干,
   而登录页那份 PDPO(第486章)声明写着「憑證照片保存 90 天後自動刪除」——
   那是一句对使用者的法律承诺。这四个字符掉了,承诺就重新变成空的,
   **而且和 2026-08-21 之前那个「压根没人调它」的状态在日志上分不出来。**

② 顺序(先备份、再删图):反过来的话,删完还没来得及备就崩,那批图连同
   台账行一起没了。这是两个动作之间唯一的耦合,而 YAML 里它只是两行的先后。

③ ``./data`` 不许挂成 ``:ro``:WAL 下连读都要能写 -shm,而且清理器**真的会删**
   data/artifacts 下的文件。挂成只读的表现是备份直接打不开库。

④ 服务名 / 容器名跟着改名走:2026-08-21 这个服务从 ``backup`` 改名
   ``housekeeping``,而改名的代价是旧容器变孤儿(compose 不带
   ``--remove-orphans`` 不会动它),两个容器一起备份、互相删对方的快照。
   这条断言不能防住那件事,但至少让「文件里到底叫什么」有一处可查。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

# parents[0]=unit  parents[1]=tests  parents[2]=backend  parents[3]=<仓库根>
# 刻意不从 config.py 借那句 parents[3](同 test_config.py 的理由):两边各算各的,
# 只有真的指向同一个目录才算对。
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_COMPOSE: Final[Path] = _REPO_ROOT / "docker-compose.vps.yml"


@pytest.fixture(scope="module")
def compose_text() -> str:
    """整份 YAML 的原文。

    刻意**不**用 yaml 解析:被测的是 command 里那段 shell 的字面内容,
    解析一遍再拼回来只会多一层可能出错的中间物。
    """
    assert _COMPOSE.is_file(), f"找不到 {_COMPOSE} —— 仓库根算错了?"
    return _COMPOSE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def housekeeping_block(compose_text: str) -> str:
    """从 ``housekeeping:`` 到下一个同级服务之间的那一段。"""
    start = compose_text.find("\n  housekeeping:\n")
    assert start != -1, "compose 里没有 housekeeping 服务 —— 改名了?那这个文件要跟着改。"
    rest = compose_text[start + 1 :]
    # 下一个顶层服务(两个空格缩进 + 名字 + 冒号 + 换行)
    end = len(rest)
    for line_start in range(1, len(rest)):
        if (
            rest[line_start - 1] == "\n"
            and rest[line_start:].startswith("  ")
            and not rest[line_start + 2 : line_start + 3].isspace()
        ):
            head = rest[line_start:].split("\n", 1)[0]
            if head.endswith(":") and not head.strip().startswith("-") and head[2:3] != " ":
                end = line_start
                break
    return rest[:end]


def test_考勤清理带着apply跑_不带就是每天演练一遍() -> None:
    """🔴 全文件最要紧的一条,理由见模块头注 ①。"""
    text = _COMPOSE.read_text(encoding="utf-8")
    assert "gyt.attendance.cleanup --apply" in text, (
        "housekeeping 里的考勤清理没带 --apply —— 它会每天演练一遍、一个字节都不删,"
        "而登录页的 PDPO 声明写着「90 天後自動刪除」。日志上和「压根没调」分不出来。"
    )
    # 反向:不许出现不带 --apply 的调用(比如有人加了第二处忘了带)
    for line in text.splitlines():
        if "gyt.attendance.cleanup" in line and "#" not in line.split("gyt.attendance")[0]:
            assert "--apply" in line, f"这一行调了清理器却没带 --apply:{line.strip()}"


def test_先备份再删图_顺序不许反(housekeeping_block: str) -> None:
    """理由见模块头注 ②:删完还没来得及备就崩 = 那批图连同台账行一起没了。"""
    first_backup = housekeeping_block.find("python -m scripts.backup_data")
    first_cleanup = housekeeping_block.find("python -m gyt.attendance.cleanup")

    assert first_backup != -1, "housekeeping 里没有备份那一步"
    assert first_cleanup != -1, "housekeeping 里没有考勤清理那一步"
    assert first_backup < first_cleanup, (
        "顺序反了:必须先备份再删图。反过来的话,删完还没来得及备就崩,"
        "那批凭证图连同台账里对应的行一起没了。"
    )


def test_data挂载可写_不许加只读(housekeeping_block: str) -> None:
    """理由见模块头注 ③。两条都会静默坏:WAL 打不开库 / 清理器删不掉文件。"""
    assert "- ./data:/app/data\n" in housekeeping_block, (
        "housekeeping 的 ./data 挂载不见了或被改成了只读。"
        "WAL 下连读都要能写 -shm,而且清理器真的要删 data/artifacts 下的文件。"
    )
    assert "./data:/app/data:ro" not in housekeeping_block


def test_容器名与服务名对得上(housekeeping_block: str) -> None:
    """理由见模块头注 ④。"""
    assert "container_name: gyt-housekeeping" in housekeeping_block


def test_没有发布端口(housekeeping_block: str) -> None:
    """和 backend / frontend / artifacts 同一条红线。

    ``preflight_vps.sh`` 第 ⑤ 组数「发布端口总数」应当恒为 3(80 / 443tcp / 443udp),
    这条是在它之前的一道更早的闸 —— 加错了在 CI 就红,不用等到上机器。
    """
    assert "ports:" not in housekeeping_block


def test_旧服务名已经清干净了(compose_text: str) -> None:
    """改名之后不许还剩一个 ``backup:`` 服务 —— 两个都在的话会各备各的、互相删快照。"""
    assert "\n  backup:\n" not in compose_text
    assert "container_name: gyt-backup\n" not in compose_text
