/**
 * 用户消息的显示 —— 覆盖 agent-chat-ui 上游的 src/components/thread/messages/human.tsx
 *
 * ⚠️ 这个文件在 scripts/frontend-overrides/ 里,由 scripts/setup-frontend.sh 拷进
 *    frontend/。**别直接改 frontend/ 里那份** —— 那个目录不进 git,换台机器就没了。
 *
 * ── 为什么要覆盖上游 ────────────────────────────────────────────────
 *
 * 用户点发送的那一瞬间,`message.content` 是数组(base64 块),上游走
 * MultimodalPreview 渲染,图是好的。但这条消息进后端之后会被
 * `backend/src/gyt/core/uploads.py` 的 `ingest_uploads`(supervisor 的
 * pre_model_hook)**永久换掉** —— 它返回
 * `{"messages": [RemoveMessage(id=last.id), 改写后的纯文本]}`,
 * 带图的那条 HumanMessage 从 state 里删干净了。
 *
 * 为什么非得永久删(而不是只改模型输入):子 Agent 与 supervisor 共享同一份
 * messages,只改输入的话子 Agent 那边照样会拿到 image 块 —— 文本档模型收到
 * image 块直接 400。uploads.py 的注释里写明了这条。
 *
 * 于是刷新页面 / 翻历史时,用户看到的是这么一行:
 *     查一下这张照片(照片编号:27ba7e5933f54547b77a30772959b3fb)
 * 自己刚拍的照片变成一串 32 位十六进制。这个覆盖件就是把它变回图。
 *
 * **图已经不在消息里了,只能从产物目录捞** —— 见下面 ARTIFACT_BASE / byIdUrl。
 *
 * 上游的编辑态、复制按钮、分支切换、数组 content 那条分支全部原样保留,
 * 本文件只加「字符串 content 里的编号 → 图 / 图纸片」这一条新分支。
 */

import { useStreamContext } from "@/providers/Stream";
import { Message } from "@langchain/langgraph-sdk";
import { useState } from "react";
import { getContentString } from "../utils";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import { BranchSwitcher, CommandBar } from "./shared";
import { MultimodalPreview } from "@/components/thread/MultimodalPreview";
import { isBase64ContentBlock } from "@/lib/multimodal-utils";
import { File as FileIcon, ImageOff } from "lucide-react";

/**
 * 产物的静态出口。**与 scripts/frontend-overrides/tool-calls.tsx 的 ARTIFACT_BASE 同源。**
 *
 * 改一处必须改多处 —— 端口 8788 现在有三份真相:
 *   · Makefile 的 `ARTIFACTS_PORT`(起服务的那一处)
 *   · scripts/frontend-overrides/tool-calls.tsx 的 `ARTIFACT_BASE`(巡检记录卡片)
 *   · 本文件的 `ARTIFACT_BASE`(历史里的照片 / 图纸)
 * 漏改的现象是「不报错,只是打不开」,属于最难发现的那一类。
 *
 * 只绑 127.0.0.1 —— 与 docker-compose 端口那条红线同理:artifacts 里是工地现场
 * 照片和巡检记录,不许出本机。
 */
const ARTIFACT_BASE = "http://127.0.0.1:8788";

/**
 * 按 id 取产物的地址。
 *
 * 为什么必须由服务端按 id 找,前端不许自己拼路径 ——
 * 产物在盘上的长相是(backend/src/gyt/core/artifacts.py):
 *     data/artifacts/<UTC 日期 YYYYMMDD>/<32 位小写 hex>.<扩展名>
 * 而**前端手里只有那 32 位 id**:日期段没有,扩展名也没有(jpg / png / webp 都可能)。
 * 按「今天」拼是错的 —— 目录名走的是 `datetime.now(UTC)`,北京时间晚上八点以后
 * 演示,文件落在「明天」那个文件夹里;跨零点的会话更是横跨两个目录。
 * 这就是本次的核心难点,解法是把「按 id 找文件」这件事留在服务端。
 *
 * ⚠️ 裸 `python3 -m http.server` 没有 /by-id/ 这条路由,得由产物服务提供。
 *    服务没起 / 路由不存在 / 文件被清掉 —— 全是 404,一律走 ArtifactPhoto 的
 *    onError 兜底(退回显示编号 + 一句人话),**不出裂图标**。
 */
