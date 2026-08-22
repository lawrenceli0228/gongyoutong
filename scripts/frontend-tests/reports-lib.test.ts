/**
 * `reports-lib.ts`(巡检记录抽屉的纯逻辑)的单元测试。
 *
 * 它守的是「拍照 → 自动出 Word」这条链**唯一的出口**。这条链此前是断的:
 * 文档真的生成了、真的落盘了,而那份 Envelope 被 supervisor 的
 * `output_mode="last_message"` 整个丢掉,于是界面上没有任何地方能拿到它,
 * 而提示词教模型说「跟管理员说编号就行」——**那个管理员不存在**。
 *
 * 这份要钉死的静默错误:
 *   · **编造编号** —— 抠不出编号时凑一个。一个长得像编号、却对不上任何文档的串,
 *     会被人报给别人、写进留档,而事后谁也查不到那份文件。
 *   · **「读不出来」被说成「还没有记录」** —— 两句在界面上长得一模一样,
 *     而前一句会让人以为自己那份记录丢了,然后重拍一遍。
 *   · **时间用错时区** —— 编号里那个时刻是**香港时间**,sidecar 的 `created_at`
 *     是 **UTC**,差 8 小时。渲染错的表现是「明明下午三点出的记录,列表上写着上午七点」。
 *   · **`size_bytes` 读不出时给 0** —— 0 会被读成「这是个空文件」。
 */

import { describe, expect, it } from "vitest";

import {
  formatReportTime,
  formatSize,
  parseReportsEnvelope,
  reportDownloadUrl,
  reportTitle,
  reportsUrl,
} from "@/lib/reports-lib";

const API = "http://127.0.0.1:2024";
const ART = "http://127.0.0.1:8788";

function envelope(data: unknown, ok = true, userMsg = ""): string {
  return JSON.stringify({ ok, data, user_msg: userMsg, error_code: null });
}

function reportRow(overrides: Record<string, unknown> = {}) {
  return {
    artifact_id: "a".repeat(32),
    report_no: "GYT-20260822-153012",
    filename: "巡检记录_GYT-20260822-153012.docx",
    size_bytes: 20480,
    created_at: "2026-08-22T07:30:12+00:00",
    ...overrides,
  };
}

describe("地址", () => {
  it("不传 limit 就不写这个键 —— 让后端用它自己的缺省", () => {
    // 在前端复制一份缺省的表现是「我明明改了后端却没变」。
    expect(reportsUrl(API)).toBe("http://127.0.0.1:2024/reports");
  });

  it("传了 limit 就写上", () => {
    expect(reportsUrl(API, { limit: 5 })).toBe("http://127.0.0.1:2024/reports?limit=5");
  });

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    "limit 是 %s 这种不合理的值时当没传",
    (limit) => {
      expect(reportsUrl(API, { limit })).toBe("http://127.0.0.1:2024/reports");
    },
  );

  it("apiBase 尾部的斜杠不许拼出双斜杠", () => {
    expect(reportsUrl(`${API}/`)).toBe("http://127.0.0.1:2024/reports");
  });

  it("下载地址走 /by-id/ 而不是拼日期目录", () => {
    // 🔴 产物在盘上带**日期段**(UTC 的 YYYYMMDD),前端既拿不到它、也不能按本地
    //    当天日期去猜:晚上演示时 UTC 已经是"明天",猜必错。
    expect(reportDownloadUrl(ART, "b".repeat(32))).toBe(
      `http://127.0.0.1:8788/by-id/${"b".repeat(32)}`,
    );
  });

  it("下载地址会转义编号,而且先 trim", () => {
    expect(reportDownloadUrl(ART, "  ab/cd  ")).toBe("http://127.0.0.1:8788/by-id/ab%2Fcd");
  });
});

