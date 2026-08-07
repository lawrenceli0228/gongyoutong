# 演示数据集(data/demo/)

这个目录**要进 git**(`.gitignore` 里 `/data/*` 之后跟着一条 `!/data/demo/` 例外),
其余 `data/` 下的内容(uploads / artifacts / cache / chroma / gyt.sqlite3)一律不进。

## 这里该放什么

| 子目录 | 放什么 | 谁用 | 大小约束 |
| --- | --- | --- | --- |
| `photos/` | 工地现场照片(含真隐患的、明显合规的各来几张) | Safety Agent 识图演示 + 评测集 | 单张 ≤ `photo_max_mb`(10 MB) |
| `docs/` | 规范条文 PDF / DOCX / TXT / MD | Knowledge Agent 检索演示 + 页码引用 | 单份 ≤ `document_max_mb`(10 MB) |
| `drawings/` | **已离线转好的 DXF**(DWG 不在白名单,后端不解析) | CAD Agent 查询与预览 | 单份 ≤ `drawing_max_mb`(20 MB) |

大小上限一律以 `gyt.config.get_settings()` 里的字段为准,不要在别处抄一份数字。

## 约定

- 空目录用 `.gitkeep` 占位,否则 git 记不住目录本身,别人冷启动会缺目录。
- 只放**可以公开**的素材:演示要在赛场投屏,别混进真实工地的敏感照片或甲方图纸。
- 文件名用 ASCII 或简短中文,避免空格 —— 后面接脚本处理时少一堆引号麻烦。

## W2 分工与数量(2026-08-06 补)

数据集责任跟着 Agent 走 —— 谁做这个 Agent,谁准备它的数据:

| 目录 | 谁准备 | 数量 | 对应评测集 |
| --- | --- | --- | --- |
| `photos/` | 你 | 30 张(违规 15 / 合规 7 / 难例 5 / 非工地 3) | `backend/eval/datasets/safety.csv` |
| `docs/` | 队友 | 10-20 份 | `backend/eval/datasets/rag.csv` |
| `drawings/` | 队友 | 3-5 份,**至少 1 份是 GBK 编码的中文图纸** | CAD 解析单测 |

命名:照片 `photo_01.jpg` ~ `photo_30.jpg`;GBK 那份图纸建议叫 `drawing_gbk_01.dxf`,
一眼能认出来。规范文档保留原文件名,但 `rag.csv` 的 `expected_source` 必须与之**逐字一致**。

**照片先压到 1-2MB 再提交。** 手机原图 5-15MB,30 张就是几百 MB,仓库会变得很难 clone;
评测只看识别结果,不需要原图画质。

```bash
sips -Z 2048 data/demo/photos/*.jpg   # macOS 批量压到长边 2048
```

怎么填标注、判分规则、违规项受控词表,全在 `backend/eval/README.md`。

## 照片来源与许可(2026-08-07)

### 27 张工地照片 —— Roboflow,CC BY 4.0

**许可 CC BY 4.0(署名即可商用/修改)**:

- 项目:`lawrence-lee-0i2uj/construction-safety-monitor-mlpd4-mwpvq` v1
- 来源:<https://universe.roboflow.com/lawrence-lee-0i2uj/construction-safety-monitor-mlpd4-mwpvq>
- 许可全文:<https://creativecommons.org/licenses/by/4.0/>
- 原始规模 5170 张,11 个类(helmet/no-helmet、vest/no-vest、boots/gloves/goggles 及其负类、person)

**我们只用它的图,标注是自己重打的。** 原因有三,都写在 `backend/eval/prefilter.py` 顶部:
粒度不同(它是逐对象框,我们要整张照片一个判断)、词表不同(它有 boots/gloves/goggles,
我们的 8 项受控词表里没有)、它的标注没为我们的评测门槛做过质量保证。

筛选过程可复现:

```bash
cd backend && uv run python -m eval.prefilter --src <解压目录> --dry-run   # 先看分桶
cd backend && uv run python -m eval.prefilter --src <解压目录>            # 拷图+出草稿
```

> **赛后若把仓库转公开(TODO-1),这段署名必须保留** —— CC BY 4.0 的唯一义务就是署名。

### 3 张非工地干扰项 —— Pexels

公开的工地数据集里当然没有非工地照片,这 3 张单独取自 Pexels。
**Pexels License:不要求署名、允许商用、允许修改**,唯一禁止的是「转投其它图库/壁纸平台」
和「未经修改直接做成实体商品出售」—— 都与本仓库无关。下表仍逐张记来源,便于日后核查:

| 文件 | Pexels ID | 来源页 | 这一张在测什么 |
| --- | --- | --- | --- |
| `photo_28.jpg` | 6835102 | <https://www.pexels.com/photo/6835102/> | 空置住宅客厅,**画面无人** —— 没人时不许编造违规 |
| `photo_29.jpg` | 6804068 | <https://www.pexels.com/photo/6804068/> | 办公室 4 人办公,**均无 PPE** —— 见人没戴安全帽不许乱报 |
| `photo_30.jpg` | 12938735 | <https://www.pexels.com/photo/12938735/> | 街头行人过街,**均无 PPE** —— 同上,换成室外 |