const byIdUrl = (id: string): string => `${ARTIFACT_BASE}/by-id/${id}`;

/**
 * 产物 id 的长相:32 位**小写** hex(`core/artifacts.py` 的 `register()` 发的号)。
 *
 * 正则必须严格。宽松匹配的代价很具体:工友随口说的「编号 3 号楼那个」、模型复读的
 * 巡检记录编号 `GYT-20260810-143012`,都会被当成产物 id 拿去取图 —— 取不到就是
 * 一片兜底提示,比不渲染还吵。大写 hex 也不认,那不是我们发的号。
 */
const ID_PATTERN = "[0-9a-f]{32}";

/**
 * 抓「(照片编号:…)」/「(图纸编号:…)」这一段。
 *
 * 格式的唯一真相在 `backend/src/gyt/core/uploads.py` 拼 body 的那两行 f-string
 * (写这行时在 234 / 240,行号会漂,认下面这两句原文):
 *     body = f"{body}\n(照片编号:{listed})" if body else f"看看这张照片。(照片编号:{listed})"
 *     body = f"{body}\n(图纸编号:{listed})" if body else f"看看这张图纸。(图纸编号:{listed})"
 * 三条要点:
 *   · 多张用**顿号「、」**(U+3001)连接,不是逗号、不是空格;
 *   · 括号和冒号是**半角**(逐字节核对过 uploads.py 那两行;下面的正则同时收全角,
 *     纯属防御 —— 哪天有人顺手把文案改成中文标点,这边不至于当场瞎掉);
 *   · **「照片」和「图纸」是两个词,不许串** —— safety 认照片编号、cad 认图纸编号,
 *     串了等于把 DXF 塞进 <img>,界面上就是一个裂图标。
 *
 * 末尾那个 `[ \t]*` 是连着编号块一起吃掉的:uploads.py 在编号块后面接提示语时用的是
 * 空格(`body = f"{body} {_UNSUPPORTED_HINT}"`),只摘括号会在行首留一个孤零零的空格。
 * 实测 `帮我看看\n(照片编号:…) 有 1 个附件不是图片` 摘完是 `帮我看看\n有 1 个附件不是图片`。
 *
 * 每次调用都新建一个 RegExp:带 `g` 的正则对象有 lastIndex 状态,复用同一个实例
 * 会在第二次匹配时莫名其妙地漏掉开头那一段。
 */
const refPattern = (word: "照片" | "图纸"): RegExp =>
  new RegExp(
    `[(（]${word}编号[:：]\\s*(${ID_PATTERN}(?:、${ID_PATTERN})*)[)）][ \\t]*`,
    "g",
  );

type ArtifactRefs = {
  photos: string[];
  drawings: string[];
  /** 摘掉编号块之后剩下的正文 */
  text: string;
};

/** content 不是字符串(上传那一瞬间)时用的空结果。提到组件外面是为了每次渲染都拿同一个对象。 */
const NO_REFS: ArtifactRefs = { photos: [], drawings: [], text: "" };

/** 从正文里摘出一类编号,返回「抓到的 id」和「摘干净之后的正文」,不改入参。 */
function takeRefs(
  raw: string,
  word: "照片" | "图纸",
): { ids: string[]; rest: string } {
  const ids: string[] = [];
  const rest = raw.replace(refPattern(word), (_whole, listed: string) => {
    ids.push(...listed.split("、"));
    return "";
  });
  return { ids, rest };
}

/** 摘掉编号块会留下空行和行尾空格(编号块前面是 `\n`),收拾干净再上屏。 */
function tidy(text: string): string {
  return text
    .replace(/[ \t]+$/gm, "")
    .replace(/\n{2,}/g, "\n")
    .trim();
}

