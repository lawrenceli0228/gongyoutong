/**
 * supervision-lib.ts(scripts/frontend-overrides/)的单测 —— W9 · S6 泳道。
 * 跑法:cd scripts/frontend-tests && pnpm install && pnpm vitest run
 *       (仓库根:make test-frontend,两份 *.test.ts 一起跑)
 *
 * 锁的都是**静默出错**的东西 —— 这一层的失败方式全部安静:
 *   · `documents` 数组少一项 → 界面少一张卡,没有任何异常,而工友以为文书没出;
 *   · 状态中文名漂了 → 界面上冒出一个英文状态词,没人会当成 bug 报;
 *   · `availableActions` 比服务端宽 → 给人一颗点下去必挨骂的按钮,而工友会以为系统坏了;
 *   · `availableActions` 漏了硬拦 → 一般隐患的界面上出现「签发暂停令」,
 *     点下去被服务端拦住(硬拦②在服务端,拦得住),但这时候人已经在演示台上了。
 *
 * 契约的唯一真相在 backend/src/gyt/supervision_api.py 的模块 docstring;
 * 状态/级别词表在 backend/src/gyt/db/hazards.py。本文件的期望值抄自那两处。
 */

import { describe, expect, it } from "vitest";

import {
  ACTION_ENDPOINT,
  ACTION_LABEL,
  actionBody,
  actionNeedsConfirm,
  actionNeedsDuePhrase,
  actionNeedsPhoto,
  availableActions,
  confirmBody,
  confirmPrompt,
  describeDocuments,
  DISPOSAL_ACTIONS,
  DisposalAction,
  documentsFromToolData,
  documentUrl,
  docTypeZh,
  DOC_TYPE_ZH,
  failedItemsFromToolData,
  GRADE_NORMAL,
  GRADE_SEVERE,
  HAZARD_GRADES,
  HAZARD_SCOPE_ACTIVE,
  HAZARD_SCOPE_ALL,
  HAZARD_SCOPE_OVERDUE,
  HAZARD_SCOPE_PENDING,
  HAZARD_SCOPES,
  HAZARD_SOURCE_TOOLS,
  HAZARD_STATUS_ZH,
  HAZARD_STATUSES,
  HazardBrief,
  hazardDetailUrl,
  hazardListUrl,
  hazardsFromToolData,
  hazardStatusZh,
  isDownloadableDoc,
  evidenceRows,
  formatHkMoment,
  MAX_CONFIRM_BATCH,
  mergeHazards,
  NETWORK_ERROR_STATUS,
  normalizeError,
  parseActionEnvelope,
  parseConfirmEnvelope,
  parseHazardDetailEnvelope,
  parseHazardListEnvelope,
  patchHazard,
  pendingHazards,
  REINSPECT_DOC_TYPE,
  REJECTED_STATUS,
  removeHazard,
  SUPERVISION_ENDPOINTS,
  SUPERVISION_MESSAGES,
  SupervisionContractError,
  supervisionUrl,
  toggleSelected,
} from "../frontend-overrides/supervision-lib";

/** 造一条隐患,单测里按需覆盖字段。默认是最常见的那一条:待确认、一般、已定级。 */
function hazard(overrides: Partial<HazardBrief> = {}): HazardBrief {
  return {
    hazard_no: "GYT-H-20260816-093000-1a2b",
    item: "未戴安全帽",
    grade: GRADE_NORMAL,
    status: "pending",
    needs_grading: false,
    ...overrides,
  };
}

/** 一份合法的三文书信封(supervision_api docstring 里那个例子,顺序固定)。 */
const SUSPEND_ENVELOPE = JSON.stringify({
  ok: true,
  data: {
    hazard_no: "GYT-H-20260816-093000-1a2b",
    status: "suspended",
    documents: [
      {
        doc_type: "notice",
        doc_no: "GYT-TZ-20260816-093012-aa11",
        artifact_id: "0123456789abcdef0123456789abcdef",
        filename: "监理通知单_GYT-TZ-20260816-093012-aa11.docx",
      },
      {
        doc_type: "suspension",
        doc_no: "GYT-ZT-20260816-093012-bb22",
        artifact_id: "1123456789abcdef0123456789abcdef",
        filename: "工程暂停令_GYT-ZT-20260816-093012-bb22.docx",
      },
      {
        doc_type: "owner_report",
        doc_no: "GYT-JS-20260816-093012-cc33",
        artifact_id: "2123456789abcdef0123456789abcdef",
        filename: "致建设单位报告_GYT-JS-20260816-093012-cc33.docx",
      },
    ],
  },
  user_msg: "三份文书已出稿",
  error_code: null,
});

// ---------------------------------------------------------------------------

describe("端点地址(路径写错的表现是 404,而 404 会被说成「接口还没开通」—— 方向全错)", () => {
  it("八条 POST 端点齐全,与 supervision_api.SUPERVISION_ROUTES 一一对应", () => {
    expect([...SUPERVISION_ENDPOINTS]).toEqual([
      "confirm",
      "grade",
      "notice",
      "suspend",
      "reinspect-result",
      "resume",
      "escalate",
      "reject",
    ]);
  });

  it("两个 GET 查询端点**不在**这张表里 —— 它们有各自的构造函数(三态与转义在那儿)", () => {
    // 混进来的话,`supervisionUrl` 那种裸拼接会把 project_id 三态和编号转义一起吃掉。
    expect(SUPERVISION_ENDPOINTS).not.toContain("hazards");
    expect(hazardListUrl("http://x")).toContain("/supervision/hazards");
  });

  it("复查那条是**连字符** reinspect-result,不是下划线", () => {
    expect(supervisionUrl("http://localhost:2024", "reinspect-result")).toBe(
      "http://localhost:2024/supervision/reinspect-result",
    );
  });

  it("apiBase 末尾多几个斜杠也不会拼出 //(公网那份来自 build args,常带斜杠)", () => {
    expect(supervisionUrl("https://x.example/api//", "confirm")).toBe(
      "https://x.example/api/supervision/confirm",
    );
  });

  it("每个界面动作都有端点、都有按钮文案 —— 少一条的表现是按钮点了没反应", () => {
    for (const action of DISPOSAL_ACTIONS) {
      expect(SUPERVISION_ENDPOINTS).toContain(ACTION_ENDPOINT[action]);
      expect(ACTION_LABEL[action]).toBeTruthy();
    }
  });
});

