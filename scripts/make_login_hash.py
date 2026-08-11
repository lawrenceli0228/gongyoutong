#!/usr/bin/env python3
"""生成登录口令的 scrypt 哈希,填进 .env 的 GYT_LOGIN_HASH。

用法(在仓库根):

    python scripts/make_login_hash.py                # 交互式输入,不回显
    python scripts/make_login_hash.py --random       # 直接生成一个高熵口令并打出来

⚠️ **别用 shell 历史会记住的方式传口令**(比如 `--password xxx`)——
所以这个脚本**故意不提供**那种参数:口令只能交互输入,或者让它自己生成。

⚠️ 和 GYT_BASIC_AUTH_HASH 不一样,scrypt 哈希里**没有 `$` 需要加倍的问题**吗?
    有。哈希串里就是用 `$` 分段的,而 docker compose 读 .env 做插值时会吃掉一层。
    所以这个脚本打出来的**已经是加倍好的**(`$$`),直接粘进 .env 即可 ——
    别自己再改。bcrypt 那边就是靠人手动加倍,而写错的表现不是报错,
    是「口令永远对不上」,查起来极慢。
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import secrets
import sys

# 与 scripts/serve_login.py 的 SCRYPT_* 同源 —— 改一处必须改两处。
# 不一致的表现:哈希算得出来,但服务端算出的 dk 不一样 → 口令永远不对。
N = 1 << 14
R = 8
P = 1
DKLEN = 32

# 去掉了容易看错的 0/O/o/1/l/I —— 这个口令要发给工地上的人手打。
ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_hash(plaintext: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(plaintext.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=DKLEN)
    return f"scrypt${N}${R}${P}${salt.hex()}${dk.hex()}"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 GYT_LOGIN_HASH")
    ap.add_argument("--random", action="store_true", help="自动生成一个 20 位高熵口令")
    args = ap.parse_args(argv)

    if args.random:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(20))
        print(f"口令(记下来,发给测试的人):{pw}\n")
    else:
        pw = getpass.getpass("输入口令(不回显):")
        again = getpass.getpass("再输一次:")
        if pw != again:
            print("[错误] 两次不一致。", file=sys.stderr)
            return 1
        if len(pw) < 12:
            # 这不是洁癖:登录接口是公开的,而 scrypt 单次只要 ~50ms。
            # 短口令在这个成本下是能被在线爆破的。
            print("[错误] 太短了,至少 12 位。", file=sys.stderr)
            return 1

    raw = make_hash(pw)
    # compose 插值会吃掉一层 $,所以打出来就先加倍好,人直接粘
    print("粘进 .env(已经替你把 $ 加倍成 $$,别再改):\n")
    print(f"GYT_LOGIN_HASH={raw.replace('$', '$$')}\n")
    print("自检:重启 login 服务后用新口令登一次。哈希写坏的表现不是报错,是「口令永远不对」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
