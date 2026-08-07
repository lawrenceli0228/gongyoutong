#!/usr/bin/env python3
"""生成「安全评测集标注台」单页 HTML(图片内联,离线可用)。

    python3 scripts/make_annotator.py [输出路径]

为什么要这个东西:27 张照片逐个开图片、再对着 CSV 数一行行改,是最容易出错的干法 ——
标错一行不会有任何报错,只会让评测分数偏低,而看报告的人会以为是模型不行。
标注台把「图」和「那一行的标注」摆在一起,并且把受控词做成只能点选的 chip,
从源头上消灭「手打错别字」和「看串行」两类错误。

图片必须内联成 data URI:Artifact 的 CSP 会拦掉一切外链资源。
为了体积,这里把长边压到 760px、JPEG 质量 62 —— 判断「有没有戴安全帽」够用了,
30 张合计约 2.8MB,base64 后约 3.8MB。
"""

from __future__ import annotations

import base64
import csv
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
CSV_PATH: Final[Path] = REPO_ROOT / "backend" / "eval" / "datasets" / "safety.draft.csv"
PHOTOS_DIR: Final[Path] = REPO_ROOT / "data" / "demo" / "photos"
DEFAULT_OUT: Final[Path] = REPO_ROOT / "docs" / "标注台.html"

THUMB_EDGE: Final[int] = 760
THUMB_QUALITY: Final[int] = 62

VOCAB: Final[tuple[str, ...]] = (
    "未戴安全帽",
    "未穿反光衣",
    "高空作业未系安全带",
    "临边无防护",
    "消防通道堵塞",
    "材料堆放混乱",
    "用电隐患",
    "动火作业无监护",
)
LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("violation", "有违规"),
    ("compliant", "合规"),
    ("not_site", "非工地"),
)
DONE_IDS: Final[frozenset[str]] = frozenset({"S28", "S29", "S30"})
"""这三张干扰项是选图时就逐张看过并定稿的,不需要再确认 —— 标出来免得白看。"""


def _thumb_data_uri(path: Path, workdir: Path) -> str:
    """压一张缩略图并转成 data URI。用 sips(macOS 自带),避免引入 Pillow 依赖。"""
    out = workdir / path.name
    subprocess.run(
        ["sips", "-Z", str(THUMB_EDGE), "-s", "formatOptions", str(THUMB_QUALITY),
         str(path), "--out", str(out)],
        check=True, capture_output=True,
    )
    return "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode("ascii")


def _split_note(note: str) -> tuple[str, str]:
    """把草稿 note 拆成「源标注线索」与「人写的理由」。

    草稿格式是 `待人工确认 | 源标注: no-helmet×1 | 原名: 00035`。
    前缀对标注者是噪音,源标注却是有用的对照线索,所以拆开分别显示。
    """
    parts = [p.strip() for p in note.split("|")]
    hint = " · ".join(p for p in parts if p.startswith(("源标注", "原名")))
    rest = " ".join(p for p in parts if p and not p.startswith(("待人工确认", "源标注", "原名")))
    return hint, rest