describe("查询端点地址(W10:操作台自己去取数,不等模型给)", () => {
  const BASE = "http://localhost:2024";
  const LIST = `${BASE}/supervision/hazards`;

  it("🔴 三态之一:不传 projectId → query 里根本没有这个键(= 不筛工地,看全部)", () => {
    // 这一态映射后端 list_rows(project_id=None)。写成 `set("project_id", id ?? "")`
    // 的话它会变成下一态(只看未归属)—— 屏幕上是一张短得多的清单,没有任何报错。
    expect(hazardListUrl(BASE)).toBe(LIST);
    expect(hazardListUrl(BASE, {})).toBe(LIST);
    expect(hazardListUrl(BASE, { scope: HAZARD_SCOPE_OVERDUE })).not.toContain("project_id");
  });

  it("🔴 三态之二:projectId 是空串 → 有键无值 `project_id=`(= 只看未归属)", () => {
    // D6:未归属是空串不是 NULL。这一态要是被真值判断吞掉(`if (projectId)`),
    // 那批没人认领的隐患就永远筛不出来 —— 而它恰恰是最该被人看见的一批。
    expect(hazardListUrl(BASE, { projectId: "" })).toBe(`${LIST}?project_id=`);
  });

  it("🔴 三态之三:projectId 有值 → 按值筛", () => {
    expect(hazardListUrl(BASE, { projectId: "P-tko" })).toBe(`${LIST}?project_id=P-tko`);
  });

  it("null 与「不传」是同一态(它对应后端的 None)—— 三态里没有第四种", () => {
    expect(hazardListUrl(BASE, { projectId: null })).toBe(LIST);
    // 与空串那一态必须拼出**不一样**的东西,这是整段的要害
    expect(hazardListUrl(BASE, { projectId: null })).not.toBe(
      hazardListUrl(BASE, { projectId: "" }),
    );
  });

  it("scope 不传就不写这个键 —— 缺省归后端,前端不复制一份「在办」", () => {
    // 复制一份缺省的下场:后端哪天改缺省档,前端还固执地发着老的那个词。
    expect(hazardListUrl(BASE, { scope: "  " })).toBe(LIST);
  });

  it("scope 显式给:中文按 UTF-8 百分号转义(不转义 = 后端收到乱码然后 400)", () => {
    expect(hazardListUrl(BASE, { scope: HAZARD_SCOPE_PENDING })).toBe(
      `${LIST}?scope=%E5%BE%85%E7%A1%AE%E8%AE%A4`,
    );
    expect(hazardListUrl(BASE, { scope: HAZARD_SCOPE_ALL })).toBe(
      `${LIST}?scope=%E5%85%A8%E9%83%A8`,
    );
  });

  it("两个都给:scope 在前、project_id 在后;apiBase 末尾多几个斜杠也不拼出 //", () => {
    expect(hazardListUrl("https://x.example/api//", { scope: HAZARD_SCOPE_ACTIVE, projectId: "" })).toBe(
      "https://x.example/api/supervision/hazards?scope=%E5%9C%A8%E5%8A%9E&project_id=",
    );
  });

  it("详情地址:编号一律转义 —— 斜杠不许原样进路径", () => {
    // 今天的编号全是安全字符,转不转义拼出来一样;但编号格式归 core/doc_no.py 管,
    // 哪天多一段带斜杠的东西,不转义就是打到别的路由上(404 又会被说成「接口没开通」)。
    expect(hazardDetailUrl(BASE, "GYT-H-20260816-171404-7194")).toBe(
      `${LIST}/GYT-H-20260816-171404-7194`,
    );
    expect(hazardDetailUrl(BASE, "GYT-H/../secret")).toBe(`${LIST}/GYT-H%2F..%2Fsecret`);
  });

  it("详情地址:编号首尾空格 trim 掉 —— 从聊天记录里复制编号常带一个", () => {
    // 不 trim 的话尾空格会变成 %20,然后 404,而屏幕上那串编号看着完全正常。
    expect(hazardDetailUrl(BASE, " GYT-H-1 ")).toBe(`${LIST}/GYT-H-1`);
  });
});

describe("受控词表(漂了的表现是界面上冒出英文,没人会当成 bug 报)", () => {
  it("八个状态每个都有中文名", () => {
    for (const status of HAZARD_STATUSES) {
      expect(HAZARD_STATUS_ZH[status]).toBeTruthy();
    }
  });

  it("🔴 suspended 只能念「已出具暂停令」,不许升级成「责令停工」", () => {
    // 出稿 ≠ 工地真停了工(db/hazards.py 的 STATUSES 头注 + supervision_api 那条红线)。
    // 对外措辞一旦升级,系统就在替一件没发生的事背书。
    expect(HAZARD_STATUS_ZH.suspended).toBe("已出具暂停令");
    expect(HAZARD_STATUS_ZH.suspended).not.toContain("停工");
  });

  it("词表外的状态原样透出,不抛也不猜(后端加一档时界面不许炸)", () => {
    expect(hazardStatusZh("brand_new_status")).toBe("brand_new_status");
  });

  it("级别只有一般/严重两档", () => {
    expect([...HAZARD_GRADES]).toEqual([GRADE_NORMAL, GRADE_SEVERE]);
  });

  it("清单筛子四个词、顺序不变 —— 它同时是界面上四颗按钮的顺序", () => {
    // 逐字镜像 agents/supervision/scoping.py 的 SCOPES。写错一个字的表现是后端 400,
    // 而那句 400 会被 normalizeError 原样上屏 —— 工友看到的是「我这儿没有这个词」。
    expect([...HAZARD_SCOPES]).toEqual(["在办", "待确认", "超期", "全部"]);
    expect(HAZARD_SCOPES[0]).toBe(HAZARD_SCOPE_ACTIVE); // 缺省档排第一
    expect(HAZARD_SCOPE_PENDING).toBe("待确认");
    expect(HAZARD_SCOPE_OVERDUE).toBe("超期");
    expect(HAZARD_SCOPE_ALL).toBe("全部");
  });

  it("🔴 「已删除」不许混进八档状态词表 —— 库里永远查不到这么一条", () => {
    // reject 回执里那个 status 是 deleted:那一行已经没了,它没有状态。
    // 混进 HAZARD_STATUSES 的下场是状态徽章里多出一档后端不认的值。
    expect(REJECTED_STATUS).toBe("deleted");
    expect(HAZARD_STATUSES).not.toContain(REJECTED_STATUS);
    expect(HAZARD_STATUS_ZH).not.toHaveProperty(REJECTED_STATUS);
  });

  it("五种文书 + 复查记录都有中文名;认不出的原样透出", () => {
    expect(docTypeZh("notice")).toBe("监理通知单");
    expect(docTypeZh("suspension")).toBe("工程暂停令");
    expect(docTypeZh("resumption")).toBe("工程复工令");
    expect(docTypeZh("owner_report")).toBe("致建设单位报告");
    expect(docTypeZh("authority_report")).toBe("监理报告");
    expect(docTypeZh("reinspect")).toBe("复查记录");
    expect(Object.keys(DOC_TYPE_ZH)).toHaveLength(6);
    expect(docTypeZh("weekly_summary")).toBe("weekly_summary");
  });
});