function splitArtifactRefs(raw: string): ArtifactRefs {
  const photo = takeRefs(raw, "照片");
  const drawing = takeRefs(photo.rest, "图纸");
  return {
    photos: photo.ids,
    drawings: drawing.ids,
    text: tidy(drawing.rest),
  };
}

/**
 * 历史里的一张现场照片。
 *
 * 取不到时**不许出裂图标** —— 演示时「忘了起产物服务」是很可能发生的事,
 * 一排断图会让人以为照片丢了(其实照片好好躺在 data/artifacts 里,只是没人递出来)。
 * 所以 onError 退回显示编号 + 一句工地师傅也看得懂的话。
 */
function ArtifactPhoto({ id }: { id: string }) {
  const [broken, setBroken] = useState(false);

  if (broken) {
    return (
      <div className="flex max-w-[16rem] items-start gap-2 rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 text-left">
        <ImageOff className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />
        <div className="min-w-0">
          <div className="text-[13px] text-gray-600">照片暂时打不开</div>
          <div className="mt-0.5 font-mono text-[11px] break-all text-gray-400 select-all">
            {id}
          </div>
          {/* ⚠️ 这句话**不许写死成「服务没起」**。2026-08-11 对抗复核指出:
              <img> 的 onError 分不清 404 的原因,而两种原因的正确动作完全相反。
              第二种(文件不在这台机器上)恰恰是最常发生的:按验收流程清过
              data/、或者换台机器打开旧线程 —— 服务开着、端口通着,而界面
              教人去「起服务」,人照做、无效,然后开始查端口查防火墙,方向全错。
              这与 CLAUDE.md 里 `rm -f backend/data/...` 那条踩过的坑是同一个形状:
              **一条恒定的错误指引,比不给指引贵得多。** */}
          <div className="mt-1 text-[11px] leading-snug text-gray-400">
            照片本身没丢。两种可能:取件的服务没起(仓库根执行{" "}
            <code className="font-mono">make serve-artifacts</code>
            ),或者这台机器上没有这份文件(数据目录被清过,或者照片是别的机器传的)。
          </div>
        </div>
      </div>
    );
  }

  return (
    <figure className="m-0 flex flex-col items-end gap-1">
      <a
        href={byIdUrl(id)}
        target="_blank"
        rel="noreferrer"
        title="点开看大图"
      >
        {/* 用原生 <img> 而不是 next/image:next/image 要求把远端主机写进
            next.config.mjs 的 images.remotePatterns,而产物服务的端口是可改的
            (`make serve-artifacts ARTIFACTS_PORT=…`),配死在 next.config 里更脆;
            而且这里只是聊天气泡里的缩略图,用不上 next/image 那套优化。

            这里**不要**加 `eslint-disable-next-line @next/next/no-img-element`:
            本仓 frontend/eslint.config.js 是 typescript-eslint 那套扁平配置,
            压根没装 next 插件,写了这行 eslint 会直接报
            「Definition for rule '@next/next/no-img-element' was not found」——
            实测过一次,为了压一个不存在的告警反而把 lint 弄红了。 */}
        <img
          src={byIdUrl(id)}
          alt={`工地照片 ${id.slice(0, 8)}`}
          onError={() => setBroken(true)}
          className="max-h-56 max-w-[16rem] rounded-xl border border-gray-200 object-contain transition-opacity hover:opacity-90"
        />
      </a>
      {/* 编号降级成图注:小字、等宽、点一下整串选中(select-all)方便复制。
          留着它的理由见下面 `bodyText` 那段注释 —— 编号是取件凭证,不能只剩图。 */}
      <figcaption
        className="font-mono text-[11px] break-all text-gray-400 select-all"
        title="照片编号"
      >
        {id}
      </figcaption>
    </figure>
  );
}

