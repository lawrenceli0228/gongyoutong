# NOTICE

工友通(GYT · Gongyoutong)—— 建筑工地多智能体助手。

    Copyright (c) 2026 Lawrence L, Chenghao Fan

本项目以 **MIT License** 授权,全文见 [`LICENSE`](LICENSE)。

⚠️ **MIT 只覆盖「本项目自己写的东西」。** 仓库里还有一批第三方素材(演示照片、
规范文本、前端上游代码),它们各自有各自的许可,**不随本项目的 MIT 再许可**。
本文件就是这份清单 —— 其中 CC BY 4.0 的署名是**义务**,不是礼貌。

---

## 一、第三方素材

### 1.1 前端:LangChain 官方 agent-chat-ui(MIT)

`frontend/` **不进 git**,由 [`scripts/setup-frontend.sh`](scripts/setup-frontend.sh)
从上游 clone 生成;本仓只保存打在它上面的补丁
(`scripts/frontend-overrides/`)。

    agent-chat-ui — MIT License
    Copyright (c) 2025 Brace Sproul
    https://github.com/langchain-ai/agent-chat-ui

### 1.2 后端依赖

LangGraph / LangChain / Pydantic / Pillow / ezdxf / python-docx / reportlab /
Chroma / sentence-transformers 等,各自的许可以
[`backend/pyproject.toml`](backend/pyproject.toml) 与 `backend/uv.lock` 为准。
嵌入模型 **BAAI/bge-m3** 由 HuggingFace 下载,不在本仓分发。

### 1.3 演示照片 `data/demo/photos/`(30 张)

**逐张的来源、许可与筛选过程记在
[`data/demo/README.md`](data/demo/README.md)**,那里是唯一真相,本节只做汇总。

**14 张来自 Pexels** —— Pexels License:不要求署名、允许商用与修改。

**16 张来自 Roboflow 的公开数据集** —— 打包者声明的许可是 **CC BY 4.0**,
署名如下(这是 CC BY 4.0 唯一的义务):

    construction-safety-monitor-mlpd4-mwpvq (v1)
    by lawrence-lee-0i2uj, via Roboflow Universe
    https://universe.roboflow.com/lawrence-lee-0i2uj/construction-safety-monitor-mlpd4-mwpvq
    Licensed under CC BY 4.0 — https://creativecommons.org/licenses/by/4.0/
    本项目只使用其图像;标注为本项目重新标注(理由见 backend/eval/prefilter.py 头注)。

> ⚠️ **关于这 16 张的一个已知局限,写在明处:**
> 它们来自一个**用户上传**的聚合数据集。打包者对整个数据集声明了 CC BY 4.0,
> 但**逐张图的原始出处无法由我们独立核实** —— 这类数据集常混有来源不明的存量图。
> 我们保留它们是为了评测集的可复现性(`backend/eval/datasets/safety.csv`
> 的标注与之逐行对应),不代表我们能担保每一张的权利状态。
>
> **权利人若认为其作品被误收,请通过本仓库的 GitHub Issues 联系,我们会立即移除并替换。**

### 1.4 照片中的人物

部分照片含可辨识人物,且在评测中被标注为「安全违规」样例。
这些图像仅用于**技术评测与学术演示**,不构成对画面中任何个人的评价。
如涉及本人或其代理人提出异议,同样按上一条处理:**立即移除**。

### 1.5 规范文本 `data/demo/docs/`

    GB 50016-2014《建筑设计防火规范》(2018年版)

中华人民共和国国家标准,著作权归其发布与出版机构所有。仓库中包含该文件**仅为
让检索功能可以开箱演示**;它不是本项目的一部分,也不随本项目的 MIT 授权再许可。

**自行部署时建议移除该 PDF,改由使用者自备。** 检索链路不依赖这一份特定文件 ——
把任意规范 PDF 放进 `data/demo/docs/` 再 `make build-knowledge` 即可。

---

## 二、移除请求

任何权利人(图片、文本、商标、肖像)如认为本仓库中的素材侵害其权益,
请在 GitHub Issues 中提出并指明文件路径。我们的处理原则是**先移除,再讨论**。
