/*
 * 规格书导入页的交互回归测试（无浏览器依赖）。
 *
 * 重点不是「页面长得对」，而是几条容易静默出错的判断：
 *   1. 条件没填齐时点击必须有反馈（曾经的 bug：按钮禁用 → 点击无任何反应）
 *   2. 型号已存在要说「将更新」，不存在要说「将新建」——导入是扩充数据库的通道
 *   3. 新公司名称与已有公司相近时必须提示，且露出「确认是另一家」的勾选
 *   4. 型号已属于其他大类时必须预警（后端会 409，前端不该等人提交才发现）
 *
 * 跑法：node tests/js/test_datasheet_ui.js
 */

"use strict";

const path = require("path");

let failures = 0;
const results = [];

function check(name, fn) {
  try {
    fn();
    results.push(["PASS", name, ""]);
  } catch (e) {
    failures += 1;
    results.push(["FAIL", name, e.message]);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function assertEqual(actual, expected, msg) {
  if (actual !== expected) {
    throw new Error(`${msg}：期望 ${JSON.stringify(expected)}，实际 ${JSON.stringify(actual)}`);
  }
}

function assertIncludes(haystack, needle, msg) {
  if (String(haystack).indexOf(needle) < 0) {
    throw new Error(`${msg}：应包含 ${JSON.stringify(needle)}，实际 ${JSON.stringify(haystack)}`);
  }
}

// ── 最小 DOM 桩 ────────────────────────────────────────────
class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(c) { this.set.add(c); }
  remove(c) { this.set.delete(c); }
  contains(c) { return this.set.has(c); }
  toggle(c, on) {
    const want = on === undefined ? !this.set.has(c) : !!on;
    if (want) this.set.add(c); else this.set.delete(c);
    return want;
  }
  get value() { return [...this.set].join(" "); }
}

class El {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.options = [];
    this.files = null;
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.textContent = "";
    this.innerHTML = "";
    this.disabled = false;
    this.classList = new ClassList();
    this.listeners = {};
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  dispatch(type, extra) {
    const evt = Object.assign({ preventDefault() {}, target: this, dataTransfer: null }, extra || {});
    (this.listeners[type] || []).forEach((fn) => fn(evt));
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(node) { this.children.push(node); }
  focus() { this.focused = true; }
  scrollIntoView() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get className() { return this.classList.value; }
  set className(v) {
    this.classList = new ClassList();
    String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
  }
}

const IDS = [
  "cfg", "companySel", "newCompanyWrap", "newCompanyInput", "categorySel", "modelInput",
  "modelList", "resolveHint", "forceWrap", "forceNewCompany", "productIndex",
  "fileInput", "fileName", "dropZone", "analyzeBtn", "uploadMsg", "dsStatus",
  "reviewPanel", "reviewBody", "reviewHint", "selStat", "headCheck", "commitBtn",
  "commitMsg", "checkAll", "checkNone", "checkHigh",
];

const els = {};
IDS.forEach((id) => { els[id] = new El(id); });

els.cfg.dataset = { entity: "", minConf: "0.75", maxMb: "25", productTotal: "11" };

// 公司下拉：id 型选项会被脚本读成「已有公司」，"__new__" 是新建入口
els.companySel.options = [
  { value: "", textContent: "— 选择公司 —" },
  { value: "1", textContent: "本公司（本方）" },
  { value: "2", textContent: "村田制作所" },
  { value: "3", textContent: "巴特尔斯微技术" },
  { value: "__new__", textContent: "＋ 新建公司…" },
];

els.categorySel.options = [
  { value: "", textContent: "— 选择大类 —" },
  { value: "pump_air", textContent: "压电气泵" },
  { value: "fan", textContent: "压电风扇" },
  { value: "pump_liquid", textContent: "压电液泵" },
];

// 已有型号索引（服务端注入的那段 JSON）
els.productIndex.textContent = JSON.stringify([
  { entity_id: 2, model: "MZB1001T02", category: "pump_air" },
  { entity_id: 3, model: "mp6-gas", category: "pump_air" },
  { entity_id: 3, model: "mp6-liq", category: "pump_liquid" },
]);

const fetchCalls = [];
let lastFetchBody = null;

global.document = {
  getElementById: (id) => els[id] || null,
  querySelector: (sel) => (String(sel).indexOf("csrf-token") >= 0 ? { content: "csrf-token-stub" } : null),
  querySelectorAll: () => [],
  createElement: () => new El("created"),
  addEventListener: () => {},
};
global.window = { location: { href: "" } };
global.setTimeout = () => 0;
global.fetch = (url, opts) => {
  fetchCalls.push({ url, opts });
  lastFetchBody = opts && opts.body;
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify({
      doc_id: "x", candidates: [], metrics: [], pages: 1, chars: 10,
      metric_total: 0, target: { entity_name: "", model: "", category_label: "" },
    })),
  });
};

const SCRIPT = path.join(__dirname, "..", "..", "app", "static", "datasheet.js");
require(SCRIPT);

// ── 用例 ───────────────────────────────────────────────────
check("初始状态提示选公司", () => {
  assertIncludes(els.dsStatus.textContent, "请先选择公司", "状态栏");
  assertEqual(els.analyzeBtn.getAttribute("aria-disabled"), "true", "按钮应标记未就绪");
});