/**
 * 历史里的一张图纸(DXF)。
 *
 * **不塞 <img>** —— 浏览器渲染不了 DXF,塞进去必然是个裂图标,还会让人以为图纸坏了。
 * 给一片可点开的文件片就行,点开走同一个 by-id 端点(交给浏览器下载 / 另存)。
 * 长相刻意跟 MultimodalPreview 里 DXF 那条分支对齐(File 图标 + 蓝色 + 「图纸」二字),
 * 这样上传那一瞬间和事后翻历史看到的是同一个东西。
 */
function ArtifactDrawing({ id }: { id: string }) {
  return (
    <a
      href={byIdUrl(id)}
      target="_blank"
      rel="noreferrer"
      title="点开取图纸文件"
      className="flex max-w-[16rem] items-start gap-2 rounded-md border border-gray-200 bg-gray-100 px-3 py-2 text-left transition-colors hover:bg-gray-200"
    >
      <FileIcon className="mt-0.5 h-5 w-5 shrink-0 text-blue-700" />
      <div className="min-w-0">
        <div className="text-sm text-gray-800">图纸</div>
        <div className="mt-0.5 font-mono text-[11px] break-all text-gray-500">
          {id}
        </div>
      </div>
    </a>
  );
}

function EditableContent({
  value,
  setValue,
  onSubmit,
}: {
  value: string;
  setValue: React.Dispatch<React.SetStateAction<string>>;
  onSubmit: () => void;
}) {
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      onSubmit();
    }
  };

  return (
    <Textarea
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onKeyDown={handleKeyDown}
      className="focus-visible:ring-0"
    />
  );
}