describe("解析", () => {
  it("正常一份,五个键都读出来", () => {
    const result = parseReportsEnvelope(envelope({ reports: [reportRow()], total: 1 }));

    expect(result.ok).toBe(true);
    expect(result.total).toBe(1);
    expect(result.reports).toEqual([
      {
        artifactId: "a".repeat(32),
        reportNo: "GYT-20260822-153012",
        filename: "巡检记录_GYT-20260822-153012.docx",
        sizeBytes: 20480,
        createdAt: "2026-08-22T07:30:12+00:00",
      },
    ]);
  });

  it.each([
    ["不是 JSON", "这不是 json"],
    ["ok 是 false", envelope(null, false, "出错了")],
    ["data 不是对象", envelope("字符串")],
    ["reports 不是数组", envelope({ reports: "不是数组" })],
  ])("%s 时回 ok:false —— **不许说成「还没有记录」**", (_label, body) => {
    const result = parseReportsEnvelope(body);

    // 🔴 这一条是整份测试里最要紧的。「读不出来」与「真的没有」在界面上长得
    //    一模一样,而给人的下一步完全相反:一个该重试,一个该去拍照。
    //    说成后者的表现是 —— 后端挂了的时候工友以为自己那份记录丢了,
    //    然后重拍一遍(又花一次识图的钱和二十秒)。
    expect(result.ok).toBe(false);
    expect(result.reports).toEqual([]);
  });

  it("后端那句人话在读不出来时也要留着", () => {
    // 非 200 那几种由组件按状态码说话,但 200 带 ok:false 这种
    // 「进了 handler 又没成」的情形,后端写好的那句是屏幕上唯一的线索。
    expect(parseReportsEnvelope(envelope(null, false, "沒權限")).userMsg).toBe("沒權限");
  });

  it("真的一份都没有时是 ok:true + 空数组", () => {
    const result = parseReportsEnvelope(envelope({ reports: [], total: 0 }));

    expect(result.ok).toBe(true);
    expect(result.reports).toEqual([]);
  });

  it("没有 artifact_id 的那一行跳过 —— 它点不开", () => {
    const result = parseReportsEnvelope(
      envelope({ reports: [reportRow({ artifact_id: "" }), reportRow()], total: 2 }),
    );

    // 渲染出来只是一行骗人的东西:看着能点,点了什么都不会发生。
    expect(result.reports).toHaveLength(1);
  });

  it("抠不出编号时是 null,不是空串、更不是编一个", () => {
    const result = parseReportsEnvelope(
      envelope({ reports: [reportRow({ report_no: "" })], total: 1 }),
    );

    expect(result.reports[0].reportNo).toBeNull();
    expect(result.reports[0].filename).toBe("巡检记录_GYT-20260822-153012.docx");
  });

  it.each([undefined, null, "20480", -1, Number.NaN])(
    "size_bytes 是 %s 时给 null,**不给 0**",
    (size) => {
      const result = parseReportsEnvelope(
        envelope({ reports: [reportRow({ size_bytes: size })], total: 1 }),
      );

      // 0 会被读成「这是个空文件」,而真相是「不知道多大」——
      // 与耗时那条 token 数同一条规矩。
      expect(result.reports[0].sizeBytes).toBeNull();
    },
  );

  it("total 读不出时兜底是手上真有的行数,不是 0", () => {
    const result = parseReportsEnvelope(envelope({ reports: [reportRow(), reportRow()] }));

    // 列着 2 行而表头写「共 0 份」是自相矛盾,人会以为界面坏了。
    expect(result.total).toBe(2);
  });

  it("scan_truncated 透传 —— 界面靠它说「只列了最近这些」", () => {
    expect(
      parseReportsEnvelope(envelope({ reports: [], total: 0, scan_truncated: true })).scanTruncated,
    ).toBe(true);
    expect(parseReportsEnvelope(envelope({ reports: [], total: 0 })).scanTruncated).toBe(false);
  });
});

describe("排版", () => {
  it.each([
    [0, "0 B"],
    [512, "512 B"],
    [1024, "1.0 KB"],
    [20480, "20 KB"],
    [1024 * 1024, "1.0 MB"],
  ])("%s 字节念成 %s", (bytes, expected) => {
    expect(formatSize(bytes)).toBe(expected);
  });

  it("大小是 null 时给空串,**不给「0 B」**", () => {
    // 「0 B」是在编一个数 —— 那会让人以为文件是空的。
    expect(formatSize(null)).toBe("");
  });

  it("时间从**编号**里解,而不是 created_at", () => {
    // 🔴 编号里那个时刻是**香港时间**(core/doc_no.py 从唯一时间权威取),
    //    而 sidecar 的 created_at 是 **UTC**(artifacts.register 用 datetime.now(UTC))。
    //    两者差 8 小时。给工地师傅看的必须是他手表上那个时间 ——
    //    直接渲染 created_at 的表现是「明明下午三点出的记录,列表上写着上午七点」,
    //    而没有任何报错。
    expect(formatReportTime("GYT-20260822-153012")).toBe("8月22日 15:30");
  });

  it.each([null, "", "不是编号", "GYT-2026-08-22", "GYT-20260822"])(
    "认不出的编号 %s 给空串,由调用方退回 createdAt",
    (bad) => {
      expect(formatReportTime(bad)).toBe("");
    },
  );

  it("标题优先用编号,抠不出才退回文件名", () => {
    // 编号是拿去跟人对账的那个东西(「你把 GYT-… 那份发我」),文件名只是它的包装。
    expect(
      reportTitle({
        artifactId: "a".repeat(32),
        reportNo: "GYT-20260822-153012",
        filename: "巡检记录_x.docx",
        sizeBytes: 1,
        createdAt: "",
      }),
    ).toBe("GYT-20260822-153012");

    expect(
      reportTitle({
        artifactId: "a".repeat(32),
        reportNo: null,
        filename: "不知道谁生成的.docx",
        sizeBytes: 1,
        createdAt: "",
      }),
    ).toBe("不知道谁生成的.docx");
  });
});
