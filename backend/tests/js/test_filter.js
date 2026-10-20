/* 关键词过滤的纯逻辑测试（无浏览器、无 DOM）。

这段逻辑现在同时服务对比页型号选择器与产品管理页，两边各写一份迟早会走样，
所以抽成 filter.js 并在这里钉住语义：
  1. 大小写不敏感
  2. 空格分隔多词 = AND（"村田 MZB" 两个词都得出现）
  3. 空串 / 空白 = 不过滤
  4. % 与 _ 在前端只是普通字符（转义是后端 LIKE 的事，这里不能自己造规则）
  5. 中文按子串匹配（不分词，也不需要）

跑法：node tests/js/test_filter.js
*/

"use strict";

const path = require("path");

const results = [];
let failures = 0;

function check(name, fn) {
  try {
    fn();
    results.push(["PASS", name]);
  } catch (e) {
    failures += 1;
    results.push(["FAIL", name, e.message]);
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

// 载入被测模块（它挂在 window 上）
global.window = {};
require(path.join(__dirname, "..", "..", "app", "static", "filter.js"));
const F = global.window.CIBFilter;

const HAY = "村田制作所 MZB1001T02 MZB1001T02 微型气泵（流量型）";

check("模块导出 tokens / matches / haystackOf", () => {
  assert(F && typeof F.matches === "function", "CIBFilter.matches 未导出");
  assert(typeof F.tokens === "function", "tokens 未导出");
  assert(typeof F.haystackOf === "function", "haystackOf 未导出");
});

check("空查询不过滤", () => {
  assert(F.matches(HAY, "") === true, "空串应当视为不过滤");
  assert(F.matches(HAY, "   ") === true, "纯空白应当视为不过滤");
  assert(F.matches(HAY, null) === true, "null 应当视为不过滤");
});

check("按公司名 / 型号 / 名称都能命中", () => {
  assert(F.matches(HAY, "村田") === true, "公司名未命中");
  assert(F.matches(HAY, "mzb1001") === true, "型号（小写）未命中");
  assert(F.matches(HAY, "微型气泵") === true, "产品名未命中");
});

check("大小写不敏感", () => {
  assert(F.matches("Murata MP6-GAS", "mp6") === true, "小写查询未命中大写型号");
  assert(F.matches("Murata MP6-GAS", "MURATA") === true, "大写查询未命中");
});

check("多词是 AND，不是 OR", () => {
  assert(F.matches(HAY, "村田 MZB") === true, "两个词都命中却返回 false");
  assert(F.matches(HAY, "村田 不存在") === false, "只命中一个词不该算匹配");
  assert(F.matches(HAY, "村田 MZB 微型") === true, "三个词全中应命中");
});

check("% 与 _ 在前端没有通配含义", () => {
  assert(F.matches(HAY, "%") === false, "% 不该匹配一切");
  assert(F.matches(HAY, "_") === false, "_ 不该匹配任意字符");
  assert(F.matches("流量 100%", "%") === true, "文本里真有 % 时应按字面命中");
});

check("haystackOf 读 data-haystack", () => {
  assert(F.haystackOf({ dataset: { haystack: "abc" } }) === "abc", "dataset 读取失败");
  assert(F.haystackOf({ getAttribute: () => "xyz" }) === "xyz", "getAttribute 读取失败");
  assert(F.haystackOf(null) === "", "空元素应返回空串");
});

check("tokens 按空白切分并小写", () => {
  const t = F.tokens("  村田   MZB  ");
  assert(t.length === 2 && t[0] === "村田" && t[1] === "mzb", `实际 ${JSON.stringify(t)}`);
  assert(F.tokens("").length === 0, "空串应切成 0 个词");
});

// ── 输出 ───────────────────────────────────────────────────
results.forEach(([state, name, detail]) => {
  console.log(`  ${state}  ${name}${detail ? "  " + detail : ""}`);
});
console.log(`\n${results.length - failures}/${results.length} passed`);
process.exit(failures ? 1 : 0);
