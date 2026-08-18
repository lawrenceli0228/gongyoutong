/**
 * 「这些中文串**必须留简体**」的登记表 —— 界面繁體化的唯一豁免清单。
 *
 * 配套守卫:`hant-ui-strings.test.ts`。规矩是**默认繁體、例外登记**:
 * 覆盖件里任何一个 `s2hk(v) !== v` 的串,要么改成繁體,要么写进这里并说明理由。
 * 没登记 = 测试红。
 *
 * ===========================================================================
 * 为什么要有这张表(而不是「小心点别转错」)
 * ---------------------------------------------------------------------------
 * 这些串**转错了不会报错**,只会静默错:
 *
 *   · `图纸` 转成 `圖紙` → 后端拼的 `(图纸编号:…)` 匹配不上 →
 *     图纸退回一段裸 hex,页面照开、控制台干净;
 *   · `在办` 转成 `在辦` → 后端受控词表回 400 →
 *     监理以为是自己按错了按钮;
 *   · `替换成` 转成 `替換成` → 占位符被当成真令牌 →
 *     所有请求带着假 key 打 401。
 *
 * 靠人记是记不住的 —— 本仓已经在「同源清单」上栽过好几次。
 *
 * ⚠️ 往这里加一条,先问自己:**它是「值」还是「字」?**
 *    送去后端的、跟后端返回值比较的、当对象键的、匹配后端拼串的 → 是值,留简体。
 *    只是给人看的 → 是字,转繁體。
 *    两样都是的(grade / scope 就是)→ **值留简体,另起一张标签表**,别二选一。
 */

/**
 * 🔴 **转换器自己选错字、必须人工修正**的词。key = 简体原文,value = { right, wrong, why }。
 *
 * ===========================================================================
 * 为什么会有这张表 —— 「一律用工具、不许手打」这条铁律有例外
 * ---------------------------------------------------------------------------
 * `opencc` 的 `cn → hk` 靠**词组表**消歧,词组表里没有的词落回**单字默认**。
 * 「签」的单字默认是「籤」(抽籤、標籤那个籤),于是:
 *
 *     签发 → 簽發 ✅   签字 → 簽字 ✅   签收 → 簽收 ✅   签署 → 簽署 ✅
 *     签错 → 籤錯 ❌   ← 词组表里没有「签错」,落回单字默认
 *
 * 后果的严重程度跟 §5.1 那个「裁子集」是同一档:「簽」是签字,「籤」是抽签,
 * 在监理文书的语境里写错就是**另一个意思**,而**没有任何测试会发现**
 * (转换器没坏,只是选错了字)。
 *
 * ⚠️ 所以 `hant-verify-diff.mjs` 的判据「新值 === s2hk(旧值)」在这些词上
 *    **会把正确的人工修正报成剩菜**。登记在这里,那件工具就认得它了。
 *
 * ⚠️ 反过来更要紧:哪天有人跑溯源器看到剩菜,顺手「照工具回改」,
 *    就把「簽錯」改回「籤錯」了 —— 下面那条测试专门拦这一手。
 */
export const TOOL_MISCONVERT = Object.freeze({
  签错: {
    right: "簽錯",
    wrong: "籤錯",
    why:
      "签字的「簽」,不是抽签的「籤」。出现在监理面板签发暂停令的确认框正文里:" +
      "「暫停令是法律文書,簽字蓋章後據以停工。系統裏不能撤銷,簽錯只能另走複查/升級流程。」" +
      "opencc 的词组表收了签发/签字/签收/签署/签到,唯独漏了「签错」,于是落回单字默认「籤」。",
  },
  签文书: {
    right: "簽文書",
    wrong: "籤文書",
    why:
      "同一个坑的第二个词,监理面板里出现 5 次(「已簽文書和複查記錄」" +
      "「確認之後才進正式流程(簽文書、算整改率、能被升級)」等)。" +
      "词组表同样没收「签文书」,落回单字默认「籤」。" +
      "⚠️ 这两条说明**不是孤例而是一类**:opencc 的「签」单字默认就是「籤」," +
      "凡是词组表没收的「签X」组合都会中招。往这个产品里加新文案时," +
      "只要句子里有「签」,就回来核一眼工具给的是哪个字。",
  },
});

