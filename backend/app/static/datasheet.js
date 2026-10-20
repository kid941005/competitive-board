/* 规格书导入 / 复核页交互。零依赖。 */
(function () {
  "use strict";

  var cfg = document.getElementById("cfg");
  var productSel = document.getElementById("productSel");
  var fileInput = document.getElementById("fileInput");
  var fileName = document.getElementById("fileName");
  var dropZone = document.getElementById("dropZone");
  var analyzeBtn = document.getElementById("analyzeBtn");
  var uploadMsg = document.getElementById("uploadMsg");

  var reviewPanel = document.getElementById("reviewPanel");
  var reviewBody = document.getElementById("reviewBody");
  var reviewHint = document.getElementById("reviewHint");
  var selStat = document.getElementById("selStat");
  var headCheck = document.getElementById("headCheck");
  var commitBtn = document.getElementById("commitBtn");
  var commitMsg = document.getElementById("commitMsg");

  var state = { file: null, docId: null, productId: null, metrics: [], metricMap: {} };

  if (cfg && cfg.dataset.product) {
    productSel.value = cfg.dataset.product;
  }

  function refreshBtn() {
    analyzeBtn.disabled = !(state.file && productSel.value);
  }

  // ── 文件选择 ────────────────────────────────────────────────
  function setFile(f) {
    state.file = f || null;
    fileName.textContent = f ? f.name + "（" + (f.size / 1024).toFixed(0) + " KB）" : "点击选择文件，或拖拽到此处";
    dropZone.classList.toggle("has-file", !!f);
    refreshBtn();
  }

  fileInput.addEventListener("change", function () {
    setFile(fileInput.files && fileInput.files[0]);
  });
  productSel.addEventListener("change", refreshBtn);

  ["dragenter", "dragover"].forEach(function (ev) {
    dropZone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropZone.classList.add("drag");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    dropZone.addEventListener(ev, function (e) {
      e.preventDefault();
      dropZone.classList.remove("drag");
    });
  });
  dropZone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
  });

  // ── 识别 ────────────────────────────────────────────────────
  analyzeBtn.addEventListener("click", function () {
    if (!state.file || !productSel.value) return;
    var fd = new FormData();
    fd.append("file", state.file);
    fd.append("product_id", productSel.value);

    analyzeBtn.disabled = true;
    uploadMsg.className = "ds-msg";
    uploadMsg.textContent = "正在解析文档并匹配指标…";

    fetch("/api/datasheet/analyze", { method: "POST", body: fd })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || "识别失败");
          return body;
        });
      })
      .then(render)
      .catch(function (err) {
        uploadMsg.className = "ds-msg warn";
        uploadMsg.textContent = "识别失败：" + err.message;
      })
      .finally(function () {
        refreshBtn();
      });
  });

  // ── 渲染复核表 ──────────────────────────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (m) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }

  function confClass(c) {
    if (c >= 0.85) return "high";
    if (c >= 0.7) return "mid";
    return "low";
  }

  function render(data) {
    state.docId = data.doc_id;
    state.productId = data.product.id;
    state.metrics = data.metrics || [];
    state.metricMap = {};
    state.metrics.forEach(function (m) { state.metricMap[m.key] = m; });

    uploadMsg.className = "ds-msg ok";
    uploadMsg.textContent =
      "已解析 " + data.pages + " 页 / " + data.chars + " 字符，在 " + data.metric_total +
      " 个适用指标中识别出 " + data.candidates.length + " 项。";

    reviewBody.innerHTML = "";
    if (!data.candidates.length) {
      reviewHint.textContent = "没有识别到任何参数。可能是扫描版 PDF（图片）或版式特殊。";
      reviewPanel.hidden = false;
      updateSelStat();
      return;
    }

    data.candidates.forEach(function (c, i) {
      var tr = document.createElement("tr");
      tr.dataset.conf = String(c.confidence);
      tr.dataset.key = c.metric_key;

      var opts = state.metrics.map(function (m) {
        return '<option value="' + esc(m.key) + '"' + (m.key === c.metric_key ? " selected" : "") + ">" +
          esc(m.name) + (m.unit ? " · " + esc(m.unit) : "") + "</option>";
      }).join("");

      var metric = state.metricMap[c.metric_key] || {};
      var valueText = c.value_num !== null && c.value_num !== undefined ? String(c.value_num) : (c.value_text || "");

      tr.innerHTML =
        '<td class="ds-check"><input type="checkbox" class="row-check"' +
          (c.confidence >= 0.75 ? " checked" : "") + "></td>" +
        '<td><select class="metric-sel">' + opts + "</select></td>" +
        '<td><input class="val-input" value="' + esc(valueText) + '"></td>' +
        '<td class="unit-cell">' + esc(metric.unit || c.unit || "") + "</td>" +
        '<td><input class="cond-input" value="' + esc(c.condition || "") + '" placeholder="—"></td>' +
        "<td>" + (c.page || "") + "</td>" +
        '<td><span class="conf-badge ' + confClass(c.confidence) + '">' +
          Math.round(c.confidence * 100) + "%</span>" +
          (c.converted ? ' <span class="tag-conv" title="单位已按指标标准单位换算">换算</span>' : "") +
          (c.qualifier ? ' <span class="tag-warn" title="标签后紧跟“型/范围”等修饰语，可能误配">存疑</span>' : "") +
          (c.hits > 1 ? ' <span class="tag-hit" title="文档中多处出现，仅取置信度最高的一处">×' + c.hits + "</span>" : "") +
        "</td>" +
        '<td class="ctx" title="' + esc(c.context) + '">' + esc(c.context || "") + "</td>";

      reviewBody.appendChild(tr);

      tr.querySelector(".metric-sel").addEventListener("change", function (e) {
        var m = state.metricMap[e.target.value] || {};
        tr.querySelector(".unit-cell").textContent = m.unit || "";
      });
      tr.querySelector(".row-check").addEventListener("change", updateSelStat);
    });

    reviewHint.textContent = data.product.category_label + " · " + data.product.model + " · " + data.filename;
    reviewPanel.hidden = false;
    updateSelStat();
    reviewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ── 选择统计 ────────────────────────────────────────────────
  function rows() {
    return Array.prototype.slice.call(reviewBody.querySelectorAll("tr"));
  }

  function checkedRows() {
    return rows().filter(function (tr) { return tr.querySelector(".row-check").checked; });
  }

  function updateSelStat() {
    var all = rows();
    if (!all.length) {
      selStat.textContent = "";
      headCheck.checked = false;
      return;
    }
    var n = checkedRows().length;
    selStat.textContent = "已选 " + n + " / " + all.length + " 项";
    headCheck.checked = n === all.length;
    headCheck.indeterminate = n > 0 && n < all.length;
  }

  function setAll(predicate) {
    rows().forEach(function (tr) {
      tr.querySelector(".row-check").checked = predicate(tr);
    });
    updateSelStat();
  }

  headCheck.addEventListener("change", function () {
    setAll(function () { return headCheck.checked; });
  });
  document.getElementById("checkAll").addEventListener("click", function () {
    setAll(function () { return true; });
  });
  document.getElementById("checkNone").addEventListener("click", function () {
    setAll(function () { return false; });
  });
  document.getElementById("checkHigh").addEventListener("click", function () {
    setAll(function (tr) { return Number(tr.dataset.conf) >= 0.8; });
  });

  // ── 提交 ────────────────────────────────────────────────────
  commitBtn.addEventListener("click", function () {
    if (!state.docId) return;
    var picked = checkedRows();
    if (!picked.length) {
      commitMsg.className = "ds-msg warn";
      commitMsg.textContent = "请至少勾选一项。";
      return;
    }

    var payloadRows = [];
    var bad = 0;
    picked.forEach(function (tr) {
      var key = tr.querySelector(".metric-sel").value;
      var raw = tr.querySelector(".val-input").value.trim();
      var metric = state.metricMap[key] || {};
      var isNum = (metric.value_type || "number") === "number";
      if (!raw) { bad += 1; return; }
      var num = Number(raw);
      if (isNum && (raw === "" || isNaN(num))) { bad += 1; return; }
      payloadRows.push({
        metric_key: key,
        value_num: isNum ? num : null,
        value_text: isNum ? null : raw,
        condition: tr.querySelector(".cond-input").value.trim() || null,
        page: Number(tr.children[5].textContent) || null,
        confidence: Number(tr.dataset.conf) || 0.7
      });
    });

    if (!payloadRows.length) {
      commitMsg.className = "ds-msg warn";
      commitMsg.textContent = "勾选的行里没有有效数值，请检查。";
      return;
    }

    commitBtn.disabled = true;
    commitMsg.className = "ds-msg";
    commitMsg.textContent = "正在写入…";

    fetch("/api/datasheet/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        doc_id: state.docId,
        product_id: state.productId,
        rows: payloadRows
      })
    })
      .then(function (r) {
        return r.json().then(function (body) {
          if (!r.ok) throw new Error(body.detail || "写入失败");
          return body;
        });
      })
      .then(function (res) {
        var parts = [];
        if (res.written) parts.push("新增 " + res.written + " 项");
        if (res.updated) parts.push("更新 " + res.updated + " 项");
        if (res.skipped) parts.push("跳过 " + res.skipped + " 项");
        if (bad) parts.push("无效 " + bad + " 项");
        commitMsg.className = "ds-msg ok";
        commitMsg.innerHTML = esc(res.product) + " 写入完成：" + parts.join("，") +
          "。 <a href=\"/compare\">去对比页核对</a>";
        if (res.errors && res.errors.length) {
          commitMsg.innerHTML += '<br><span class="ds-err">' + esc(res.errors.join("；")) + "</span>";
        }
        commitBtn.disabled = false;
      })
      .catch(function (err) {
        commitMsg.className = "ds-msg warn";
        commitMsg.textContent = "写入失败：" + err.message;
        commitBtn.disabled = false;
      });
  });
})();
