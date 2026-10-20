/*
 * 规格书导入页的交互回归测试（无浏览器依赖）。
 *
 * 覆盖的正是线上报的那个 bug：按钮处于不可用状态时点击没有任何反馈，
 * 用户无法区分「没选齐」和「页面坏了」。
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
    this.files = null;
    this.value = "";
    this.textContent = "";
    this.classList = new ClassList();
    this.listeners = {};
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  dispatch(type) {
    (this.listeners[type] || []).forEach((fn) =>
      fn({ preventDefault() {}, target: this, dataTransfer: null })
    );
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  appendChild(node) { this.children.push(node); }
  focus() { this.focused = true; }
  scrollIntoView() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get className() { return this.classList.value; }
  set className(v) { this.classList = new ClassList(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
}

const IDS = ["cfg", "productSel", "fileInput", "fileName", "dropZone", "analyzeBtn",
  "uploadMsg", "dsStatus", "reviewPanel", "reviewBody", "reviewHint",
  "selStat", "headCheck", "commitBtn", "commitMsg",
  "checkAll", "checkNone", "checkHigh"];

const els = {};
IDS.forEach((id) => { els[id] = new El(id); });
els.cfg.dataset = { product: "", minConf: "0.75", maxMb: "25", productTotal: "14" };

const fetchCalls = [];
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
  return Promise.resolve({
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify({ doc_id: "x", candidates: [], metrics: [], product: {}, pages: 1, chars: 10, metric_total: 0 })),
  });
};

// 加载被测脚本（IIFE，require 即执行并挂载监听）
require(path.join(__dirname, "..", "..", "app", "static", "datasheet.js"));

// ── 用例 ───────────────────────────────────────────────────
check("初始状态提示缺型号", () => {
  assert(els.dsStatus.textContent.indexOf("请先选择要导入的型号") >= 0,
    `状态栏内容不符：${els.dsStatus.textContent}`);
  assertEqual(els.analyzeBtn.getAttribute("aria-disabled"), "true", "按钮应标记为未就绪");
});

check("未选型号时点击给出反馈（原 bug：静默无反应）", () => {
  els.analyzeBtn.dispatch("click");
  assertEqual(els.uploadMsg.textContent, "请先选择要导入的型号", "点击后必须提示原因");
  assertEqual(fetchCalls.length, 0, "条件不满足时不应发请求");
});

check("只选型号时提示缺文件", () => {
  els.productSel.value = "3";
  els.productSel.dispatch("change");
  assert(els.dsStatus.textContent.indexOf("请选择规格书文件") >= 0,
    `状态栏应提示选文件，实际：${els.dsStatus.textContent}`);
  els.analyzeBtn.dispatch("click");
  assertEqual(els.uploadMsg.textContent, "请选择规格书文件（PDF / TXT / Markdown）", "应提示选文件");
  assertEqual(fetchCalls.length, 0, "仍不应发请求");
});

check("型号与文件齐备后进入就绪", () => {
  els.fileInput.files = [{ name: "mzb.pdf", size: 2048 }];
  els.fileInput.dispatch("change");
  assert(els.dsStatus.textContent.indexOf("已就绪") >= 0,
    `应显示就绪，实际：${els.dsStatus.textContent}`);
  assertEqual(els.analyzeBtn.getAttribute("aria-disabled"), "false", "按钮应变为可用");
});

check("就绪后点击会发起识别请求", () => {
  const before = fetchCalls.length;
  els.analyzeBtn.dispatch("click");
  assertEqual(fetchCalls.length, before + 1, "应发出 1 次请求");
  assertEqual(fetchCalls[0].url, "/api/datasheet/analyze", "请求地址不对");
  assertEqual(fetchCalls[0].opts.headers["X-CSRF-Token"], "csrf-token-stub", "必须带 CSRF 头");
  assertEqual(els.uploadMsg.textContent, "正在解析文档并匹配指标…", "应显示进度提示");
});

check("超大文件被拦截并说明原因", () => {
  els.fileInput.files = [{ name: "huge.pdf", size: 30 * 1024 * 1024 }];
  els.fileInput.dispatch("change");
  els.analyzeBtn.dispatch("click");
  assert(els.uploadMsg.textContent.indexOf("超过上限 25 MB") >= 0,
    `应提示超限，实际：${els.uploadMsg.textContent}`);
});

check("无产品时给出前置提示", () => {
  // 重新加载一份，productTotal=0
  delete require.cache[require.resolve(path.join(__dirname, "..", "..", "app", "static", "datasheet.js"))];
  els.cfg.dataset.productTotal = "0";
  els.productSel.value = "";
  require(path.join(__dirname, "..", "..", "app", "static", "datasheet.js"));
  assert(els.dsStatus.textContent.indexOf("还没有产品") >= 0,
    `应提示先导入产品，实际：${els.dsStatus.textContent}`);
});

// ── 输出 ───────────────────────────────────────────────────
results.forEach(([status, name, msg]) => {
  console.log(`  ${status}  ${name}${msg ? "  " + msg : ""}`);
});
console.log(`\n${results.length - failures}/${results.length} passed`);
process.exit(failures ? 1 : 0);