/**
 * 徽章「内表」的理由 —— `withHantKeys(简体组, 繁體组, styleOf)` 那种结构。
 *
 * 外层两组词收了简繁两套键(所以模型吐繁體时徽章照样上色),但 `styleOf`
 * 这个内层查表**只会被喂简体词**(withHantKeys 的实现:`styleOf(简体项)`,
 * 再把结果同时挂到简体键和繁體键上)。内表的键转了 = `styleOf` 四个都查不到,
 * 全落 `?? CHIP_BASE` 的灰底 —— 徽章还在、字还在,**只是颜色没了**,控制台干净。
 */
const BADGE_INNER_TABLE_WHY = (what) =>
  `🔴 ${what}徽章的**内层查表键**。它只被 withHantKeys 用简体词查(实现见 lang-lib.ts 的 ` +
  `withHantKeys:外层收简繁两套键,内层 styleOf 只喂简体)。转了 = 四档全查不到样式,` +
  `落灰底默认色 —— 徽章还在、字还在,只是颜色没了,而且控制台干净、测试全绿。` +
  `⚠️ 简繁同形的那几个(重大/一般)一并登记,是因为它们同形只是**碰巧**;` +
  `登记在这儿才能让「参与匹配的位置」那条守卫替下一个人拦住手改。`;

/** 徽章直接拿后端返回值查表的理由 —— 没有 withHantKeys 那一层。 */
const BADGE_BACKEND_KEY_WHY =
  "🔴 隐患定级徽章的查表键,直接拿**后端返回的 `hazard.severity`** 精确匹配" +
  "(那一份永远是简体:`agents/safety/severity.py` 的四个常量)。" +
  "转了 = 查不到样式,落灰底默认色,**颜色没了而控制台干净**。" +
  "上屏的字是另一条路(`useHantUI(hazard.severity)`),两者互不影响 —— " +
  "这正是「值留简体、屏幕上转」那条分层在同一行代码里的样子。" +
  "⚠️ 同形的重大/一般一并登记,理由同上:同形是碰巧,不是保证。";

/** 整份豁免的文件。key = 文件名,value = 理由。 */
export const FILE_EXEMPT = Object.freeze({
  "lang-lib.ts":
    "整份是**数据不是文案**:SCRIPT_PAIRS 是简繁判别字对(转了判别就永远打平)," +
    "TASK_STATUS_WORDS / SEVERITY_WORDS 是拿去精确匹配模型输出的**简体键**" +
    "(繁體键另有 _HANT 一组,派生自它)。这个文件一个字都不上屏。",
});

/**
 * 逐条豁免。key = `文件名`,value = `{ 串: { why, contexts } }`。
 *
 * 用「文件 + 串」而不是「文件 + 行号」做键:行号会随无关改动漂,
 * 漂了之后守卫要么误报要么被人放宽,两条路都通向守卫失效。
 *
 * ===========================================================================
 * 🔴 `contexts` 为什么是必需的 —— 这条是 2026-08-18 实测出来的设计缺陷
 * ---------------------------------------------------------------------------
 * 第一版只有 `{ 串: 理由 }`,于是**豁免的粒度是「整个文件里所有同名的串」**。
 * 当天就出事了:
 *
 *     human.tsx:114/133/153   "图纸"  ← 匹配后端拼的标记,必须留简体(登记的就是它)
 *     human.tsx:253           "图纸"  ← `<div>图纸</div>`,**纯显示标签**
 *
 * 那条为匹配位置写的豁免,把 253 那个显示标签**一起盖住了** ——
 * 守卫永远不会报它。代价是具体的:同一个词,上传那一刻显示「圖紙 xxx.dxf」
 * (MultimodalPreview 转了),翻历史却显示「图纸」(human.tsx 没转),
 * 而 human.tsx:239 的注释明写着这两处「长相刻意对齐」。
 *
 * 所以豁免必须连**语法位置**一起登记:「这个串在**这些位置上**是值,
 * 在别的位置上它就只是个字」。
 *
 * contexts 里写的是 `hant-scan.mjs` 的 `describeContext()` 产出的字符串,
 * 常见几种:`jsx-text` / `jsx-attr:title` / `object-value:xxx` / `array-item` /
 * `const:XXX` / `call:foo.includes#0` / `LiteralType` / `compare:===`。
 * 拿不准就先跑 `node hant-scan.mjs list | grep 你的串` 看它现在落在哪。
 */
