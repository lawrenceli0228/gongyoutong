/**
 * sha256Hex 走 crypto.subtle。Node 18+ 的 globalThis.crypto 自带 WebCrypto,
 * 这里的兜底只服务某些精简发行版 —— **兜在测试 setup,不给库代码开洞**:
 * 库若自己 import node:crypto,「浏览器不安全上下文没有 subtle 必须抛」
 * 那条分支就永远测不出来了(库会悄悄从 Node 拿到 subtle)。
 */
import { webcrypto } from "node:crypto";

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", {
    value: webcrypto,
    // 留给测试自己 stub:「crypto 缺失」那条分支要把它整个换掉
    configurable: true,
    writable: true,
  });
}