describe("availableActions —— 服务端四道闸在界面上的投影(只许更窄,永远不许更宽)", () => {
  it("needs_grading=1 → **签发那几件一件都不给**(硬拦③:任何签发都会被服务端拒)", () => {
    const 签发动作 = ["notice", "suspend", "reinspect", "resume", "escalate"] as const;
    for (const status of ["pending", "open"] as const) {
      for (const grade of HAZARD_GRADES) {
        const actions = availableActions(hazard({ status, grade, needs_grading: true }));
        // 断的是「签发一件都没有」而不是「恰好等于 ['grade']」—— 后者会在放开
        // 「未定级的 pending 可以否决」时误红,而那条放开与硬拦③无关(否决不是签发)。
        for (const 动作 of 签发动作) expect(actions).not.toContain(动作);
        expect(actions).toContain("grade");
      }
    }
  });

  it("open + 一般 → 给通知单,**不给暂停令**(硬拦②:平白停一片人的工)", () => {
    const actions = availableActions(hazard({ status: "open", grade: GRADE_NORMAL }));
    expect(actions).toContain("notice");
    expect(actions).not.toContain("suspend");
  });

  it("open + 严重 → 给暂停令,**不给通知单**(硬拦①:该停工的没停)", () => {
    const actions = availableActions(hazard({ status: "open", grade: GRADE_SEVERE }));
    expect(actions).toContain("suspend");
    expect(actions).not.toContain("notice");
    // 主要动作排第一 —— 手最先够到的那颗要是对的那颗
    expect(actions[0]).toBe("suspend");
  });

  it("pending / open 都能改级别(_GRADABLE_STATUSES 就是这两档)", () => {
    expect(availableActions(hazard({ status: "pending" }))).toContain("grade");
    expect(availableActions(hazard({ status: "open" }))).toContain("grade");
  });

  it("签过文书之后不给改级别 —— 改了,发出去的那份纸就和台账对不上", () => {
    for (const status of ["notified", "suspended", "resuming", "closed"] as const) {
      expect(availableActions(hazard({ status }))).not.toContain("grade");
    }
  });

  it("notified / suspended → 只能登记复查结论", () => {
    expect(availableActions(hazard({ status: "notified" }))).toEqual(["reinspect"]);
    expect(availableActions(hazard({ status: "suspended", grade: GRADE_SEVERE }))).toEqual([
      "reinspect",
    ]);
  });

  it("reinspect_failed → 再复查一次,或者上报主管部门", () => {
    expect(availableActions(hazard({ status: "reinspect_failed" }))).toEqual([
      "reinspect",
      "escalate",
    ]);
  });

  it("🔴 上报只从 reinspect_failed 进 —— 举证链要「通知过+期限到了+复查过+没改」", () => {
    for (const status of HAZARD_STATUSES) {
      if (status === "reinspect_failed") continue;
      expect(availableActions(hazard({ status }))).not.toContain("escalate");
    }
  });

  it("resuming → 只能签复工令(停过工的收尾,漏发/滥发都是 Codex#6 那个坑)", () => {
    expect(availableActions(hazard({ status: "resuming", grade: GRADE_SEVERE }))).toEqual([
      "resume",
    ]);
  });

  it("复工令**只**发给 resuming,别的状态一律没有这颗按钮", () => {
    for (const status of HAZARD_STATUSES) {
      if (status === "resuming") continue;
      expect(availableActions(hazard({ status }))).not.toContain("resume");
    }
  });

  it("closed / escalated / 词表外的状态 → 没有下一步", () => {
    expect(availableActions(hazard({ status: "closed" }))).toEqual([]);
    expect(availableActions(hazard({ status: "escalated" }))).toEqual([]);
    expect(availableActions(hazard({ status: "who_knows" }))).toEqual([]);
  });

  it("pending 的隐患不给任何签发按钮 —— 得先有人确认(D17)", () => {
    const actions = availableActions(hazard({ status: "pending", grade: GRADE_SEVERE }));
    expect(actions).not.toContain("notice");
    expect(actions).not.toContain("suspend");
  });

  it("pending → 定级 + 否决两颗(W10:确认与否决是同一个岔路口的两条)", () => {
    expect(availableActions(hazard({ status: "pending" }))).toEqual(["grade", "reject"]);
  });

  it("🔴 否决**只在 pending** —— 服务端硬拦「确认过的不许删」,别处给出来必挨骂", () => {
    // 这一条是「表只许更窄、永不更宽」那条红线在 W10 的落点:多给一档 = 一颗
    // 点下去必被 409 拒的按钮,而工友会以为是系统坏了。
    for (const status of HAZARD_STATUSES) {
      if (status === "pending") continue;
      expect(availableActions(hazard({ status }))).not.toContain("reject");
    }
    // 词表外的状态同样没有(default 分支一个动作都不给)
    expect(availableActions(hazard({ status: "who_knows" }))).not.toContain("reject");
  });

  it("未定级的 pending **要给否决** —— 误报恰恰最常落在这一档", () => {
    // safety 认出一个受控词表外的违规项,产出的就是 needs_grading=1 + pending。
    // 逼人先给一个「根本不是隐患」的东西定级才准删 = 往台账里留一条判过级的假隐患,
    // 而定级是要进法律文书的动作。服务端 delete_pending 只看 status='pending',
    // 压根不关心 needs_grading —— 所以给出来仍然比服务端窄。
    expect(availableActions(hazard({ status: "pending", needs_grading: true }))).toEqual([
      "grade",
      "reject",
    ]);
  });

  it("未定级但**已经确认过**的,否决不许出现 —— 服务端只让删 pending", () => {
    // 这一条守的是上一条的边界:放开 needs_grading 那一档时,很容易顺手写成
    // 「needs_grading 就给 reject」,那样 open/notified 上也会冒出一颗必被 409 拒的按钮。
    for (const status of HAZARD_STATUSES) {
      if (status === "pending") continue;
      expect(availableActions(hazard({ status, needs_grading: true }))).toEqual(["grade"]);
    }
  });
});

describe("二次确认(判据是「这一下是不是法律行为」,不是「会不会写库」)", () => {
  it("暂停令与上报要举手确认;其余不要 —— 每颗都弹的下场是人闭眼点确定", () => {
    expect(actionNeedsConfirm("suspend")).toBe(true);
    expect(actionNeedsConfirm("escalate")).toBe(true);
    expect(actionNeedsConfirm("notice")).toBe(false);
    expect(actionNeedsConfirm("grade")).toBe(false);
    expect(actionNeedsConfirm("reinspect")).toBe(false);
    expect(actionNeedsConfirm("resume")).toBe(false);
  });

  it("确认话里要有隐患编号、三份文书的名字、以及「不能撤销」", () => {
    const prompt = confirmPrompt("suspend", hazard({ grade: GRADE_SEVERE }));
    expect(prompt).toContain("GYT-H-20260816-093000-1a2b");
    expect(prompt).toContain("工程暂停令");
    expect(prompt).toContain("致建设单位报告");
    expect(prompt).toContain("不能撤销");
  });

  it("上报那句要说清是对施工单位的正式指控", () => {
    expect(confirmPrompt("escalate", hazard())).toContain("指控");
  });

  it("否决也要举手确认(W10)—— 它跟「人工定级」并排长着,点偏一格就没了一条隐患", () => {
    expect(actionNeedsConfirm("reject")).toBe(true);
  });

  it("否决那句要说清是**整行删掉、找不回来**,并给出「确实是隐患」的出路", () => {
    const prompt = confirmPrompt("reject", hazard());
    expect(prompt).toContain("GYT-H-20260816-093000-1a2b");
    expect(prompt).toContain("删掉");
    expect(prompt).toContain("找不回来");
    expect(prompt).toContain("确认"); // 误点进来的人得知道该走哪条
    // 这句话是拿去弹原生对话框的:markdown 记号会原样显示成星号
    expect(prompt).not.toContain("**");
  });

  it("只有通知单/暂停令要期限,只有复查要照片", () => {
    const needsDue = DISPOSAL_ACTIONS.filter(actionNeedsDuePhrase);
    const needsPhoto = DISPOSAL_ACTIONS.filter(actionNeedsPhoto);
    expect(needsDue).toEqual(["notice", "suspend"]);
    expect(needsPhoto).toEqual(["reinspect"]);
  });
});

