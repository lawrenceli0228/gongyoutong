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