def build_rows() -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        rows: list[dict[str, Any]] = []
        with io.open(CSV_PATH, encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                photo = PHOTOS_DIR / raw["image"]
                if not photo.is_file():
                    raise SystemExit(f"照片不存在:{photo}")
                hint, reason = _split_note(raw.get("note", ""))
                rows.append({
                    "id": raw["id"],
                    "type": raw["type"],
                    "image": raw["image"],
                    "label": raw["label"],
                    "violations": [v for v in raw["violations"].split(";") if v],
                    "hint": hint,
                    "reason": reason,
                    "done": raw["id"] in DONE_IDS,
                    "src": _thumb_data_uri(photo, workdir),
                })
        return rows


HTML: Final[str] = """<title>安全评测集 · 标注台</title>
<style>
:root{
  --ink:#16150F; --paper:#F4F2EA; --hazard:#E2571F; --helmet:#EFBB24;
  --concrete:#8B8578; --ok:#4C7A57; --elsewhere:#52738F;
  --bg:var(--paper); --fg:var(--ink);
  --surface:#FFFFFF; --line:#DCD8CC; --muted:#6E6A5F; --shadow:rgba(22,21,15,.07);
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Noto Sans SC",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#121109; --fg:#EDE9DD; --surface:#1D1B12; --line:#33301F;
  --muted:#9A9482; --concrete:#7A7466; --shadow:rgba(0,0,0,.4);
  --hazard:#FF6F35; --helmet:#F5C93C; --ok:#63946F; --elsewhere:#6B90AE;
}}
:root[data-theme="dark"]{
  --bg:#121109; --fg:#EDE9DD; --surface:#1D1B12; --line:#33301F;
  --muted:#9A9482; --concrete:#7A7466; --shadow:rgba(0,0,0,.4);
  --hazard:#FF6F35; --helmet:#F5C93C; --ok:#63946F; --elsewhere:#6B90AE;
}
:root[data-theme="light"]{
  --bg:#F4F2EA; --fg:#16150F; --surface:#FFFFFF; --line:#DCD8CC;
  --muted:#6E6A5F; --concrete:#8B8578; --shadow:rgba(22,21,15,.07);
  --hazard:#E2571F; --helmet:#EFBB24; --ok:#4C7A57; --elsewhere:#52738F;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);
     font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 140px}

header{padding:40px 0 24px;border-bottom:2px solid var(--fg)}
h1{margin:0;font-size:clamp(26px,3.4vw,38px);font-weight:800;letter-spacing:-.025em;
   text-wrap:balance}
.lede{margin:10px 0 0;color:var(--muted);max-width:62ch}
kbd{font-family:var(--mono);font-size:.82em;background:var(--surface);
    border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:1px 6px}

.bar{position:sticky;top:0;z-index:20;background:var(--bg);
     border-bottom:1px solid var(--line);padding:12px 0;margin-bottom:8px;
     display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.track{flex:1;min-width:180px;height:8px;background:var(--line);border-radius:99px;overflow:hidden}
.fill{height:100%;width:0;background:var(--hazard);transition:width .25s ease}
.count{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:14px;font-weight:700}
button{font:inherit;cursor:pointer;border-radius:6px;border:1px solid var(--line);
       background:var(--surface);color:var(--fg);padding:7px 14px;transition:.15s}
button:hover{border-color:var(--fg)}
button:focus-visible{outline:2px solid var(--hazard);outline-offset:2px}
.primary{background:var(--hazard);border-color:var(--hazard);color:#fff;font-weight:700}
.primary:hover{filter:brightness(1.08)}

.card{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(320px,.95fr);gap:26px;
      padding:26px 0;border-bottom:1px solid var(--line);scroll-margin-top:76px}
.card[data-state="confirmed"]{opacity:.62}
.card[data-state="confirmed"]:hover{opacity:1}
@media (max-width:860px){.card{grid-template-columns:1fr}}

figure{margin:0;position:relative}
img{width:100%;height:auto;display:block;border-radius:8px;border:1px solid var(--line);
    box-shadow:0 2px 14px var(--shadow);background:var(--surface)}
.tag{position:absolute;top:10px;left:10px;font-family:var(--mono);font-size:12px;
     font-weight:700;letter-spacing:.06em;padding:4px 9px;border-radius:5px;
     background:var(--fg);color:var(--bg)}
.bucket{position:absolute;top:10px;right:10px;font-size:12px;padding:4px 9px;border-radius:5px;
        background:var(--surface);border:1px solid var(--line);color:var(--muted)}

.meta{font-family:var(--mono);font-size:12.5px;color:var(--muted);
      background:var(--surface);border:1px solid var(--line);border-radius:6px;
      padding:9px 11px;margin:0 0 18px;overflow-x:auto;white-space:nowrap}
h3{margin:0 0 9px;font-size:11px;font-weight:700;letter-spacing:.11em;
   text-transform:uppercase;color:var(--muted)}
.seg{display:flex;gap:0;margin-bottom:20px;border:1px solid var(--line);border-radius:7px;
     overflow:hidden;width:fit-content}
.seg button{border:0;border-radius:0;padding:8px 18px;background:transparent;font-weight:600}
.seg button+button{border-left:1px solid var(--line)}
.seg button[aria-pressed="true"]{color:#fff}
.seg button[data-v="violation"][aria-pressed="true"]{background:var(--hazard)}
.seg button[data-v="compliant"][aria-pressed="true"]{background:var(--ok)}
.seg button[data-v="not_site"][aria-pressed="true"]{background:var(--elsewhere)}

.chips{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:8px}
.chip{font-size:13.5px;padding:7px 12px;border-radius:99px;position:relative;padding-left:26px}
.chip::before{content:attr(data-k);position:absolute;left:9px;top:50%;transform:translateY(-50%);
              font-family:var(--mono);font-size:10px;color:var(--muted)}
.chip[aria-pressed="true"]{background:var(--hazard);border-color:var(--hazard);color:#fff;font-weight:600}
.chip[aria-pressed="true"]::before{color:rgba(255,255,255,.7)}
.chips.off{opacity:.34;pointer-events:none}
.warn{color:var(--hazard);font-size:13px;font-weight:600;margin:0 0 14px;min-height:19px}
textarea{width:100%;font:inherit;font-size:13.5px;padding:9px 11px;border-radius:6px;
         border:1px solid var(--line);background:var(--surface);color:var(--fg);
         resize:vertical;min-height:56px;margin-bottom:14px}
textarea:focus-visible{outline:2px solid var(--hazard);outline-offset:1px}
.act{display:flex;gap:9px;align-items:center}
.confirmed{color:var(--ok);font-weight:700;font-size:13.5px}

.dock{position:fixed;left:0;right:0;bottom:0;z-index:30;background:var(--surface);
      border-top:2px solid var(--fg);padding:13px 20px;
      display:flex;gap:14px;align-items:center;justify-content:center;flex-wrap:wrap;
      box-shadow:0 -3px 18px var(--shadow)}
.hint{color:var(--muted);font-size:13px}
dialog{border:2px solid var(--fg);border-radius:10px;background:var(--surface);color:var(--fg);
       max-width:min(900px,92vw);padding:22px}
dialog::backdrop{background:rgba(0,0,0,.55)}
pre{font-family:var(--mono);font-size:12px;line-height:1.5;background:var(--bg);
    border:1px solid var(--line);border-radius:6px;padding:13px;overflow:auto;max-height:52vh}
code{font-family:var(--mono);font-size:.88em;background:var(--surface);
     border:1px solid var(--line);border-radius:4px;padding:1px 5px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="wrap">
<header>
  <h1>安全评测集 · 标注台</h1>
  <p class="lede">逐张核对模型要考的那份「标准答案」。左边是照片，右边改结论。
  违规项只能从受控词表里点选——<strong>手打的错别字判分侧不认</strong>，
  而报告只会写「模型没看出来」，矛头指向模型。
  快捷键：<kbd>J</kbd>/<kbd>K</kbd> 翻卡，<kbd>1</kbd>–<kbd>8</kbd> 切违规项，<kbd>Enter</kbd> 确认本张。</p>
</header>

<div class="bar">
  <span class="count" id="count">0 / 0</span>
  <div class="track"><div class="fill" id="fill"></div></div>
  <button id="jump">跳到下一张待确认</button>
  <button class="primary" id="export">导出 CSV</button>
</div>

<main id="list"></main>
</div>

<div class="dock">
  <span class="hint" id="dockhint">改动会自动存在这台电脑的浏览器里，关掉页面不会丢。</span>
  <button id="reset">重置为草稿</button>
  <button class="primary" id="export2">导出 CSV</button>
</div>

<dialog id="out">
  <h2 style="margin:0 0 6px;font-size:19px">导出 CSV</h2>
  <p style="margin:0 0 12px;color:var(--muted);font-size:14px">
    覆盖到 <code>backend/eval/datasets/safety.csv</code>，然后跑 <code>make eval SUITE=safety</code>。
  </p>
  <pre id="csv"></pre>
  <div class="act" style="margin-top:13px">
    <button class="primary" id="copy">复制</button>
    <button id="download">下载文件</button>
    <button id="close">关闭</button>
  </div>
</dialog>

<script>
const ROWS = __ROWS__, VOCAB = __VOCAB__, LABELS = __LABELS__;
const KEY = "gyt.annotator.v1";
const saved = JSON.parse(localStorage.getItem(KEY) || "{}");
const state = ROWS.map(r => ({
  ...r,
  ...(saved[r.id] || {}),
  confirmed: saved[r.id]?.confirmed ?? r.done,
}));
const $ = s => document.querySelector(s);
let active = 0;

function esc(s){ return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

function render(){
  $("#list").innerHTML = state.map((r,i) => `
    <article class="card" id="c${i}" data-state="${r.confirmed?"confirmed":"pending"}">
      <figure>
        <img src="${r.src}" alt="${esc(r.image)}" loading="lazy" width="760">
        <span class="tag">${r.id} · ${esc(r.image)}</span>
        <span class="bucket">${esc(r.type)}</span>
      </figure>
      <div>
        <p class="meta">${esc(r.hint) || "（无源标注线索）"}</p>
        <h3>这张是什么</h3>
        <div class="seg">${LABELS.map(([v,t])=>
          `<button data-i="${i}" data-v="${v}" aria-pressed="${r.label===v}">${t}</button>`).join("")}</div>
        <h3>违规项${r.label==="violation"?"（至少选一项）":"（非违规不可选）"}</h3>
        <div class="chips ${r.label==="violation"?"":"off"}">${VOCAB.map((w,k)=>
          `<button class="chip" data-k="${k+1}" data-i="${i}" data-w="${esc(w)}"
             aria-pressed="${r.violations.includes(w)}">${esc(w)}</button>`).join("")}</div>
        <p class="warn">${warnOf(r)}</p>
        <h3>标注理由（可空）</h3>
        <textarea data-i="${i}" placeholder="为什么这么判；看不清的地方也写这里">${esc(r.reason)}</textarea>
        <div class="act">
          <button class="primary" data-ok="${i}">${r.confirmed?"重新确认":"确认这张"}</button>
          ${r.confirmed?'<span class="confirmed">✓ 已确认</span>':""}
        </div>
      </div>
    </article>`).join("");
  const done = state.filter(r=>r.confirmed).length;
  $("#count").textContent = `${done} / ${state.length}`;
  $("#fill").style.width = (done/state.length*100) + "%";
}

function warnOf(r){
  if (r.label === "violation" && !r.violations.length) return "标成有违规却没选违规项 —— 判分无从判起，会当场炸。";
  if (r.label !== "violation" && r.violations.length) return "非违规行不能带违规项，自相矛盾。";
  return "";
}

function save(){
  const out = {};
  state.forEach(r => out[r.id] = {label:r.label, violations:r.violations, reason:r.reason, confirmed:r.confirmed});
  localStorage.setItem(KEY, JSON.stringify(out));
}

document.addEventListener("click", e => {
  const seg = e.target.closest(".seg button");
  if (seg){
    const r = state[+seg.dataset.i];
    r.label = seg.dataset.v;
    if (r.label !== "violation") r.violations = [];
    save(); render(); return;
  }
  const chip = e.target.closest(".chip");
  if (chip){
    const r = state[+chip.dataset.i], w = chip.dataset.w;
    r.violations = r.violations.includes(w) ? r.violations.filter(x=>x!==w) : [...r.violations, w];
    save(); render(); return;
  }
  const ok = e.target.closest("[data-ok]");
  if (ok){
    const i = +ok.dataset.ok, r = state[i];
    if (warnOf(r)) { alert(warnOf(r)); return; }
    r.confirmed = true; save(); render();
    const next = state.findIndex((x,j)=>j>i && !x.confirmed);
    if (next >= 0) { active = next; $("#c"+next).scrollIntoView({behavior:"smooth",block:"start"}); }
    return;
  }
});

document.addEventListener("input", e => {
  if (e.target.tagName === "TEXTAREA"){ state[+e.target.dataset.i].reason = e.target.value; save(); }
});

document.addEventListener("keydown", e => {
  if (e.target.tagName === "TEXTAREA" || e.metaKey || e.ctrlKey) return;
  if (e.key === "j" || e.key === "k"){
    active = Math.max(0, Math.min(state.length-1, active + (e.key==="j"?1:-1)));
    $("#c"+active).scrollIntoView({behavior:"smooth",block:"start"});
  } else if (e.key >= "1" && e.key <= "8"){
    const r = state[active];
    if (r.label !== "violation") return;
    const w = VOCAB[+e.key-1];
    r.violations = r.violations.includes(w) ? r.violations.filter(x=>x!==w) : [...r.violations, w];
    save(); render();
  } else if (e.key === "Enter"){
    const r = state[active];
    if (warnOf(r)) { alert(warnOf(r)); return; }
    r.confirmed = true; save(); render();
    const next = state.findIndex((x,j)=>j>active && !x.confirmed);
    if (next >= 0){ active = next; $("#c"+next).scrollIntoView({behavior:"smooth",block:"start"}); }
  }
});

$("#jump").onclick = () => {
  const i = state.findIndex(r=>!r.confirmed);
  if (i < 0) return alert("30 张全部确认完了。可以导出了。");
  active = i; $("#c"+i).scrollIntoView({behavior:"smooth",block:"start"});
};

function cell(v){ const s = String(v ?? ""); return /[",\\n]/.test(s) ? '"'+s.replace(/"/g,'""')+'"' : s; }
function toCSV(){
  const head = "id,type,image,label,violations,note";
  const body = state.map(r => [r.id, r.type, r.image, r.label,
    r.violations.join(";"), [r.reason, r.hint].filter(Boolean).join(" | ")].map(cell).join(","));
  return [head, ...body].join("\\n") + "\\n";
}
function openOut(){
  const bad = state.filter(r => warnOf(r));
  const un = state.filter(r => !r.confirmed);
  $("#csv").textContent = toCSV();
  $("#dockhint").textContent = bad.length
    ? `还有 ${bad.length} 行自相矛盾，导出前请先修：` + bad.map(r=>r.id).join("、")
    : un.length ? `还有 ${un.length} 张没确认，导出的是草稿值。` : "全部确认完毕。";
  $("#out").showModal();
}
$("#export").onclick = openOut; $("#export2").onclick = openOut;
$("#close").onclick = () => $("#out").close();
$("#copy").onclick = async () => {
  await navigator.clipboard.writeText(toCSV());
  $("#copy").textContent = "已复制";
  setTimeout(()=>$("#copy").textContent="复制", 1400);
};
$("#download").onclick = () => {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([toCSV()], {type:"text/csv;charset=utf-8"}));
  a.download = "safety.csv"; a.click(); URL.revokeObjectURL(a.href);
};
$("#reset").onclick = () => {
  if (!confirm("把所有改动丢掉，回到草稿值？")) return;
  localStorage.removeItem(KEY); location.reload();
};

render();
</script>
"""


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    rows = build_rows()
    html = (
        HTML.replace("__ROWS__", json.dumps(rows, ensure_ascii=False))
        .replace("__VOCAB__", json.dumps(VOCAB, ensure_ascii=False))
        .replace("__LABELS__", json.dumps(LABELS, ensure_ascii=False))
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"已生成 {out_path}({out_path.stat().st_size / 1024 / 1024:.1f} MB,{len(rows)} 张)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