describe("请求体拼装", () => {
  it("期限**原话原样**送后端,前端一行日期换算都不写", () => {
    // dates.py 用 314 行证明了中文日期不好算,红线是「只传原话,代码来算」。
    // 前端要是自作聪明换算成 2026-08-19,这条测试就红。
    const body = actionBody("notice", { hazardNo: "GYT-H-1", duePhrase: " 下周三 " });
    expect(body).toEqual({ hazard_no: "GYT-H-1", due_phrase: "下周三" });
  });

  it("🔴 期限空着就拒,绝不留空 —— due_date 为空的隐患永远不会被升级", () => {
    expect(() => actionBody("suspend", { hazardNo: "GYT-H-1" })).toThrow(SupervisionContractError);
    expect(() => actionBody("suspend", { hazardNo: "GYT-H-1", duePhrase: "  " })).toThrow(
      SUPERVISION_MESSAGES.missingDue,
    );
  });

  it("定级只收词表内的两个字", () => {
    expect(actionBody("grade", { hazardNo: "GYT-H-1", grade: GRADE_SEVERE })).toEqual({
      hazard_no: "GYT-H-1",
      grade: "严重",
    });
    expect(() => actionBody("grade", { hazardNo: "GYT-H-1", grade: "特别严重" })).toThrow(
      SUPERVISION_MESSAGES.badGrade,
    );
    expect(() => actionBody("grade", { hazardNo: "GYT-H-1" })).toThrow(SupervisionContractError);
  });

  it("复查必须挂 32 位照片编号 + 人给的结论;大写归一成小写", () => {
    const body = actionBody("reinspect", {
      hazardNo: "GYT-H-1",
      result: "pass",
      afterPhotoId: " 0123456789ABCDEF0123456789ABCDEF ",
    });
    expect(body).toEqual({
      hazard_no: "GYT-H-1",
      result: "pass",
      after_photo_id: "0123456789abcdef0123456789abcdef",
    });
  });

  it("🔴 照片编号不对就拒 —— 编号打错一位照样非空,而复查合格能把隐患销项", () => {
    expect(() =>
      actionBody("reinspect", { hazardNo: "GYT-H-1", result: "pass", afterPhotoId: "abc" }),
    ).toThrow(SUPERVISION_MESSAGES.badPhotoId);
    // 结论必须是人给的两个值之一,不许默认成 pass
    expect(() =>
      actionBody("reinspect", {
        hazardNo: "GYT-H-1",
        afterPhotoId: "0123456789abcdef0123456789abcdef",
      }),
    ).toThrow(SupervisionContractError);
  });

  it("复工令 / 上报 / 否决只要编号", () => {
    expect(actionBody("resume", { hazardNo: "GYT-H-1" })).toEqual({ hazard_no: "GYT-H-1" });
    expect(actionBody("escalate", { hazardNo: "GYT-H-1" })).toEqual({ hazard_no: "GYT-H-1" });
    expect(actionBody("reject", { hazardNo: " GYT-H-1 " })).toEqual({ hazard_no: "GYT-H-1" });
    expect(ACTION_ENDPOINT.reject).toBe("reject");
  });

  it("否决的回执:status 是 deleted、documents 空数组,严格解析器照样认", () => {
    // 它是唯一一个 status 不在八档里的回执 —— 解析器不许因此判成契约破了。
    const result = parseActionEnvelope(
      JSON.stringify({
        ok: true,
        data: { hazard_no: "GYT-H-1", status: "deleted", documents: [] },
        user_msg: "已把这条从台账里删掉。",
        error_code: null,
      }),
    );
    expect(result.status).toBe(REJECTED_STATUS);
    expect(result.documents).toEqual([]);
    // 拿它当状态渲染会在屏幕上冒出一个英文词 —— 正确用法是据此 removeHazard
    expect(hazardStatusZh(result.status)).toBe("deleted");
  });

  it("没有隐患编号一律拒(七个端点全靠它定位)", () => {
    for (const action of DISPOSAL_ACTIONS) {
      expect(() => actionBody(action as DisposalAction, { hazardNo: "   " })).toThrow(
        SupervisionContractError,
      );
    }
  });

  it("批量确认:去重保序、去空白;空的和超上限的都拒", () => {
    expect(confirmBody([" A ", "B", "A", ""])).toEqual({ hazard_nos: ["A", "B"] });
    expect(() => confirmBody([])).toThrow(SUPERVISION_MESSAGES.noSelection);
    const tooMany = Array.from({ length: MAX_CONFIRM_BATCH + 1 }, (_, i) => `GYT-H-${i}`);
    expect(() => confirmBody(tooMany)).toThrow(SupervisionContractError);
  });
});

describe("响应解析(严格那一份:人刚点了签发,契约破了必须响亮失败)", () => {
  it("三份文书按数组顺序回来:通知单 → 暂停令 → 致建设单位报告", () => {
    const result = parseActionEnvelope(SUSPEND_ENVELOPE);
    expect(result.hazard_no).toBe("GYT-H-20260816-093000-1a2b");
    expect(result.status).toBe("suspended");
    expect(result.documents.map((d) => d.doc_type)).toEqual([
      "notice",
      "suspension",
      "owner_report",
    ]);
    expect(result.documents[1].doc_no).toBe("GYT-ZT-20260816-093012-bb22");
    expect(result.user_msg).toBe("三份文书已出稿");
  });

  it("🔴 缺 documents 就抛 —— 「连数组都没有」是契约破了,不能当成「这次没出文书」", () => {
    const body = JSON.stringify({
      ok: true,
      data: { hazard_no: "GYT-H-1", status: "notified" },
      user_msg: "出了",
      error_code: null,
    });
    expect(() => parseActionEnvelope(body)).toThrow(SupervisionContractError);
  });

  it("不出文书的动作(定级/复查)documents 是空数组,照样解析得动", () => {
    const graded = parseActionEnvelope(
      JSON.stringify({
        ok: true,
        data: {
          hazard_no: "GYT-H-1",
          status: "pending",
          grade: "严重",
          needs_grading: false,
          documents: [],
        },
        user_msg: "已定为严重隐患",
        error_code: null,
      }),
    );
    expect(graded.documents).toEqual([]);
    expect(graded.grade).toBe("严重");
    expect(graded.needsGrading).toBe(false);

    const reinspected = parseActionEnvelope(
      JSON.stringify({
        ok: true,
        data: {
          hazard_no: "GYT-H-1",
          status: "resuming",
          documents: [],
          result: "pass",
          after_photo_id: "0123456789abcdef0123456789abcdef",
        },
        user_msg: "复查合格",
        error_code: null,
      }),
    );
    // 复查合格落 closed 还是 resuming 由 db 按 was_suspended 挑边,前端**照抄**不猜
    expect(reinspected.status).toBe("resuming");
    expect(reinspected.result).toBe("pass");
    expect(reinspected.documents).toEqual([]);
  });

  it("artifact_id 为空 → null(卡片改渲染一行说明,绝不出死链接);filename 有兜底", () => {
    const body = JSON.stringify({
      ok: true,
      data: {
        hazard_no: "GYT-H-1",
        status: "notified",
        documents: [{ doc_type: "notice", doc_no: "GYT-TZ-1", artifact_id: "" }],
      },
      user_msg: "",
      error_code: null,
    });
    const doc = parseActionEnvelope(body).documents[0];
    expect(doc.artifact_id).toBeNull();
    // 文书确实签发了(编号已进台账),只是取不了件 —— 丢掉整条的话人会以为没出
    expect(doc.filename).toBe("监理通知单_GYT-TZ-1.docx");
  });

  it("ok:false / 不是 JSON / data 不是对象 一律抛契约错", () => {
    expect(() => parseActionEnvelope('{"ok": false, "data": null}')).toThrow(
      SupervisionContractError,
    );
    expect(() => parseActionEnvelope("<html>502</html>")).toThrow(SupervisionContractError);
    expect(() => parseActionEnvelope('{"ok": true, "data": []}')).toThrow(SupervisionContractError);
  });

  it("批量确认:成功与失败都要拿到 —— 只报「已确认 3 条」人会以为全成了", () => {
    const result = parseConfirmEnvelope(
      JSON.stringify({
        ok: true,
        data: {
          confirmed: ["GYT-H-1", "GYT-H-2"],
          failed: [{ hazard_no: "GYT-H-3", reason: "这条现在是「已签发通知单」,不用再确认。" }],
        },
        user_msg: "已确认 2 条隐患",
        error_code: null,
      }),
    );
    expect(result.confirmed).toEqual(["GYT-H-1", "GYT-H-2"]);
    expect(result.failed).toEqual([
      { hazard_no: "GYT-H-3", reason: "这条现在是「已签发通知单」,不用再确认。" },
    ]);
    expect(result.user_msg).toBe("已确认 2 条隐患");
  });

  it("批量确认:failed 缺省当空;confirmed 不是数组就抛", () => {
    const result = parseConfirmEnvelope(
      JSON.stringify({ ok: true, data: { confirmed: ["A"] }, user_msg: "", error_code: null }),
    );
    expect(result.failed).toEqual([]);
    expect(() =>
      parseConfirmEnvelope(JSON.stringify({ ok: true, data: { confirmed: "A" } })),
    ).toThrow(SupervisionContractError);
  });
});

