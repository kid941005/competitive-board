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
      var selfChips = chips.filter(function (c) {
        var row = c.closest(".ent-row");
        var kind = row && row.querySelector(".ent-kind");
        return kind && kind.classList.contains("self");
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

    if (d.srcUrl) {
      parts.push(
        row(
          "来源",
          '<a href="' + esc(d.srcUrl) + '" target="_blank" rel="noopener noreferrer">' +
            esc(d.srcTitle || d.srcUrl) + "</a>"
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