export function HumanMessage({
  message,
  isLoading,
}: {
  message: Message;
  isLoading: boolean;
}) {
  const thread = useStreamContext();
  const meta = thread.getMessagesMetadata(message);
  const parentCheckpoint = meta?.firstSeenState?.parent_checkpoint;

  const [isEditing, setIsEditing] = useState(false);
  const [value, setValue] = useState("");
  const contentString = getContentString(message.content);

  // 只在 content 是**字符串**时找编号 —— 那是被 ingest_uploads 改写过的历史消息。
  // content 还是数组时说明消息刚发出去、还没进后端,图就在本地 base64 块里,
  // 走下面上游那条 MultimodalPreview 分支;这时候正文里根本没有编号,
  // 硬去解析只会白跑一趟,还可能和上游分支双份渲染。
  const refs =
    typeof message.content === "string"
      ? splitArtifactRefs(message.content)
      : NO_REFS;
  const hasRefs = refs.photos.length > 0 || refs.drawings.length > 0;

  /**
   * 正文里那段「(照片编号:…)」怎么处理 —— 显示成图之后它就是噪声,这里的取舍是
   * **从气泡正文里摘掉,但绝不让编号消失**:
   *
   *   · 编号是工友和管理员之间的**取件凭证**(报编号去取件、对账、在电话里念),
   *     完全抹掉是有代价的 —— 屏幕上那张图和「那张照片」之间就断了名字。
   *   · 所以编号降级成缩略图下面的小字图注(ArtifactPhoto 的 figcaption /
   *     ArtifactDrawing 的第二行),点一下整串选中,该念还能念。
   *   · 更要紧的是:上游的**复制按钮和编辑态用的都是 `contentString` 原文**
   *     (含编号),本文件一个字没动 —— 「复制这条消息」拿到的仍然是带编号的全文,
   *     点编辑看到的也是原文。凭证走的是这条路,气泡里那段只是重复。
   *
   * 没抓到编号时直接用 `contentString`,而不是 `refs.text`:后者过了 tidy(),
   * 会顺手 trim 掉用户自己打的空行 —— 没东西要摘的时候不该动人家的字。
   */
  const bodyText = hasRefs ? refs.text : contentString;

  const handleSubmitEdit = () => {
    setIsEditing(false);

    const newMessage: Message = { type: "human", content: value };
    thread.submit(
      { messages: [newMessage] },
      {
        checkpoint: parentCheckpoint,
        streamMode: ["values"],
        streamSubgraphs: true,
        streamResumable: true,
        optimisticValues: (prev) => {
          const values = meta?.firstSeenState?.values;
          if (!values) return prev;

          return {
            ...values,
            messages: [...(values.messages ?? []), newMessage],
          };
        },
      },
    );
  };

  return (
    <div
      className={cn(
        "group ml-auto flex items-center gap-2",
        isEditing && "w-full max-w-xl",
      )}
    >
      <div className={cn("flex flex-col gap-2", isEditing && "w-full")}>
        {isEditing ? (
          <EditableContent
            value={value}
            setValue={setValue}
            onSubmit={handleSubmitEdit}
          />
        ) : (
          <div className="flex flex-col gap-2">
            {/* 上游原样保留:上传那一瞬间 content 还是数组(base64 块),本地直接渲染。
                这条路不许弄坏 —— 它是「点了发送就立刻看得见自己那张图」的唯一来源,
                而 ingest_uploads 的改写要等消息进了后端才发生。 */}
            {Array.isArray(message.content) && message.content.length > 0 && (
              <div className="flex flex-wrap items-end justify-end gap-2">
                {message.content.reduce<React.ReactNode[]>(
                  (acc, block, idx) => {
                    if (isBase64ContentBlock(block)) {
                      acc.push(
                        <MultimodalPreview
                          key={idx}
                          block={block}
                          size="md"
                        />,
                      );
                    }
                    return acc;
                  },
                  [],
                )}
              </div>
            )}

            {/* 本覆盖件新增:历史消息里的照片 / 图纸(content 已被后端换成纯文本)。
                key 带上下标 —— 同一条消息里理论上不会出现重复 id,但真出现了
                React 会报重复 key,渲染顺序还可能错位,不值当为这点省事冒险。 */}
            {hasRefs && (
              <div className="ml-auto flex flex-col items-end gap-1.5">
                {refs.photos.length > 0 && (
                  <div className="flex flex-wrap items-end justify-end gap-2">
                    {refs.photos.map((id, idx) => (
                      <ArtifactPhoto
                        key={`${id}-${idx}`}
                        id={id}
                      />
                    ))}
                  </div>
                )}
                {refs.drawings.length > 0 && (
                  <>
                    <div className="flex flex-wrap items-end justify-end gap-2">
                      {refs.drawings.map((id, idx) => (
                        <ArtifactDrawing
                          key={`${id}-${idx}`}
                          id={id}
                        />
                      ))}
                    </div>
                    {/* 图纸片没有 onError 可挂(不是 <img>),所以把那句提示常驻。
                        措辞与 tool-calls.tsx 巡检记录卡片底下那行保持一致。 */}
                    <div className="text-[11px] text-gray-400">
                      打不开?先在仓库根执行{" "}
                      <code className="font-mono">make serve-artifacts</code>
                    </div>
                  </>
                )}
              </div>
            )}

            {/* Render text if present, otherwise fallback to file/image name */}
            {bodyText ? (
              <p className="bg-muted ml-auto w-fit rounded-3xl px-4 py-2 text-right whitespace-pre-wrap">
                {bodyText}
              </p>
            ) : null}
          </div>
        )}

        <div
          className={cn(
            "ml-auto flex items-center gap-2 transition-opacity",
            "opacity-0 group-focus-within:opacity-100 group-hover:opacity-100",
            isEditing && "opacity-100",
          )}
        >
          <BranchSwitcher
            branch={meta?.branch}
            branchOptions={meta?.branchOptions}
            onSelect={(branch) => thread.setBranch(branch)}
            isLoading={isLoading}
          />
          {/* content / setValue 都喂**原文**(含编号)—— 复制和编辑要拿到凭证,
              上游这段一个字没改,别为了"干净"把 contentString 换成 bodyText。 */}
          <CommandBar
            isLoading={isLoading}
            content={contentString}
            isEditing={isEditing}
            setIsEditing={(c) => {
              if (c) {
                setValue(contentString);
              }
              setIsEditing(c);
            }}
            handleSubmitEdit={handleSubmitEdit}
            isHumanMessage={true}
          />
        </div>
      </div>
    </div>
  );
}