三张分别对应三种不同的过触发失败模式,不是三张同类图。
**关键是 29、30 里必须有人** —— 干扰项若全都没有人,「见人就喊未戴安全帽」这个最该防的
失败模式根本测不到,3 张干扰项等于白放。

### 新增照片的硬规矩(2026-08-07 立,踩过坑才立的)

> **只从 Pexels / Unsplash / Pixabay / Wikimedia(CC0 或 CC BY)取图,且必须走来源站的下载按钮。**
> **禁止从 Google 图片搜索结果里直接扒 CDN 链接。**
>
> 明确不收:**Freepik / Magnific**、Shutterstock、iStock、Getty、Adobe Stock。
>
> 尤其是 Freepik(现已更名 Magnific,`freepik.com/legal/terms-of-use` 301 跳到
> `magnific.com/legal/terms-of-use`,主体仍是 Freepik Company S.L.)。它的条款里有三条
> 直接卡死本项目,**免费档和付费档一样适用**:
>
> - 禁止再分发:*"Does not resell, assign, transfer or sublicense the Magnific Content or any derived work"*
> - 禁止入库:*"The Magnific Content or any derivative work is not used or included (in whole or in part) in a database, archive or in any other media/stock product, collection, set of clips, or library"*
> - **禁止 AI/ML 用途**:*"Does not use the Magnific Content (totally or partially) in any…machine learning and/or artificial intelligence purposes"*
>
> 第三条是当场违约,不用等仓库转公开 —— 本项目的全部用途就是把这些图喂给视觉模型跑评测,
> 正是条款点名禁止的行为。**转成 .jpg 也没用**,那仍属于 "any derived work"。
>
> 本目录曾经混进过两张 Freepik 图(从 Google 图片扒的 `img.magnific.com` 链接),已删除替换。
> 判断来源的最快办法是看 macOS 的下载来源属性,文件名会骗人、它不会:
>
> ```bash
> xattr -p com.apple.metadata:kMDItemWhereFroms <文件>   # 看真实下载 URL
> xattr -c <文件>                                        # 提交前清掉,别把 referrer 带进 git
> ```
>
> 另外注意:**文件名里写着 `pexels-` 不代表它来自 Pexels**。本目录曾有一张
> `pexels-jimmy-liao-*.jpg`,实际是从某台湾媒体 CDN 扒的、被裁成 banner 又重编码过的副本
> (`file` 能看到 `Intel(R) JPEG Library` 注释)。底图授权虽没问题,但要用就从 Pexels 原站重下。

### 11 张真工地违规照片(2026-08-07 换入,全部 Pexels)

**为什么要换:** 首轮评测发现原先从 Roboflow 挑的图里有 45% 根本不是建筑工地
(动物园游客、工厂车间、木工房、学童路队、儿童活动、采访摆拍、机械修理厂)。
根因是那个数据集的收集标准是「画面里有人穿戴/未穿戴 PPE」,不是「这是工地」——
详见 TODOS 的 TODO-19。

这 11 张的挑选流程与之前**根本不同**:先按类别搜索,再**逐张下载看图确认**
「确实是建筑工地」且「目标违规清晰可见」。共看过 106 张,留下 41 张,最终选用 11 张。
筛掉的 65 张里有 33 张就是「不是工地」——这道看图关卡正是上一轮缺的那一环。

| 文件 | 违规类别 | Pexels ID |
| --- | --- | --- |
| `photo_01.jpg` | 高空作业未系安全带 | 29438558 |
| `photo_02.jpg` | 高空作业未系安全带 | 15964928 |
| `photo_03.jpg` | 临边无防护 | 10420789 |
| `photo_04.jpg` | 临边无防护 | 30832160 |
| `photo_06.jpg` | 材料堆放混乱 | 36296037 |
| `photo_07.jpg` | 材料堆放混乱 | 27944997 |
| `photo_10.jpg` | 用电隐患 | 14546537 |
| `photo_11.jpg` | 用电隐患 | 12514711 |
| `photo_23.jpg` | 动火作业无监护 | 35383408 |
| `photo_24.jpg` | 动火作业无监护 | 9074493 |
| `photo_26.jpg` | 未戴安全帽 | 19408681 |

来源页一律是 `https://www.pexels.com/photo/<ID>/`。Pexels License:
不要求署名、允许商用与修改、无 AI 用途限制。下载后已 `xattr -c` 清掉来源属性。

**覆盖面因此从 2 类扩到 7 类** —— 仍缺「消防通道堵塞」:
在四个合规免费图库里搜了 10 张候选,没有一张同时满足「是工地」+「消防通道被堵」。
处理办法见 TODO-18(要么继续找,要么把这个词从受控词表里删掉)。
