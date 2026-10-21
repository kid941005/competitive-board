/* 参数对比页交互：型号勾选、行筛选、溯源浮层。
 * 保持零依赖 —— P0 阶段不引入构建链，只做这几件必要的事。 */
(function () {
  "use strict";

  var cfg = document.getElementById("cfg");
  var MAX_COMPARE = Number((cfg && cfg.dataset.max) || 8);
  var maxCompare = MAX_COMPARE;

  // ── 滚动位置记忆 ─────────────────────────────────────────────
  var wrap = document.getElementById("tableWrap");
  var SCROLL_KEY = "cib-matrix-scroll";
  var CHIP_Q_KEY = "cib-chip-q";
  if (wrap) {
    var saved = sessionStorage.getItem(SCROLL_KEY);
    if (saved) {
      wrap.scrollTop = Number(saved) || 0;
      sessionStorage.removeItem(SCROLL_KEY);
    }
  }

  // ── 型号勾选 ─────────────────────────────────────────────────
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip"));
  var selCount = document.getElementById("selCount");
  var hint = document.querySelector(".picker-foot .hint");

  function selectedIds() {
    return chips.filter(function (c) { return c.classList.contains("on"); })
      .map(function (c) { return c.dataset.pid; });
  }

  function syncChipState() {
    var n = selectedIds().length;
    if (selCount) selCount.textContent = String(n);
    chips.forEach(function (c) {
      c.disabled = !c.classList.contains("on") && n >= maxCompare;
    });
  }

  function gotoSelection(ids) {
    if (wrap) sessionStorage.setItem(SCROLL_KEY, String(wrap.scrollTop));
    // 勾选会整页刷新，把当前搜索词带上，否则每选一个型号都要重输。
    // 这里按 id 取而不是用上面的变量：chipSearch 的赋值在这个函数之后，
    // 只靠 var 提升能跑通但读起来是隐患。
    var searchBox = document.getElementById("chipSearch");
    if (searchBox) sessionStorage.setItem(CHIP_Q_KEY, searchBox.value);
    var url = new URL(window.location.href);
    url.searchParams.set("p", ids.join(","));
    url.searchParams.delete("only");
    window.location.href = url.toString();
  }

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var ids = selectedIds();
      var pid = chip.dataset.pid;
      var isOn = chip.classList.contains("on");

      if (isOn) {
        if (ids.length <= 1) {
          flash(hint, "至少保留一个型号");
          return;
        }
        ids = ids.filter(function (x) { return x !== pid; });
      } else {
        if (ids.length >= maxCompare) {
          flash(hint, "最多同时对比 " + maxCompare + " 个型号");
          return;
        }
        ids.push(pid);
      }
      gotoSelection(ids);
    });
  });

  var pickSelf = document.getElementById("pickSelf");
  if (pickSelf) {
    pickSelf.addEventListener("click", function () {
      // 用行的 data-kind 判断归属：自家公司不渲染「友商」徽标，
      // 不能再靠徽标元素的 class 来识别
      var selfChips = chips.filter(function (c) {
        var row = c.closest(".ent-row");
        if (row && row.dataset && row.dataset.kind) {
          return row.dataset.kind === "self";
        }
        var kind = row && row.querySelector(".ent-kind");
        return !!kind && kind.classList.contains("self");
      });
      var ids = selfChips.map(function (c) { return c.dataset.pid; });
      if (ids.length) gotoSelection(ids);
    });
  }

  function flash(node, msg) {
    if (!node) return;
    var old = node.textContent;
    node.textContent = msg;
    node.style.color = "var(--warn)";
    setTimeout(function () {
      node.textContent = old;
      node.style.color = "";
    }, 1600);
  }

  syncChipState();

  // ── 选择器折叠 ───────────────────────────────────────────────
  // 默认收起，把屏幕让给参数表；但同一次会话内记住展开状态 ——
  // 勾选型号是整页刷新，否则每点一个型号都要重新点开。
  var pickerBody = document.getElementById("pickerBody");
  var pickerToggle = document.getElementById("pickerToggle");
  var PICKER_KEY = "cib.picker.open";

  function setPickerOpen(open) {
    if (!pickerBody || !pickerToggle) return;
    pickerBody.hidden = !open;
    pickerToggle.setAttribute("aria-expanded", open ? "true" : "false");
    var label = pickerToggle.querySelector(".toggle-text");
    if (label) label.textContent = open ? "收起选择" : "展开选择";
    try { sessionStorage.setItem(PICKER_KEY, open ? "1" : "0"); } catch (e) { /* 隐私模式下忽略 */ }
  }

  if (pickerBody && pickerToggle) {
    var pickerWasOpen = null;
    try { pickerWasOpen = sessionStorage.getItem(PICKER_KEY); } catch (e) { /* 同上 */ }
    if (pickerWasOpen === "1") setPickerOpen(true);
    pickerToggle.addEventListener("click", function () {
      setPickerOpen(pickerBody.hidden);
    });
  }

  // ── 型号搜索 ─────────────────────────────────────────────────
  // 型号一多（几十个往上）靠肉眼在 chips 里找是不现实的。这里只做本地过滤：
  // 数据已经渲染在页面上，不需要请求后端，也不会打断勾选状态。
  var chipSearch = document.getElementById("chipSearch");
  var pickerEmpty = document.getElementById("pickerEmpty");
  var filter = window.CIBFilter || { matches: function () { return true; } };

  function applyChipFilter() {
    if (!chipSearch) return;
    var q = chipSearch.value;
    var entRows = Array.prototype.slice.call(document.querySelectorAll(".ent-row"));
    var visible = 0;

    entRows.forEach(function (row) {
      var any = false;
      Array.prototype.forEach.call(row.querySelectorAll(".chip"), function (chip) {
        var hit = filter.matches(chip.getAttribute("data-haystack") || "", q);
        chip.hidden = !hit;
        if (hit) { any = true; visible += 1; }
      });
      row.hidden = !any;
    });

    // 大类分组标题：整组都过滤掉了，标题也不该留在页面上（否则会出现空标题）
    Array.prototype.forEach.call(document.querySelectorAll(".cat-group"), function (group) {
      var rows = Array.prototype.slice.call(group.querySelectorAll(".ent-row"));
      group.hidden = rows.every(function (r) { return r.hidden; });
    });

    if (pickerEmpty) pickerEmpty.hidden = !(q.trim() && visible === 0);
    var hint = document.getElementById("pickHint");
    if (hint) {
      var sel = selectedIds().length;
      hint.innerHTML = q.trim()
        ? "匹配 <b>" + visible + "</b> 个 · 已选 <b>" + sel + "</b> / 最多 " + maxCompare
        : "已选 <b>" + sel + "</b> / 最多 " + maxCompare;
    }
  }

  if (chipSearch) {
    var savedQ = sessionStorage.getItem(CHIP_Q_KEY);
    if (savedQ) {
      chipSearch.value = savedQ;
      sessionStorage.removeItem(CHIP_Q_KEY);
      applyChipFilter();
    }
    chipSearch.addEventListener("input", applyChipFilter);
    chipSearch.addEventListener("search", applyChipFilter);   // 点 × 清空时也触发
    document.addEventListener("keydown", function (e) {
      var tag = (e.target && e.target.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !typing) {
        e.preventDefault();
        if (pickerBody && pickerBody.hidden) setPickerOpen(true);
        chipSearch.focus();
      } else if (e.key === "Escape" && document.activeElement === chipSearch) {
        chipSearch.value = "";
        applyChipFilter();
      }
    });
  }

  // ── 行筛选 ───────────────────────────────────────────────────
  var tbody = document.querySelector("#matrix tbody");
  var onlyDiff = document.getElementById("onlyDiff");
  var onlyMissing = document.getElementById("onlyMissing");
  var search = document.getElementById("search");
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var emptyBox = document.getElementById("tableEmpty");

  var state = { cat: "", onlyDiff: false, onlyMissing: false, q: "" };

  if (onlyDiff) state.onlyDiff = onlyDiff.checked;
  if (onlyMissing) state.onlyMissing = onlyMissing.checked;

  function rowVisible(tr) {
    if (state.cat && tr.dataset.cat !== state.cat) return false;
    if (state.onlyDiff && tr.dataset.diff !== "1") return false;
    if (state.onlyMissing && tr.dataset.missing !== "1") return false;
    if (state.q && (tr.dataset.name || "").indexOf(state.q) === -1) return false;
    return true;
  }

  function applyFilters() {
    if (!tbody) return;
    var pendingGroup = null;
    var groupHasVisible = false;
    var visibleRows = 0;

    Array.prototype.forEach.call(tbody.children, function (tr) {
      if (tr.classList.contains("group-row")) {
        if (pendingGroup) pendingGroup.hidden = !groupHasVisible;
        pendingGroup = tr;
        groupHasVisible = false;
        // 分组行受类别筛选约束，但可被后续可见行救活
        tr.hidden = !!(state.cat && tr.dataset.cat !== state.cat);
        return;
      }
      var vis = rowVisible(tr);
      tr.hidden = !vis;
      if (vis) {
        visibleRows += 1;
        groupHasVisible = true;
        if (pendingGroup && !(state.cat && pendingGroup.dataset.cat !== state.cat)) {
          pendingGroup.hidden = false;
        }
      }
    });

    if (pendingGroup) pendingGroup.hidden = !groupHasVisible;

    if (emptyBox) emptyBox.hidden = visibleRows > 0;
    if (wrap) wrap.style.display = visibleRows > 0 ? "" : "none";
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (t) { t.classList.remove("on"); });
      tab.classList.add("on");
      state.cat = tab.dataset.cat || "";
      applyFilters();
    });
  });

  if (onlyDiff) {
    onlyDiff.addEventListener("change", function () {
      state.onlyDiff = onlyDiff.checked;
      applyFilters();
    });
  }
  if (onlyMissing) {
    onlyMissing.addEventListener("change", function () {
      state.onlyMissing = onlyMissing.checked;
      applyFilters();
    });
  }
  if (search) {
    var timer = null;
    search.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        state.q = search.value.trim().toLowerCase();
        applyFilters();
      }, 120);
    });
  }

  applyFilters();

  // ── 溯源浮层 ─────────────────────────────────────────────────
  var pop = document.getElementById("popover");
  var popTitle = document.getElementById("popTitle");
  var popBody = document.getElementById("popBody");
  var popClose = document.getElementById("popClose");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (m) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }

  function row(label, value, cls) {
    return "<div><dt>" + label + "</dt><dd" + (cls ? ' class="' + cls + '"' : "") + ">" + value + "</dd></div>";
  }

  function openPopover(cell) {
    var d = cell.dataset;
    popTitle.textContent = d.product + " · " + d.metric;

    var parts = [];
    parts.push(row("取值", esc(d.value) + (d.unit ? " " + esc(d.unit) : "")));
    if (d.cond) parts.push(row("测试条件", esc(d.cond)));

    var conf = Number(d.confidence || 0);
    parts.push(
      row(
        "置信度",
        (conf * 100).toFixed(0) + "%",
        conf >= 0.9 ? "pill-ok" : "pill-warn"
      )
    );
    parts.push(
      row(
        "复核",
        d.verified === "1"
          ? '<span class="pill-ok">已人工确认</span>'
          : '<span class="pill-warn">未复核</span>'
      )
    );
    if (d.captured) parts.push(row("抓取时间", esc(d.captured.replace("T", " "))));

    // 只渲染 http(s) 与站内相对链接：来源链接来自导入表格，历史数据里可能存着
    // javascript: 之类的东西，直接拼进 href 就等于在管理者点「来源」时执行它。
    var srcUrl = String(d.srcUrl || "");
    if (srcUrl && !/^https?:\/\//i.test(srcUrl) && srcUrl.charAt(0) !== "/") {
      srcUrl = "";
    }
    if (srcUrl) {
      parts.push(
        row(
          "来源",
          '<a href="' + esc(srcUrl) + '" target="_blank" rel="noopener noreferrer">' +
            esc(d.srcTitle || srcUrl) + "</a>"
        )
      );
    } else {
      parts.push(row("来源", '<span class="pill-warn">未登记</span>'));
    }

    popBody.innerHTML = parts.join("");
    pop.hidden = false;

    var rect = cell.getBoundingClientRect();
    var pw = 300;
    var left = rect.left + window.scrollX + Math.min(rect.width / 2, 40);
    var maxLeft = window.scrollX + document.documentElement.clientWidth - pw - 12;
    left = Math.max(12 + window.scrollX, Math.min(left, maxLeft));
    var top = rect.bottom + window.scrollY + 6;

    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function closePopover() {
    pop.hidden = true;
  }

  document.addEventListener("click", function (e) {
    var cell = e.target.closest ? e.target.closest(".cell[data-trace]") : null;
    if (cell) {
      e.stopPropagation();
      openPopover(cell);
      return;
    }
    if (pop && !pop.hidden && !e.target.closest("#popover")) closePopover();
  });

  if (popClose) popClose.addEventListener("click", closePopover);

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closePopover();
  });

  if (wrap) {
    wrap.addEventListener("scroll", function () {
      if (pop && !pop.hidden) closePopover();
    }, { passive: true });
  }
})();
