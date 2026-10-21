/* 产品管理页：按关键词 / 公司 / 大类筛选表格。零依赖。
 *
 * 数据都在页面里（型号数量是几十到几百的量级），本地过滤比每次发请求快，
 * 也不会因为刷新丢失「刚编辑完的位置」。
 */
(function () {
  "use strict";

  var search = document.getElementById("prodSearch");
  var company = document.getElementById("prodCompany");
  var category = document.getElementById("prodCat");
  if (!search || !company || !category) return;

  var rows = Array.prototype.slice.call(document.querySelectorAll("tr[data-haystack]"));
  var groupRows = Array.prototype.slice.call(document.querySelectorAll("tr.grouprow"));
  var noMatch = document.getElementById("prodNoMatch");
  var hint = document.getElementById("prodFilterHint");
  var total = rows.length;
  var filter = (window.CIBFilter || { matches: function () { return true; } });

  function apply() {
    var q = search.value;
    var wantEnt = company.value;
    var wantCat = category.value;
    var shown = 0;

    rows.forEach(function (tr) {
      var ok = filter.matches(tr.getAttribute("data-haystack") || "", q);
      if (ok && wantEnt) ok = tr.getAttribute("data-ent") === wantEnt;
      if (ok && wantCat) ok = tr.getAttribute("data-cat") === wantCat;
      tr.hidden = !ok;
      if (ok) shown += 1;
    });

    // 大类分组标题：整组都被筛掉了就把标题一起藏起来，否则页面上全是空标题
    groupRows.forEach(function (head) {
      var anyVisible = false;
      var node = head.nextElementSibling;
      while (node && !node.classList.contains("grouprow")) {
        if (!node.hidden) { anyVisible = true; break; }
        node = node.nextElementSibling;
      }
      head.hidden = !anyVisible;
    });

    if (noMatch) noMatch.hidden = !(total > 0 && shown === 0);
    var filtering = !!q.trim() || !!wantEnt || !!wantCat;
    if (hint) {
      hint.hidden = !filtering;
      hint.textContent = filtering ? "匹配 " + shown + " / " + total + " 个型号" : "";
    }
  }

  [search, company, category].forEach(function (el) {
    el.addEventListener("input", apply);
    el.addEventListener("change", apply);
  });

  // 按 / 聚焦搜索框；Esc 清空并退出筛选（与对比页一致）
  document.addEventListener("keydown", function (e) {
    var tag = (e.target && e.target.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if (e.key === "/" && !typing) {
      e.preventDefault();
      search.focus();
    } else if (e.key === "Escape" && document.activeElement === search) {
      search.value = "";
      apply();
    }
  });
})();
