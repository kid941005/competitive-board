/* 关键词过滤的纯逻辑（对比页选择器 / 产品管理页共用）。零依赖。
 *
 * 只做两件事：把输入切成词、判断一段文本是否命中。DOM 隐藏交给调用方 ——
 * 这样这段逻辑可以脱离浏览器直接测（tests/js/test_filter.js）。
 *
 * 语义与后端 app/search.py 保持一致：
 *   - 大小写不敏感
 *   - 空格分隔多个词，词之间是 AND（"村田 MZB" 两个词都得出现）
 *   - 空串 = 不过滤
 *   - % 与 _ 在这里没有特殊含义（后端做了 LIKE 转义，前端是纯子串匹配）
 */
(function (global) {
  "use strict";

  function tokens(q) {
    return String(q == null ? "" : q)
      .toLowerCase()
      .split(/\s+/)
      .filter(function (t) { return t.length > 0; });
  }

  /** haystack: 预先拼好的「公司 + 型号 + 名称」文本；q: 用户输入 */
  function matches(haystack, q) {
    var words = tokens(q);
    if (!words.length) return true;
    var text = String(haystack == null ? "" : haystack).toLowerCase();
    for (var i = 0; i < words.length; i++) {
      if (text.indexOf(words[i]) < 0) return false;
    }
    return true;
  }

  /** 从元素上取待匹配文本（模板里写成 data-haystack） */
  function haystackOf(el) {
    if (!el) return "";
    if (el.dataset && el.dataset.haystack != null) return el.dataset.haystack;
    return el.getAttribute ? el.getAttribute("data-haystack") || "" : "";
  }

  global.CIBFilter = { tokens: tokens, matches: matches, haystackOf: haystackOf };
})(typeof window !== "undefined" ? window : globalThis);