describe("错误归一化(前端不能假设所有错误都是 Envelope)", () => {
  it("🔴 Envelope 的 user_msg 优先 —— 404 有两种来源,先看话再看码", () => {
    // 「没找到隐患编号」被说成「监理接口还没开通」的话,人会跑去查部署,
    // 而真相只是编号打错了一位。
    const out = normalizeError(
      404,
      "application/json",
      JSON.stringify({
        ok: false,
        data: null,
        user_msg: "没找到隐患「GYT-H-x」,核对一下编号。",
        error_code: "NOT_FOUND",
      }),
    );
    expect(out.message).toBe("没找到隐患「GYT-H-x」,核对一下编号。");
    expect(out.errorCode).toBe("NOT_FOUND");
  });

  it("三条硬拦那几句(409)原样上屏,不重新包装成「操作失败」", () => {
    const out = normalizeError(
      409,
      "application/json",
      JSON.stringify({
        ok: false,
        data: null,
        user_msg: "隐患「GYT-H-1」是严重隐患,只发一份通知单不够,要走「签发暂停令」。",
        error_code: "CONFLICT",
      }),
    );
    expect(out.message).toContain("要走「签发暂停令」");
  });

  it("{detail:…} 这种英文内部话不上屏,按状态码给固定中文", () => {
    const out = normalizeError(401, "application/json", '{"detail": "Invalid token"}');
    expect(out.message).toBe(SUPERVISION_MESSAGES.authFailed);
    expect(out.message).not.toContain("token");
  });

  it("Caddy 的非 JSON 错误页按状态码说话;500 说人话", () => {
    expect(normalizeError(502, "text/html", "<html>bad gateway</html>").message).toBe(
      SUPERVISION_MESSAGES.serverError,
    );
    expect(normalizeError(404, "text/html", "not found").message).toBe(
      SUPERVISION_MESSAGES.notFound,
    );
  });

  it("网络层异常(status=0)说「连不上服务器」,不许让 Failed to fetch 上屏", () => {
    const out = normalizeError(NETWORK_ERROR_STATUS, null, "");
    expect(out.message).toBe(SUPERVISION_MESSAGES.network);
    expect(out.message).not.toMatch(/[A-Za-z]{4,}/);
  });

  it("content-type 撒谎时以「解析得动吗」为准", () => {
    const out = normalizeError(409, "text/plain", '{"user_msg": "状态刚被改过"}');
    expect(out.message).toBe("状态刚被改过");
  });
});

describe("工具结果里捞数据(渲染路径:认不出就跳过,一个错都不抛)", () => {
  const toolData = {
    label: "violation",
    violations: ["未戴安全帽", "临边无防护"],
    hazards: [
      {
        hazard_no: "GYT-H-1",
        item: "未戴安全帽",
        grade: "一般",
        status: "pending",
        needs_grading: false,
      },
      {
        hazard_no: "GYT-H-2",
        item: "临边无防护",
        grade: "严重",
        status: "pending",
        needs_grading: false,
      },
    ],
    failed_items: ["消防通道堵塞"],
  };

  it("analyze_site_photo 的五键形状照单全收", () => {
    const list = hazardsFromToolData(toolData);
    expect(list).toHaveLength(2);
    expect(list[1]).toEqual({
      hazard_no: "GYT-H-2",
      item: "临边无防护",
      grade: "严重",
      status: "pending",
      needs_grading: false,
    });
  });

  it("登记失败的项要捞出来 —— 界面不显示的话,一条真实隐患就这么没了", () => {
    expect(failedItemsFromToolData(toolData)).toEqual(["消防通道堵塞"]);
  });

  it("没有 hazards / 不是对象 / null 一律空数组,不抛", () => {
    expect(hazardsFromToolData({ label: "compliant" })).toEqual([]);
    expect(hazardsFromToolData(null)).toEqual([]);
    expect(hazardsFromToolData("怎么会是字符串")).toEqual([]);
    expect(failedItemsFromToolData(undefined)).toEqual([]);
  });

  it("没有编号的条目跳过(那种条目在界面上什么也做不了);其余字段有保守缺省", () => {
    const list = hazardsFromToolData({
      hazards: [{ item: "没有编号" }, { hazard_no: "GYT-H-9" }, "不是对象"],
    });
    expect(list).toHaveLength(1);
    // 状态缺省成 pending —— 权限最小的那一档,猜错也不会让人点到不该点的按钮
    expect(list[0]).toEqual({
      hazard_no: "GYT-H-9",
      item: "(未写明事项)",
      grade: "",
      status: "pending",
      needs_grading: false,
    });
  });

  it("needs_grading 只认真布尔 true(字符串 \"false\" 不许被当成真)", () => {
    expect(hazardsFromToolData({ hazards: [{ hazard_no: "A", needs_grading: "false" }] })[0]
      .needs_grading).toBe(false);
    expect(hazardsFromToolData({ hazards: [{ hazard_no: "A", needs_grading: true }] })[0]
      .needs_grading).toBe(true);
  });

  it("HAZARD_SOURCE_TOOLS 里必须有 analyze_site_photo —— 少了它整条入口就没了", () => {
    expect(HAZARD_SOURCE_TOOLS).toContain("analyze_site_photo");
    // list_hazards / get_hazard / suggest_disposal 是 supervision Agent 的只读三件
    // (backend/src/gyt/agents/supervision/tools.py 的 @tool 名字,已核对)
    expect(HAZARD_SOURCE_TOOLS).toContain("list_hazards");
    expect(HAZARD_SOURCE_TOOLS).toContain("get_hazard");
  });

  it("get_hazard 那种「data 自己就是一条隐患」的形状也要认 —— 它是处置老隐患的唯一入口", () => {
    // 后端 _hazard_item 是 ** 展开进 data 的,没有 hazards 数组
    const list = hazardsFromToolData({
      hazard_no: "GYT-H-20260801-080000-9999",
      item: "临边无防护",
      grade: "严重",
      status: "notified",
      status_display: "已签发通知单",
      due_date: "2026-08-20",
      overdue: false,
      needs_grading: false,
      documents: [],
    });
    expect(list).toHaveLength(1);
    expect(list[0].status).toBe("notified");
    // 而且它拿得到复查那颗按钮 —— 否则「老隐患没法复查」这条路就是死的
    expect(availableActions(list[0])).toContain("reinspect");
  });

  it("宽松版 documentsFromToolData 永不抛(与严格版 parseActionEnvelope 的对照)", () => {
    const docs = documentsFromToolData({
      documents: [
        { doc_type: "notice", doc_no: "GYT-TZ-1", artifact_id: "abc" },
        { doc_no: "缺类型" },
        null,
        "不是对象",
      ],
    });
    expect(docs).toHaveLength(1);
    expect(docs[0].filename).toBe("监理通知单_GYT-TZ-1.docx");
    // 同一份坏数据交给严格版必须抛 —— 两条路的代价不对称,严格度也不对称
    expect(() =>
      parseActionEnvelope(
        JSON.stringify({
          ok: true,
          data: { hazard_no: "A", status: "notified", documents: [{ doc_no: "缺类型" }] },
        }),
      ),
    ).toThrow(SupervisionContractError);
    expect(documentsFromToolData({ report_no: "GYT-20260816-093000" })).toEqual([]);
  });
});

describe("清单操作(全部不可变 —— React 里改原数组等于界面不刷新)", () => {
  const list: HazardBrief[] = [
    hazard({ hazard_no: "A" }),
    hazard({ hazard_no: "B", status: "open" }),
  ];

  it("patchHazard 只改中的那条,原数组一个字节不动", () => {
    const next = patchHazard(list, "A", { status: "open" });
    expect(next[0].status).toBe("open");
    expect(list[0].status).toBe("pending");
    expect(next).not.toBe(list);
  });

  it("patchHazard 找不到编号就原样返回(并发把它删了也不崩)", () => {
    expect(patchHazard(list, "不存在", { status: "closed" })).toEqual(list);
  });

  it("removeHazard / pendingHazards / toggleSelected 都返回新数组", () => {
    expect(removeHazard(list, "A").map((h) => h.hazard_no)).toEqual(["B"]);
    expect(pendingHazards(list).map((h) => h.hazard_no)).toEqual(["A"]);
    const selected = toggleSelected([], "A");
    expect(selected).toEqual(["A"]);
    expect(toggleSelected(selected, "A")).toEqual([]);
    // 点选顺序即请求体顺序,不排序
    expect(toggleSelected(["B"], "A")).toEqual(["B", "A"]);
  });

  it("mergeHazards:按编号去重、后来的赢、**顺序不变**(面板开着时按钮不许跳位)", () => {
    const merged = mergeHazards(list, [
      hazard({ hazard_no: "B", status: "notified" }),
      hazard({ hazard_no: "C" }),
    ]);
    expect(merged.map((h) => h.hazard_no)).toEqual(["A", "B", "C"]);
    expect(merged[1].status).toBe("notified");
    expect(list).toHaveLength(2);
  });
});