check("未填公司时点击给出反馈（原 bug：静默无反应）", () => {
  els.analyzeBtn.dispatch("click");
  assertEqual(els.uploadMsg.textContent, "请先选择公司（或新建公司）", "点击后必须说明缺什么");
  assertEqual(fetchCalls.length, 0, "条件不满足时不应发请求");
});

check("填齐前逐项提示：大类 / 型号 / 文件", () => {
  els.companySel.value = "2";
  els.companySel.dispatch("change");
  assertIncludes(els.dsStatus.textContent, "请选择产品大类", "缺大类");

  els.categorySel.value = "pump_air";
  els.categorySel.dispatch("change");
  assertIncludes(els.dsStatus.textContent, "请填写型号", "缺型号");

  els.modelInput.value = "MZB1001T02";
  els.modelInput.dispatch("input");
  assertIncludes(els.dsStatus.textContent, "请选择规格书文件", "缺文件");
  assertEqual(fetchCalls.length, 0, "仍不应发请求");
});

check("已有型号提示「将更新」，新型号提示「将新建」", () => {
  els.modelInput.value = "MZB1001T02";
  els.modelInput.dispatch("input");
  assertIncludes(els.resolveHint.textContent, "将更新", "命中已有型号");

  els.modelInput.value = "MZB9999Z99";
  els.modelInput.dispatch("input");
  assertIncludes(els.resolveHint.textContent, "将新建型号", "未命中应提示新建");
});

check("型号属于其他大类时预警", () => {
  // mp6-liq 属液泵，这里故意选气泵
  els.companySel.value = "3";
  els.companySel.dispatch("change");
  els.categorySel.value = "pump_air";
  els.categorySel.dispatch("change");
  els.modelInput.value = "mp6-liq";
  els.modelInput.dispatch("input");
  assertIncludes(els.resolveHint.textContent, "提交会被拒绝", "应预警大类冲突");
  assertIncludes(els.resolveHint.className, "warn", "应为警告样式");
});

check("新建公司：与已有名称相近时要求确认", () => {
  els.companySel.value = "__new__";
  els.companySel.dispatch("change");
  assertEqual(els.newCompanyWrap.hidden, false, "应显示新公司名称输入");

  els.newCompanyInput.value = "村田";
  els.newCompanyInput.dispatch("input");
  assertIncludes(els.resolveHint.textContent, "名称相近", "应提示相似公司");
  assertEqual(els.forceWrap.hidden, false, "应露出「确认是另一家」");
});

check("新建公司：完全同名时直接归入已有，不建新档案", () => {
  els.newCompanyInput.value = "村田制作所";
  els.newCompanyInput.dispatch("input");
  assertIncludes(els.resolveHint.textContent, "已有同名公司", "应提示归入");
  assertEqual(els.forceWrap.hidden, true, "同名不需要确认新建");
});

check("齐备后进入就绪", () => {
  els.companySel.value = "2";
  els.companySel.dispatch("change");
  els.categorySel.value = "pump_air";
  els.categorySel.dispatch("change");
  els.modelInput.value = "MZB1001T02";
  els.modelInput.dispatch("input");
  els.fileInput.files = [{ name: "mzb.pdf", size: 2048 }];
  els.fileInput.dispatch("change");

  assertIncludes(els.dsStatus.textContent, "已就绪", "状态栏");
  assertEqual(els.analyzeBtn.getAttribute("aria-disabled"), "false", "按钮应可用");
});

check("就绪后发起识别，表单带公司/大类/型号", () => {
  const before = fetchCalls.length;
  els.analyzeBtn.dispatch("click");
  assertEqual(fetchCalls.length, before + 1, "应发 1 次请求");
  assertEqual(fetchCalls[0].url, "/api/datasheet/analyze", "请求地址");
  assertEqual(fetchCalls[0].opts.headers["X-CSRF-Token"], "csrf-token-stub", "必须带 CSRF 头");

  const body = fetchCalls[0].opts.body;
  assertEqual(body.get("entity_id"), "2", "应传公司 id");
  assertEqual(body.get("category"), "pump_air", "应传大类");
  assertEqual(body.get("model"), "MZB1001T02", "应传型号");
});

check("新建公司时改传名称而非 id", () => {
  els.companySel.value = "__new__";
  els.companySel.dispatch("change");
  els.newCompanyInput.value = "江苏江佳电子";
  els.newCompanyInput.dispatch("input");
  els.analyzeBtn.dispatch("click");
  const body = fetchCalls[fetchCalls.length - 1].opts.body;
  assertEqual(body.get("entity_id"), null, "新建公司不应有 entity_id");
  assertEqual(body.get("entity_name"), "江苏江佳电子", "应传新公司名称");
});

check("超大文件被拦截并说明原因", () => {
  els.companySel.value = "2";
  els.companySel.dispatch("change");
  els.fileInput.files = [{ name: "huge.pdf", size: 30 * 1024 * 1024 }];
  els.fileInput.dispatch("change");
  els.analyzeBtn.dispatch("click");
  assertIncludes(els.uploadMsg.textContent, "超过上限 25 MB", "应提示超限");
});

// ── 输出 ───────────────────────────────────────────────────
results.forEach(([status, name, msg]) => {
  console.log(`  ${status}  ${name}${msg ? "  " + msg : ""}`);
});
console.log(`\n${results.length - failures}/${results.length} passed`);
process.exit(failures ? 1 : 0);
