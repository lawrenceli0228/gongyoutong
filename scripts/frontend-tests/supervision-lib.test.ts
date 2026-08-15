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
  HAZARD_SOURCE_TOOLS,
  HAZARD_STATUS_ZH,
  HAZARD_STATUSES,
  HazardBrief,
  hazardsFromToolData,
  hazardStatusZh,
  isDownloadableDoc,
  MAX_CONFIRM_BATCH,
  mergeHazards,
  NETWORK_ERROR_STATUS,
  normalizeError,
  parseActionEnvelope,
  parseConfirmEnvelope,
  patchHazard,
  pendingHazards,
  REINSPECT_DOC_TYPE,
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
  it("七条端点齐全,与 supervision_api.SUPERVISION_ROUTES 一一对应", () => {
    expect([...SUPERVISION_ENDPOINTS]).toEqual([
      "confirm",
      "grade",
      "notice",
      "suspend",
      "reinspect-result",
      "resume",
      "escalate",
    ]);
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
  it("needs_grading=1 → 只剩定级一件事(硬拦③:任何签发都会被服务端拒)", () => {
    for (const status of ["pending", "open"] as const) {
      for (const grade of HAZARD_GRADES) {
        const actions = availableActions(hazard({ status, grade, needs_grading: true }));
        expect(actions).toEqual(["grade"]);
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

  it("复工令 / 上报只要编号", () => {
    expect(actionBody("resume", { hazardNo: "GYT-H-1" })).toEqual({ hazard_no: "GYT-H-1" });
    expect(actionBody("escalate", { hazardNo: "GYT-H-1" })).toEqual({ hazard_no: "GYT-H-1" });
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