describe("文书取件地址与话术", () => {
  it("按编号取件:${base}/by-id/<artifact_id>(与 human.tsx 同一条已验证路径)", () => {
    expect(
      documentUrl({ artifact_id: "0123456789abcdef0123456789abcdef" }, "https://x.example/artifacts/"),
    ).toBe("https://x.example/artifacts/by-id/0123456789abcdef0123456789abcdef");
  });

  it("🔴 没有 artifact_id 就返回 null —— 死链接和「文件真没了」在界面上分不开", () => {
    expect(documentUrl({ artifact_id: null }, "http://127.0.0.1:8788")).toBeNull();
  });

  it("🔴 复查记录不是文书:它本来就没有文件,界面上不许跟着喊「下不了」", () => {
    // get_hazard 的证据链里会带一行 doc_type=reinspect、artifact_id=None。
    // 两种「没有 artifact_id」必须分开说 —— 混成一句的话,真出事的那种
    //(文书签了却取不了件)会被这种噪声淹掉。
    expect(REINSPECT_DOC_TYPE).toBe("reinspect");
    expect(isDownloadableDoc({ doc_type: REINSPECT_DOC_TYPE })).toBe(false);
    for (const docType of ["notice", "suspension", "resumption", "owner_report", "authority_report"]) {
      expect(isDownloadableDoc({ doc_type: docType })).toBe(true);
    }
  });

  it("describeDocuments 说清出了几份、都是什么;0 份时给空串", () => {
    const docs = parseActionEnvelope(SUSPEND_ENVELOPE).documents;
    expect(describeDocuments(docs)).toBe("已出稿 3 份:监理通知单、工程暂停令、致建设单位报告");
    expect(describeDocuments([])).toBe("");
  });
});

// ---------------------------------------------------------------------------
// W10:两个 GET 查询端点。这两条路**面板每次打开、每次刷新都在走** ——
// 抛出去就是整个操作台白屏,而白屏之后连「重试」按钮都没有。所以下面每一条
// 断言的另一面都是「它没抛」。
// ---------------------------------------------------------------------------

/** 一屏在办清单(契约里那个例子的形状,补了第二行好让扩展字段有东西可断)。 */
const LIST_ENVELOPE = JSON.stringify({
  ok: true,
  data: {
    scope: "在办",
    project_id: null,
    today: "2026-08-16",
    hazards: [
      {
        hazard_no: "GYT-H-20260816-171404-7194",
        item: "未穿反光衣",
        grade: "一般",
        severity: "一般",
        status: "pending",
        status_display: "待确认",
        due_date: null,
        due_display: null,
        overdue: false,
        needs_grading: false,
        project_id: "",
      },
      {
        hazard_no: "GYT-H-20260810-081500-3311",
        item: "临边无防护",
        grade: "严重",
        severity: "重大",
        status: "notified",
        status_display: "已签发通知单",
        due_date: "2026-08-12",
        due_display: "8月12日",
        overdue: true,
        needs_grading: false,
        project_id: "P-tko",
      },
    ],
    total: 12,
    pending: 3,
    overdue: 2,
    unassigned: 4,
    truncated: false,
  },
  user_msg: "在办隐患 12 条:待确认 3 条、超期 2 条。",
  error_code: null,
});

describe("清单端点解析(读不出来 ≠ 台账里没有,这两句在界面上长得一模一样)", () => {
  it("正常一屏:行、计数、今天、回声全拿得到,扩展字段也在", () => {
    const result = parseHazardListEnvelope(LIST_ENVELOPE);
    expect(result.ok).toBe(true);
    expect(result.scope).toBe(HAZARD_SCOPE_ACTIVE);
    expect(result.today).toBe("2026-08-16");
    expect(result.hazards.map((h) => h.hazard_no)).toEqual([
      "GYT-H-20260816-171404-7194",
      "GYT-H-20260810-081500-3311",
    ]);
    expect(result.total).toBe(12);
    expect(result.pending).toBe(3);
    expect(result.overdue).toBe(2);
    expect(result.userMsg).toBe("在办隐患 12 条:待确认 3 条、超期 2 条。");

    const second = result.hazards[1];
    expect(second.due_date).toBe("2026-08-12");
    expect(second.due_display).toBe("8月12日");
    expect(second.overdue).toBe(true);
    expect(second.status_display).toBe("已签发通知单");
    expect(second.project_id).toBe("P-tko");
    // 🔴 界面上念的是 severity(现场判的那档)而不是 grade(只有一般/严重两档)
    expect(second.severity).toBe("重大");
    expect(second.grade).toBe(GRADE_SEVERE);
    // 老路径(工具结果)拿不到这几个字段,所以它们必须是**可选**的:
    // 缺了就是 undefined,渲染层显示「—」,而不是硬填一个 false 冒充「没超期」。
    expect(hazardsFromToolData({ hazards: [{ hazard_no: "A" }] })[0].overdue).toBeUndefined();
  });

  it("🔴 未归属那条:project_id 是空串,**不许被压成 undefined/null**(D6)", () => {
    // 压掉的话「没人认领的一批」在界面上就变成了「不知道归属」,而它是要人去认领的。
    const first = parseHazardListEnvelope(LIST_ENVELOPE).hazards[0];
    expect(first.project_id).toBe("");
    expect(first).toHaveProperty("project_id");
  });

  it("🔴 未归属计数不过 scope 筛子 —— 它回答的是「有没有一批隐患没人看得见」", () => {
    const result = parseHazardListEnvelope(LIST_ENVELOPE);
    // 这一屏只有 2 行、其中 1 行未归属,而 unassigned 是 4:两个数本来就不该相等,
    // 谁要是「顺手修正」成按行数算,那批筛不出来的隐患就再也没人提起了。
    expect(result.unassigned).toBe(4);
    expect(result.hazards).toHaveLength(2);
  });

  it("🔴 归属回声三态分得开:null=全部 / 空串=未归属 / 具体工地", () => {
    const echo = (projectId: unknown): string | null =>
      parseHazardListEnvelope(
        JSON.stringify({ ok: true, data: { hazards: [], project_id: projectId } }),
      ).projectId;
    expect(echo(null)).toBeNull();
    expect(echo("")).toBe(""); // 用 `textOf(...) || null` 写就会在这儿红
    expect(echo("P-tko")).toBe("P-tko");
    // 键压根没有 = 没筛工地,同 null
    expect(parseHazardListEnvelope(JSON.stringify({ ok: true, data: { hazards: [] } })).projectId)
      .toBeNull();
  });

  it("🔴 truncated 要透传 —— 不显示的话,监理看着一张完整的清单说「就剩这些了」", () => {
    const body = JSON.stringify({
      ok: true,
      data: { hazards: [], total: 137, truncated: true },
      user_msg: "太多了,只显示前 50 条。",
    });
    expect(parseHazardListEnvelope(body).truncated).toBe(true);
    // 缺省不喊狼来了;而且只认真布尔(字符串 "false" 是真值,松一点就次次报截断)
    expect(parseHazardListEnvelope(LIST_ENVELOPE).truncated).toBe(false);
    expect(
      parseHazardListEnvelope(JSON.stringify({ ok: true, data: { hazards: [], truncated: "false" } }))
        .truncated,
    ).toBe(false);
  });

  it("ok:false:退化成空清单,但后端那句人话留着(它是屏幕上唯一的线索)", () => {
    const result = parseHazardListEnvelope(
      JSON.stringify({
        ok: false,
        data: null,
        user_msg: "查隐患只能按这几种来:在办、待确认、超期、全部。",
        error_code: "INVALID_INPUT",
      }),
    );
    expect(result.ok).toBe(false);
    expect(result.hazards).toEqual([]);
    expect(result.userMsg).toContain("在办、待确认、超期、全部");
  });

  it("🔴 hazards 不是数组 → ok:false,**不许说成「台账里没有隐患」**", () => {
    const result = parseHazardListEnvelope(
      JSON.stringify({ ok: true, data: { hazards: "怎么会是字符串", total: 12 } }),
    );
    expect(result.ok).toBe(false);
    expect(result.hazards).toEqual([]);
  });

  it("不是 JSON / data 不是对象 / 空串:一个错都不抛,一律「读不出来」", () => {
    for (const body of ["<html>502 Bad Gateway</html>", "", '{"ok": true, "data": []}', "null"]) {
      const result = parseHazardListEnvelope(body);
      expect(result.ok).toBe(false);
      expect(result.hazards).toEqual([]);
      expect(result.truncated).toBe(false);
    }
  });

  it("缺编号的条目跳过,其余照常出 —— 半条隐患在界面上什么都做不了", () => {
    const result = parseHazardListEnvelope(
      JSON.stringify({
        ok: true,
        data: { hazards: [{ item: "没有编号" }, null, "不是对象", { hazard_no: "GYT-H-9" }] },
      }),
    );
    expect(result.ok).toBe(true);
    expect(result.hazards.map((h) => h.hazard_no)).toEqual(["GYT-H-9"]);
    // 跳掉的那几条不算进 total —— total 兜底成**手上真有的行数**,不编数
    expect(result.total).toBe(1);
  });

  it("计数拿不到就报 0(不编数);total 兜底成手上真有的行数(不自相矛盾)", () => {
    const result = parseHazardListEnvelope(
      JSON.stringify({
        ok: true,
        data: { hazards: [{ hazard_no: "A" }, { hazard_no: "B" }], pending: "三", overdue: -1 },
      }),
    );
    // 「不知道有几条」只能说 0:凭空编一个数就是吓唬人 / 骗人
    expect(result.pending).toBe(0);
    expect(result.overdue).toBe(0);
    expect(result.unassigned).toBe(0);
    // 清单里明明列着 2 条而表头写「共 0 条」,人会以为界面坏了
    expect(result.total).toBe(2);
  });

  it("scope 缺了按「在办」算 —— 与后端缺省同一档", () => {
    expect(parseHazardListEnvelope(JSON.stringify({ ok: true, data: { hazards: [] } })).scope).toBe(
      HAZARD_SCOPE_ACTIVE,
    );
  });
});