export const KEEP_HANS = Object.freeze({
  "human.tsx": {
    图纸: {
      why:
        "🔴 匹配后端 uploads.py 拼进消息里的 `(图纸编号:<32位hex>)`,那段是**简体**。" +
        "转成「圖紙」→ 正则匹配不上 → 图纸退回一段裸 hex,**零报错**。" +
        "⚠️ contexts 卡死在类型标注与实参两处 —— 同一个文件里还有一个 " +
        "`<div>图纸</div>` 是**纯显示标签**,那个必须转。粒度不卡的话它会被这条顺带盖住," +
        "表现是上传那一刻显示「圖紙 x.dxf」、翻历史却显示「图纸」,而两处注释明写着刻意对齐。",
      contexts: [/^LiteralType$/, /^call:takeRefs#/],
    },
    照片: {
      why:
        "🔴 同上,匹配 `(照片编号:<32位hex>)`。" +
        "它**简繁同形**,所以「还是简体」那条守卫永远不会报它 —— " +
        "登记在这儿是为了让「参与匹配的位置」那条守卫认得它。" +
        "别因为它看着无害就删:哪天有人把这两个词一起改成别的说法,这条会说话。",
      contexts: [/^LiteralType$/, /^call:takeRefs#/],
    },
    "编号[:：]\\s*(":
        "🔴 `refPattern` 正则的中段 —— 与上面那两个词拼成 `(照片编号:…)` / " +
        "`(图纸编号:…)`,整条正则认的是后端 uploads.py 拼进消息正文的那段**简体**。" +
        "转成「編號」→ 两类编号一条都匹配不上 → 照片和图纸全退回一段裸 hex," +
        "页面照开、控制台干净,**零报错**。" +
        "⚠️ 它长得像正则不像文案,是这个文件里最容易被当成「反正不上屏」而漏登记的一条;" +
        "漏了守卫就红,而红了最省事的做法恰恰是去放宽守卫 —— 所以单独写清楚。",
  },
  // ── guessViewType 的四个文件名关键词 ──────────────────────────────────────
  // 这四个词在本文件里各出现在**三种**语法位置,而只有一种是「值」:
  //     object-value:label   VIEW_OPTIONS 的按钮标签      ← 字,该转(碰巧同形)
  //     object-value:plan/…  VIEW_LABEL 的资料库徽章标签  ← 字,该转(碰巧同形)
  //     call:n.includes#0    guessViewType 判图纸类型     ← 值,必须留简体
  // 所以 contexts 只卡 includes 那一处。整串豁免会把两处**显示标签**一起盖住 ——
  // 今天它们简繁同形所以看不出损失,哪天有人把标签改成「平面圖」之类的词,
  // 守卫就再也不会提醒了。
  "ProjectUploadPanel.tsx": {
    平面: {
      why:
        "🔴 拿去 `n.includes(...)` 匹配**用户上传的文件名**,判这张 .dxf 是平/立/剖。" +
        "匹配的是文件名不是界面文字:工地传上来的图九成还叫「三层平面图.dxf」," +
        "关键词一改就永远猜不中 —— 表现是每次都得手选一遍类型,**没有任何报错**。" +
        "(它本身简繁同形,所以「还是简体」那条守卫永远不报它;" +
        "登记在这儿是为了让「参与匹配的位置」那条守卫认得它。)",
      contexts: [/^call:n\.includes#/],
    },
    立面: {
      why:
        "🔴 与「平面」同一处 `n.includes(...)`,匹配用户上传的文件名判图纸类型。" +
        "它是判据不是标签,动了会让立面图静默落进「认不出类型」那档。",
      contexts: [/^call:n\.includes#/],
    },
    剖面: {
      why:
        "🔴 与「平面」同一处,匹配文件名判图纸类型。同样是判据不是标签。",
      contexts: [/^call:n\.includes#/],
    },
    剖: {
      why:
        "🔴 「剖面」的宽松兜底 —— 文件名只写一个「剖」字也认得出来。" +
        "它是判据不是标签,而且短到最容易被人当成笔误删掉,所以单独登记。",
      contexts: [/^call:n\.includes#/],
    },
    "useArchive 必须在 <ArchiveProvider> 内使用": {
      why:
        "开发期不变式的 `throw new Error`,只在「组件挂到 <ArchiveProvider> 外面」时抛 —— " +
        "那是程序员写错了,不是工友按错了。生产环境它会被 Next 的错误边界吞成一句英文," +
        "这段字一个工友都看不到。与下面 hant-convert.tsx 的 console.warn、" +
        "thread-history.tsx 的 console.error 同一口径:本仓「注释 / 日志 / 开发者报错一律简体」。",
      contexts: [/^NewExpression$/],
    },
  },
  // ── 徽章查表的中文键(三个文件,共 12 个)──────────────────────────────────
  //
  // 这一组是 2026-08-18 补上扫描器盲区后才**第一次可见**的:CJK 是合法的 JS
  // 标识符字符,所以 `{ 重大: "…" }` 里的键是 `Identifier` 而不是 `StringLiteral`,
  // 在那之前**两道守卫一起看不见它们**(见 hant-scan.mjs 的 isChineseObjectKey)。
  //
  // 它们全是「值」不是「字」——「一般」「重大」这些简繁同形的也一并登记,
  // 因为它们同形只是**碰巧**:哪天有人把「较大」手改成「較大」,
  // 徽章查不到样式会落 `?? CHIP_BASE` 的灰底,**颜色没了而控制台干净**。
  "markdown-text.tsx": {
    已逾期: { why: BADGE_INNER_TABLE_WHY("任务状态"), contexts: [/^object-key$/] },
    未完成: { why: BADGE_INNER_TABLE_WHY("任务状态"), contexts: [/^object-key$/] },
    已完成: { why: BADGE_INNER_TABLE_WHY("任务状态"), contexts: [/^object-key$/] },
    没定期限: { why: BADGE_INNER_TABLE_WHY("任务状态"), contexts: [/^object-key$/] },
  },
  "tool-calls.tsx": {
    重大: { why: BADGE_INNER_TABLE_WHY("隐患定级"), contexts: [/^object-key$/] },
    较大: { why: BADGE_INNER_TABLE_WHY("隐患定级"), contexts: [/^object-key$/] },
    一般: { why: BADGE_INNER_TABLE_WHY("隐患定级"), contexts: [/^object-key$/] },
    待定级: { why: BADGE_INNER_TABLE_WHY("隐患定级"), contexts: [/^object-key$/] },
  },
  "api-key.tsx": {
    替换成:
        "🔴 与 `scripts/preflight_vps.sh:67` 的 `case ... 替换成*|change*|your*` **同源**," +
        "认的是 .env 示例里没换掉的占位符。转了两边就对不上,表现是" +
        "占位符被当成真令牌、所有请求带假 key 打 401。",
  },
  "supervision-lib.ts": {
    在办: {
      why:
          "🔴 送后端的 `?scope=` 受控词(`agents/supervision/scoping.SCOPES`)," +
          "词表外后端直接回 400。上屏的繁體由 supervision.tsx 在渲染处转" +
          "(`useHantUIAll(HAZARD_SCOPES)`)—— **没有第二张繁體常量表**。" +
          "⚠️ 这句话原来写的是「在 HAZARD_SCOPE_LABEL 那张表里」,那是设计转向**之前**的说法," +
          "而那张表从来没被建出来过。转向的理由:屏幕上真正显示的状态字是后端的 " +
          "`status_display`(supervision.tsx:1349),拆多少张前端常量表都不生效。" +
          "留着错指针的坏处不是脏,是下一个人照着去找会扑空,然后以为「表被谁删了」。",
      contexts: [/^const:HAZARD_SCOPE_ACTIVE$/],
    },
    待确认: {
      why:
        "🔴 送后端的 `?scope=` 受控词,词表外回 400。上屏的繁體由渲染处转。" +
        "⚠️ 它**身兼两职**,而且两职都是「值」—— contexts 把两处都写出来了:" +
        "① `const:HAZARD_SCOPE_PENDING` = 筛子受控词;" +
        "② `object-value:pending` = `HAZARD_STATUS_ZH.pending` 的中文名," +
        "逐字镜像后端 `scoping.STATUS_ZH`(理由见下面那一组)。" +
        "两职共用一条登记 —— 哪天 scope 词表改了名把这条删掉,状态镜像那一处会跟着" +
        "失去登记,而守卫只会说「多了一条没登记的简体串」,不会说是谁的锅。" +
        "所以真要删,先回来看这段。",
      contexts: [/^const:HAZARD_SCOPE_PENDING$/, /^object-value:pending$/],
    },
    严重: {
      why:
          "🔴 三重身份:① `HAZARD_GRADES.includes(grade)` 校验后**送后端**;" +
          "② `hazard.grade === GRADE_SEVERE` 拿来跟**后端返回值**比;" +
          "③ 在 supervision.tsx 里当**徽章样式的对象键**(键也来自后端值)。" +
          "上屏的繁體由 supervision.tsx 在渲染处转(`useHantUIAll(GRADE_CHOICES)` " +
          "与 `useHantUI(hazard.grade)`)—— **没有 GRADE_LABEL 那张表**,理由同「在办」那条。" +
          "(同组的「一般」简繁同形,所以不在表里。)",
      contexts: [/^const:GRADE_SEVERE$/],
    },

    // ── HAZARD_STATUS_ZH 八档 ──────────────────────────────────────────────
    // 它**逐字镜像**后端 `agents/supervision/scoping.py` 的 `STATUS_ZH`
    // (CLAUDE.md「同源清单」里那条:跨语言镜像收敛不掉,靠手工对齐)。
    //
    // 转成繁體会同时坏两处,两处都**没有报错**:
    //   ① 状态徽章那一行是 `hazard.status_display || hazardStatusZh(hazard.status)`
    //      (supervision.tsx:1292)—— 左边是**后端**拼好的简体,右边是本地这张表。
    //      本地转了而后端没转,同一颗徽章会因为「这次是哪条路供的字」而变字形:
    //      清单端点带 status_display 走左边,`patchHazard` 改完状态丢掉 status_display
    //      之后走右边。工友看到的是「点一下确认,徽章的字忽然换了一种写法」;
    //   ② 手工对齐从此没法逐字 diff —— 而这两张表能不能对上,今天唯一的验法就是
    //      把两边并排看一眼(后端加一档、中文名没跟上时,漏配是**导入期硬失败**,
    //      前端这一侧却只会静默透出一个英文状态词)。
    // 上屏的繁體统一由 supervision.tsx 在渲染处转 —— 后端来的 status_display
    // 本来就必须过那一道,两条路只有都过同一道才会一致。
    已确认待处置: {
      why:
          "🔴 `open` 那一档,镜像后端 `scoping.STATUS_ZH['open']`。" +
          "它是「徽章两条路」最常撞上的一档:确认成功后 `patchHazard` 会**丢掉** " +
          "`status_display`(旧标签会一直赢,W10 抓到过),于是渲染当场回落到本地这张表。" +
          "本地转了 = 同一条隐患确认前后字形不一样,而没有任何报错。",
      contexts: [/^object-value:open$/],
    },
    已签发通知单: {
      why:
          "🔴 `notified` 那一档,镜像后端 `scoping.STATUS_ZH['notified']`。" +
          "后端**还会把这五个字拼进人话里**(批量确认的 failed.reason:" +
          "「这条现在是「已签发通知单」,不用再确认。」)。本地表转了之后," +
          "同一屏上会同时出现两种写法 —— 一处来自徽章、一处来自那句原样透传的 user_msg。",
      contexts: [/^object-value:notified$/],
    },
    已出具暂停令: {
      why:
          "🔴 `suspended` 那一档,而且是**措辞红线**那一档:只能念「已出具暂停令」," +
          "不许升级成「已责令停工」(文书出了稿 ≠ 工地真停了工)。" +
          "后端 `test_supervision_scoping.py` 与前端 `supervision-lib.test.ts` " +
          "各钉了一条**逐字相同**的断言;字形一变,两条钉子就不再钉同一句话," +
          "而这句话对外是要担责任的。",
      contexts: [/^object-value:suspended$/],
    },
    复查不合格: {
      why:
          "🔴 `reinspect_failed` 那一档,镜像后端 `scoping.STATUS_ZH`。" +
          "坏法同上:徽章两条路各说一种写法,而这一档恰恰是**要再复查一次**的入口," +
          "监理得靠它认出「这条还没完」。",
      contexts: [/^object-value:reinspect_failed$/],
    },
    待签发复工令: {
      why:
          "🔴 `resuming` 那一档,镜像后端 `scoping.STATUS_ZH`。" +
          "坏法同上。⚠️ 顺带一提:它的繁體正确写法是「復工令」不是「複工令」," +
          "所以更不该在这儿手工维护第二份 —— 上屏那一处交给转换器。",
      contexts: [/^object-value:resuming$/],
    },
    已销项: {
      why:
          "🔴 `closed` 那一档,镜像后端 `scoping.STATUS_ZH`。坏法同上。" +
          "它是终态,清单上「还剩几条」全靠它划线,写法分叉时最容易被当成两种状态。",
      contexts: [/^object-value:closed$/],
    },
    已上报主管部门: {
      why:
          "🔴 `escalated` 那一档,镜像后端 `scoping.STATUS_ZH`。坏法同上。" +
          "这一档对应的是报建设主管部门的正式指控,措辞与后端逐字一致才对得上文书。",
      contexts: [/^object-value:escalated$/],
    },

    // ── DOC_TYPE_ZH 六档 ──────────────────────────────────────────────────
    // 前五档镜像 `core/doc_no.DOC_TITLE_ZH`,第六档(复查记录)镜像
    // `agents/supervision/documents.py` 的 `_doc_type_zh`。
    //
    // 🔴 除了「镜像」这条通用理由,它们还有一条**硬的**:`docTypeZh()` 被拿去
    // **拼文件名兜底** —— `toDoc` / `documentsFromToolData` 里那句
    // `textOf(rec,"filename") || \`${docTypeZh(docType)}_${docNo}.docx\``,
    // 而后端 `supervision_api._filename()` 拼的是
    // `f"{_DOC_TYPE_ZH[doc_type]}_{doc_no}.docx"`,**简体**。
    // 转了之后前端兜底拼出来的名字和盘上那份对不上,而 supervision_api.py 头注
    // 明写着「详情里 documents[*].filename 与签发那条路给的必须一模一样」。
    // 症状:下载卡的 title 上挂着一个繁體文件名,盘上那份是简体 ——
    // 卡片照渲、文件照下(取件走 artifact_id,不走名字),等有人拿文件名去对账那天才发现。
    // 上屏的繁體同样交给 supervision.tsx 在渲染处转。
    监理通知单: {
      why:
          "🔴 `notice`,镜像 `core/doc_no.DOC_TITLE_ZH`。" +
          "它是**文件名兜底**那条链上最常走到的一档(通知单是监理日常动作)," +
          "转了就和后端 `_filename()` 拼的那份对不上,而两处必须一模一样。",
      contexts: [/^object-value:notice$/],
    },
    工程暂停令: {
      why:
          "🔴 `suspension`,镜像 `core/doc_no.DOC_TITLE_ZH`。同样进文件名兜底。" +
          "这份是签字盖章后据以停工的法律文书,名字与后端对不上最不该发生在它身上。",
      contexts: [/^object-value:suspension$/],
    },
    工程复工令: {
      why:
          "🔴 `resumption`,镜像 `core/doc_no.DOC_TITLE_ZH`。同样进文件名兜底。" +
          "⚠️ 它的繁體是「工程復工令」(復,不是複)—— 正因为一简对多繁," +
          "更不该在这张镜像表里手工维护第二份。",
      contexts: [/^object-value:resumption$/],
    },
    致建设单位报告: {
      why:
          "🔴 `owner_report`,镜像 `core/doc_no.DOC_TITLE_ZH`。同样进文件名兜底。" +
          "签暂停令时它是一次出的三份之一,三份的名字要能和台账逐份对上。",
      contexts: [/^object-value:owner_report$/],
    },
    监理报告: {
      why:
          "🔴 `authority_report`,镜像 `core/doc_no.DOC_TITLE_ZH`。同样进文件名兜底。" +
          "它是报建设主管部门那一份,事后追责时按名字取件的概率最高。",
      contexts: [/^object-value:authority_report$/],
    },
    复查记录: {
      why:
          "🔴 `reinspect`,镜像 `agents/supervision/documents.py` 的 `_doc_type_zh`" +
          "(它**不是文书**,`DocKind` 里没有这一档)。" +
          "它比五种文书更依赖这条兜底:后端对复查行回的 `filename` **就是 null**," +
          "所以那个名字**必定**由本地这张表拼出来。转了之后证据链上那一行的名字" +
          "与后端任何一处都对不上,而它长得依然像个正常文件名。",
      contexts: [/^object-value:reinspect$/],
    },

    // ── 整改期限的例句 ────────────────────────────────────────────────────
    // 与 supervision.tsx 那两条(`DUE_PHRASE_EXAMPLES` / `DUE_PHRASE_SAMPLE`)
    // 是**同一条约束的第三处**:那两处是输入框上方的例句与 placeholder,
    // 这一处是「期限没填」时抛的那句报错(`SUPERVISION_MESSAGES.missingDue`)。
    // 三处教的是同一批写法,漏掉任何一处 = 那一处照样在教一句后端解析不了的话。
    "「明天」「3天后」「下周三」「月底」": {
      why:
          "🔴 `SUPERVISION_MESSAGES.missingDue` 里那截例句(已拆成独立常量 " +
          "`DUE_PHRASE_EXAMPLES`)。它是**让人照抄进输入框的原话**,抄完原样送后端 " +
          "`agents/schedule/dates.py`,而那边的正则只认简体(`(下下周|下周)`、" +
          "`(.+)天[后内]`、`周|星期|礼拜`),后端也没有繁→简归一化。" +
          "转成「下週三」→ 一条都匹配不上 → 监理照着系统教的写法打字却被反复拒绝," +
          "而屏幕上没有任何线索指向正确写法。" +
          "⚠️ **只豁免这一小截**:外面那句「得寫明整改期限(比如…)。」仍然是繁體、" +
          "仍然受守卫管 —— 把整句登记进来的话,下次有人改措辞时漏转繁體不会有人发现。",
      contexts: [/^const:DUE_PHRASE_EXAMPLES$/],
    },
  },
  "supervision.tsx": {
    // ── 隐患定级徽章的查表键(四档)────────────────────────────────────────
    // 2026-08-18 补上扫描器盲区后才第一次可见:CJK 是合法的 JS 标识符字符,
    // 所以 `{ 重大: "…" }` 里的键是 Identifier 不是 StringLiteral,
    // 在那之前**两道守卫一起看不见它们**(见 hant-scan.mjs 的 isChineseObjectKey)。
    重大: { why: BADGE_BACKEND_KEY_WHY, contexts: [/^object-key$/] },
    较大: { why: BADGE_BACKEND_KEY_WHY, contexts: [/^object-key$/] },
    一般: { why: BADGE_BACKEND_KEY_WHY, contexts: [/^object-key$/] },
    待定级: { why: BADGE_BACKEND_KEY_WHY, contexts: [/^object-key$/] },

    // ── 整改期限那一格的例句 ────────────────────────────────────────────────
    // 这两条是本文件仅有的豁免,而且它们**不是**「值」的常见那几种(不送后端、
    // 不跟返回值比、不当键)—— 它们是**让人照抄进输入框的原话**,抄完那格原话
    // 原样送后端,由 `agents/schedule/dates.py` 解析。
    //
    // 那边从头到尾只认简体,而且后端**没有任何繁→简归一化**(全仓 grep 过):
    //     _NEXT_WEEKS_RE  = (下下周|下周)([一二三四五六日天1-7])
    //     _DAYS_RE        = (.+)天[后内]
    //     _BARE_WEEKDAY_RE= (?:周|星期|礼拜)([一二三四五六日天1-7])
    // 实测:「下週三」「3天後」「禮拜三」一条都匹配不上,直接 DueParseError。
    //
    // 转了的症状不是静默,是**更糟的一种可见错**:监理照着屏幕上的例句打进去,
    // 被后端回一句「这个日期我算不准」——而他刚刚是照着系统教的写法写的,
    // 屏幕上没有任何线索说该换成哪种写法,只会反复试、反复被拒。
    //
    // ⚠️ 留简体只是两害相权,**不是修好了**:HK 监理自己打「下週三」照样会被拒。
    //    真正的修法在后端入口做一次繁→简归一化,已回报为欠账。
    //
    // 与 supervision-lib.ts 那条(`SUPERVISION_MESSAGES.missingDue` 里的例句)
    // 是同一条约束的三处落点,三处都在教同一批写法,漏一处就有一处在教错。
    "明天 / 3天后 / 下周三 / 月底": {
      why:
          "🔴 整改期限输入框上方的例句(`DUE_PHRASE_EXAMPLES`)。它是**让人照抄的原话**," +
          "抄完原样送后端 `agents/schedule/dates.py`,而那边的正则/词表只认简体" +
          "(`(下下周|下周)`、`(.+)天[后内]`、`周|星期|礼拜`),后端也没有繁→简归一化。" +
          "转成「下週三」→ 一条都匹配不上 → 监理照着系统教的写法打字却被反复拒绝," +
          "而屏幕上没有任何线索指向正确写法。",
      // 卡死在那个常量上:同样这几个字哪天出现在别的位置(比如真写成一句 JSX 文案),
      // 它就只是个字、该转 —— 这条豁免不许顺带盖住它。
      contexts: [/^const:DUE_PHRASE_EXAMPLES$/],
    },
    下周三: {
      why:
          "🔴 同一处的 placeholder 例句(`DUE_PHRASE_SAMPLE`),坏法与上面那条**一模一样**。" +
          "单独登记而不是并进上面那条:它是独立的字面量,上面那条哪天改了措辞," +
          "这条不会跟着失去登记(反过来也一样)。",
      contexts: [/^const:DUE_PHRASE_SAMPLE$/],
    },
  },
  "hant-convert.tsx": {
    "[gyt] 繁體字典没拉到,答话保持简体显示":
        "console.warn 的内容,只进开发者控制台、不上屏。" +
        "留简体与本仓「注释/日志一律简体」的口径一致。",
  },
  "thread-history.tsx": {
    "[历史列表] 删除线程失败": "console.error 的内容,不上屏。同上。",
  },
});

/** 查一条串是否已登记豁免。 */
export function isExempt(file, value, context) {
  if (file in FILE_EXEMPT) return true;
  const entry = KEEP_HANS[file]?.[value];
  if (!entry) return false;

  // 老格式(值直接是理由字符串)= **整个文件里所有同名的串**都豁免。
  // 保留它只是为了让在途的改动不炸;新条目一律写成 { why, contexts } ——
  // 那条粒度缺陷的实录在本文件 KEEP_HANS 的头注里(human.tsx 的「图纸」)。
  if (typeof entry === "string") return true;

  // 没传 context 的调用方(临时脚本)按老行为走,别让它们莫名其妙全红。
  if (!context) return true;

  return entry.contexts.some((re) => re.test(context));
}