/** 一条走到「复查不合格」的隐患:两行证据链(一份通知单 + 一条复查记录)。 */
const DETAIL_ENVELOPE = JSON.stringify({
  ok: true,
  data: {
    hazard_no: "GYT-H-20260810-081500-3311",
    item: "临边无防护",
    grade: "严重",
    severity: "重大",
    status: "reinspect_failed",
    status_display: "复查不合格",
    due_date: "2026-08-12",
    due_display: "8月12日",
    overdue: true,
    needs_grading: false,
    project_id: "P-tko",
    // 首次发现那张照片。**本层不读它** —— 放在这儿正是为了钉住下面那条断言:
    // 证据链里复查行的 photo_id 绝不能串成这一张。
    photo_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    found_at: "2026-08-10T08:15:00+08:00",
    closed_at: null,
    reinspected: true,
    documents: [
      {
        doc_type: "notice",
        doc_type_display: "监理通知单",
        doc_no: "GYT-TZ-20260810-081600-aa11",
        artifact_id: "0123456789abcdef0123456789abcdef",
        filename: "监理通知单_GYT-TZ-20260810-081600-aa11.docx",
        photo_id: null,
        result: null,
        result_display: null,
        created_at: "2026-08-10T08:16:00+08:00",
      },
      {
        // 复查记录行:doc_no 长相刻意不像文书编号(supervision_api._REINSPECT_NO_MARK)
        doc_type: "reinspect",
        doc_type_display: "复查记录",
        doc_no: "GYT-H-20260810-081500-3311#FC-a1b2c3d4",
        artifact_id: null,
        filename: null,
        photo_id: "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        result: "fail",
        result_display: "不合格",
        created_at: "2026-08-13T09:00:00+08:00",
      },
    ],
  },
  user_msg: "这条 8月12日 到期,8月13日 复查过一次,不合格。",
  error_code: null,
});

describe("详情端点解析(证据链:哪张照片算数,是整条链的要害)", () => {
  it("正常一条:详情字段 + 两行证据链,顺序照抄后端", () => {
    const { ok, hazard } = parseHazardDetailEnvelope(DETAIL_ENVELOPE);
    expect(ok).toBe(true);
    expect(hazard).not.toBeNull();
    expect(hazard!.hazard_no).toBe("GYT-H-20260810-081500-3311");
    expect(hazard!.status).toBe("reinspect_failed");
    expect(hazard!.severity).toBe("重大");
    expect(hazard!.overdue).toBe(true);
    expect(hazard!.reinspected).toBe(true);
    expect(hazard!.foundAt).toBe("2026-08-10T08:15:00+08:00");
    expect(hazard!.closedAt).toBeNull(); // 还没销项
    expect(hazard!.documents.map((d) => d.doc_type)).toEqual(["notice", "reinspect"]);
    // 这一档能做的两件事:再复查一次,或者上报主管部门
    expect(availableActions(hazard!)).toEqual(["reinspect", "escalate"]);
  });

  it("🔴 复查记录行的 photo_id 是**这一次复查**那张,不是首次发现那张", () => {
    // 串了的后果:拿发现时的照片当「整改后」的证据摆进证据链 —— 界面上一切正常,
    // 而复查合格恰恰是靠它把隐患销项的。
    const docs = parseHazardDetailEnvelope(DETAIL_ENVELOPE).hazard!.documents;
    expect(docs[1].photo_id).toBe("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
    expect(docs[1].photo_id).not.toBe("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"); // 隐患那张
    expect(docs[1].result).toBe("fail");
    expect(docs[1].result_display).toBe("不合格"); // 英文枚举值不许上屏
    // 文书行没有这几个键(后端给的是 null)—— 别在文书卡片上画复查结论
    expect(docs[0].result).toBeUndefined();
    expect(docs[0].photo_id).toBeNull();
  });

  it("🔴 复查记录行不是文书:artifact_id 为 null,而它**本来就没有文件**", () => {
    const docs = parseHazardDetailEnvelope(DETAIL_ENVELOPE).hazard!.documents;
    expect(docs[1].doc_type).toBe(REINSPECT_DOC_TYPE);
    expect(docs[1].artifact_id).toBeNull();
    expect(isDownloadableDoc(docs[1])).toBe(false);
    // filename 是兜底拼出来的,**不是真文件名** —— 它长得像个 .docx,靠
    // isDownloadableDoc 挡着才不会被画成下载按钮。两种「没有 artifact_id」
    // 必须分得开:真出事的那种(文书签了却取不了件)不能被这种噪声淹掉。
    expect(docs[1].filename).toBe("复查记录_GYT-H-20260810-081500-3311#FC-a1b2c3d4.docx");
    expect(docs[0].artifact_id).toBe("0123456789abcdef0123456789abcdef");
    expect(isDownloadableDoc(docs[0])).toBe(true);
    expect(docs[0].doc_type_display).toBe("监理通知单");
  });

  it("404:hazard 为 null,而「核对一下编号」那句留着(不许说成「接口没开通」)", () => {
    const result = parseHazardDetailEnvelope(
      JSON.stringify({
        ok: false,
        data: null,
        user_msg: "没找到隐患「GYT-H-x」,核对一下编号。",
        error_code: "NOT_FOUND",
      }),
    );
    expect(result.ok).toBe(false);
    expect(result.hazard).toBeNull();
    expect(result.userMsg).toBe("没找到隐患「GYT-H-x」,核对一下编号。");
  });

  it("ok:true 但没有 hazard_no → 也当失败:每颗按钮都不知道该打给谁", () => {
    const result = parseHazardDetailEnvelope(
      JSON.stringify({ ok: true, data: { item: "临边无防护", status: "open" } }),
    );
    expect(result.ok).toBe(false);
    expect(result.hazard).toBeNull();
  });

  it("不是 JSON / data 不是对象:一个错都不抛", () => {
    for (const body of ["<html>502</html>", "", '{"ok": true, "data": []}']) {
      expect(() => parseHazardDetailEnvelope(body)).not.toThrow();
      expect(parseHazardDetailEnvelope(body).hazard).toBeNull();
    }
  });

  it("证据链里的坏行跳过,不因为一行坏数据把整条隐患丢掉", () => {
    const result = parseHazardDetailEnvelope(
      JSON.stringify({
        ok: true,
        data: {
          hazard_no: "GYT-H-1",
          documents: [{ doc_no: "缺类型" }, null, { doc_type: "notice", doc_no: "GYT-TZ-1" }],
        },
      }),
    );
    expect(result.ok).toBe(true);
    expect(result.hazard!.documents).toHaveLength(1);
    // documents 整个缺了也不算失败:pending 的隐患本来就一份文书都没有
    const bare = parseHazardDetailEnvelope(
      JSON.stringify({ ok: true, data: { hazard_no: "GYT-H-1" } }),
    );
    expect(bare.ok).toBe(true);
    expect(bare.hazard!.documents).toEqual([]);
    expect(bare.hazard!.reinspected).toBe(false);
    expect(bare.hazard!.status).toBe("pending"); // 状态缺省成权限最小的那一档
  });
});

describe("证据链的排版素材(W10 · 详情面板)", () => {
  const 文书 = (doc_no: string) => ({ doc_type: "notice", doc_no, artifact_id: "a".repeat(32) });
  const 复查 = (doc_no: string, result: "pass" | "fail") => ({
    doc_type: REINSPECT_DOC_TYPE,
    doc_no,
    artifact_id: null,
    result,
  });

  it("只给复查行编号,文书行恒为 null", () => {
    const rows = evidenceRows([文书("GYT-TZ-1"), 复查("R1", "fail"), 复查("R2", "pass")]);
    expect(rows.map((r) => r.reinspectionNo)).toEqual([null, 1, 2]);
  });

  it("🔴 顺序原样保留,不按类型分组 —— 分组等于把举证的时间轴拆了", () => {
    // 上报主管部门那条举证链是「通知过 + 期限到了 + 复查过 + 仍未整改」,
    // 分成「文书一堆、复查一堆」之后,答不了「先通知还是先复查」。
    const 链 = [复查("R1", "fail"), 文书("GYT-TZ-1"), 复查("R2", "fail")];
    expect(evidenceRows(链).map((r) => r.doc.doc_no)).toEqual(["R1", "GYT-TZ-1", "R2"]);
    expect(evidenceRows(链).map((r) => r.reinspectionNo)).toEqual([1, null, 2]);
  });

  it("🔴 artifact_id 为空的通知单仍算文书行,不许被编进复查序号", () => {
    // 那是**事故**(纸签了取不了件),要走文书那张卡显眼地说 ——
    // 被当成复查行的话,它会顶掉一个真复查的序号,屏幕上「第 2 次复查」其实是第 1 次。
    const 取不了件的通知单 = { doc_type: "notice", doc_no: "GYT-TZ-坏", artifact_id: null };
    const rows = evidenceRows([取不了件的通知单, 复查("R1", "pass")]);
    expect(rows[0].reinspectionNo).toBeNull();
    expect(rows[1].reinspectionNo).toBe(1);
  });

  it("空链返回空数组,不抛 —— 它在渲染路径上", () => {
    expect(evidenceRows([])).toEqual([]);
  });
});

describe("时刻排版(🔴 禁止任何时区换算)", () => {
  it("只截到分,秒丢掉", () => {
    expect(formatHkMoment("2026-08-16T09:50:08+08:00")).toBe("2026-08-16 09:50");
  });

  it("🔴 偏移量原样忽略,绝不按浏览器时区重排", () => {
    // 后端串里的偏移已经是香港时间快照(全仓唯一时间权威 = attendance/receipt.py)。
    // 写成 new Date(iso).toLocaleString() 的话,监理的笔记本设成 UTC 时,
    // 一条 09:50 发现的隐患在屏幕上会变成 01:50 —— 而这是要拿去追责的时刻,
    // 差八小时能把「下班后违规作业」说成「上班前」。
    // 所以同样的墙上时间配不同偏移,输出必须一模一样。
    const 香港 = formatHkMoment("2026-08-16T09:50:08+08:00");
    const 零时区 = formatHkMoment("2026-08-16T09:50:08+00:00");
    const 无偏移 = formatHkMoment("2026-08-16T09:50:08");
    expect(new Set([香港, 零时区, 无偏移]).size).toBe(1);
    expect(香港).toBe("2026-08-16 09:50");
  });

  it("认不出形状回空串,让调用方整格不显示", () => {
    // 宁可少一格,也不许把半截串或者 Invalid Date 摆到屏幕上。
    for (const 坏的 of ["", "  ", "昨天", "2026-08-16", "2026/08/16 09:50", "T09:50"]) {
      expect(formatHkMoment(坏的)).toBe("");
    }
  });

  it("首尾空白不影响", () => {
    expect(formatHkMoment("  2026-08-16T09:50:08+08:00 ")).toBe("2026-08-16 09:50");
  });
});

describe("patchHazard 与 status_display(2026-08-16 手工验当场抓到的那条)", () => {
  const 待确认 = (): HazardBrief => ({
    hazard_no: "GYT-H-1",
    item: "临边无防护",
    grade: GRADE_SEVERE,
    status: "pending",
    status_display: "待确认",
    needs_grading: false,
  });

  it("🔴 改了 status,旧的 status_display 必须被丢掉", () => {
    // 不丢的下场(实测):确认之后按钮已经变成「签发暂停令」、证据链也写着
    // 「已确认待处置」,而行首徽章还写着「待确认」—— 工友会以为没点成,再点一次,
    // 而下一颗按钮是签发法律文书。
    const [row] = patchHazard([待确认()], "GYT-H-1", { status: "open" });
    expect(row.status).toBe("open");
    expect(row.status_display).toBeUndefined();
    // 丢掉之后渲染回落到镜像词表 —— 同一份真相,不是现造第二份
    expect(hazardStatusZh(row.status)).toBe("已确认待处置");
  });

  it("patch 自己带了新的 status_display 就用它(后端说了算)", () => {
    const [row] = patchHazard([待确认()], "GYT-H-1", {
      status: "open",
      status_display: "已确认待处置",
    });
    expect(row.status_display).toBe("已确认待处置");
  });

  it("没改 status 的 patch 不许动 status_display", () => {
    const [row] = patchHazard([待确认()], "GYT-H-1", { grade: GRADE_NORMAL });
    expect(row.status_display).toBe("待确认");
    expect(row.grade).toBe(GRADE_NORMAL);
  });

  it("不可变:原数组与原对象一个字节没动", () => {
    const 原 = [待确认()];
    const 快照 = JSON.stringify(原);
    patchHazard(原, "GYT-H-1", { status: "open" });
    expect(JSON.stringify(原)).toBe(快照);
  });

  it("编号对不上就原样返回,不误伤别人的 status_display", () => {
    const [row] = patchHazard([待确认()], "GYT-H-别人", { status: "open" });
    expect(row.status).toBe("pending");
    expect(row.status_display).toBe("待确认");
  });
});
