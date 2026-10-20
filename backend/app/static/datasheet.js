/* 规格书导入 / 复核页交互。零依赖。
 *
 * 导入的定位是**扩充数据库**：公司与型号都允许不存在，提交时就地建档。
 * 所以入口不是「先选一个已有型号」，而是「公司 → 大类 → 型号」三格信息，
 * 型号能对上已有档案就走更新，对不上就新建。
 */
(function () {
  "use strict";

  var cfg = document.getElementById("cfg");
  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var MIN_CONF = Number((cfg && cfg.dataset.minConf) || 0.75);
  var MAX_MB = Number((cfg && cfg.dataset.maxMb) || 25);

  var companySel = document.getElementById("companySel");
  var newCompanyWrap = document.getElementById("newCompanyWrap");
  var newCompanyInput = document.getElementById("newCompanyInput");
  var categorySel = document.getElementById("categorySel");
  var modelInput = document.getElementById("modelInput");
  var resolveHint = document.getElementById("resolveHint");
  var forceWrap = document.getElementById("forceWrap");
  var forceNew = document.getElementById("forceNewCompany");

  var fileInput = document.getElementById("fileInput");
  var fileName = document.getElementById("fileName");
  var dropZone = document.getElementById("dropZone");
  var analyzeBtn = document.getElementById("analyzeBtn");
  var uploadMsg = document.getElementById("uploadMsg");
  var dsStatus = document.getElementById("dsStatus");

  var reviewPanel = document.getElementById("reviewPanel");
  var reviewBody = document.getElementById("reviewBody");
  var reviewHint = document.getElementById("reviewHint");
  var selStat = document.getElementById("selStat");
  var headCheck = document.getElementById("headCheck");
  var commitBtn = document.getElementById("commitBtn");
  var commitMsg = document.getElementById("commitMsg");

  var state = { file: null, docId: null, target: null, metrics: [], metricMap: {} };

  // 已有型号索引：用于判断「这个型号在所选公司下是否已存在」（存在=更新，不存在=新建）
  var PRODUCTS = [];
  var indexEl = document.getElementById("productIndex");
  if (indexEl) {
    try {
      PRODUCTS = JSON.parse(indexEl.textContent || "[]") || [];
    } catch (e) {
      PRODUCTS = [];
    }
  }

  // 公司清单（从下拉选项读，便于与后端同一份数据）
  var COMPANIES = [];
  Array.prototype.forEach.call(companySel ? companySel.options : [], function (opt) {
    if (!/^\d+$/.test(opt.value)) return;
    COMPANIES.push({
      id: opt.value,
      name: String(opt.textContent || "").trim()
    });
  });

  // ── 名称归一：与后端 _norm_company 保持同一口径 ──────────────
  function normName(s) {
    return String(s || "").replace(/[\s\-_.·/\\()（）&]+/g, "").toLowerCase();
  }

  function isNewCompany() {
    return companySel && companySel.value === "__new__";
  }

  function companyId() {
    return companySel && /^\d+$/.test(companySel.value) ? companySel.value : null;
  }

  function companyName() {
    if (isNewCompany()) return String((newCompanyInput && newCompanyInput.value) || "").trim();
    var id = companyId();
    for (var i = 0; i < COMPANIES.length; i++) {
      if (COMPANIES[i].id === id) return COMPANIES[i].name;
    }
    return "";
  }

  function findCompanyByName(name) {
    var key = normName(name);
    if (!key) return null;
    for (var i = 0; i < COMPANIES.length; i++) {
      if (normName(COMPANIES[i].name) === key) return COMPANIES[i];
    }
    return null;
  }

  function similarCompany(name) {
    var key = normName(name);
    if (key.length < 2) return null;
    for (var i = 0; i < COMPANIES.length; i++) {
      var other = normName(COMPANIES[i].name);
      if (!other) continue;
      if (key.indexOf(other) >= 0 || other.indexOf(key) >= 0) return COMPANIES[i];
    }
    return null;
  }

  function existingModel(entityId, model) {
    var key = String(model || "").trim().toLowerCase();
    if (!entityId || !key) return null;
    for (var i = 0; i < PRODUCTS.length; i++) {
      var p = PRODUCTS[i];
      if (String(p.entity_id) === String(entityId) &&
          String(p.model || "").trim().toLowerCase() === key) {
        return p;
      }
    }
    return null;
  }

  // ── 建档解析提示 ────────────────────────────────────────────
  // 作用：让用户在点「开始识别」之前就知道这一票是「更新」还是「新建」，
  // 避免上传解析之后才发现建错了公司/型号。
  function refreshResolve() {
    if (!resolveHint) return;
    var bits = [];
    var cls = "ds-resolve";

    if (!companySel || !companySel.value) {
      bits.push("选择公司，或点「＋ 新建公司…」");
    } else if (isNewCompany()) {
      if (newCompanyWrap) newCompanyWrap.hidden = false;
      var name = companyName();
      if (!name) {
        bits.push("填写新公司名称");
      } else {
        var same = findCompanyByName(name);
        var sim = same ? null : similarCompany(name);
        if (same) {
          bits.push("已有同名公司「" + same.name + "」，将直接归入它");
        } else if (sim) {
          cls += " warn";
          bits.push("与已有「" + sim.name + "」名称相近，提交时需确认是否为另一家");
        } else {
          bits.push("将新建公司「" + name + "」");
        }
      }
    } else {
      if (newCompanyWrap) newCompanyWrap.hidden = true;
      bits.push("公司「" + companyName() + "」");
    }

    var showForce = isNewCompany() && !!companyName() &&
      !findCompanyByName(companyName()) && !!similarCompany(companyName());
    if (forceWrap) {
      forceWrap.hidden = !showForce;
      // 不需要确认时必须清掉勾选，否则上一次的「确认新建」会悄悄带进下一次提交
      if (!showForce && forceNew) forceNew.checked = false;
    }

    if (!categorySel || !categorySel.value) {
      bits.push("选择产品大类");
    } else {
      var model = String((modelInput && modelInput.value) || "").trim();
      if (!model) {
        bits.push("填写型号");
      } else {
        var hit = existingModel(companyId(), model);
        if (!hit) {
          bits.push("将新建型号「" + model + "」");
        } else if (hit.category && hit.category !== categorySel.value) {
          cls += " warn";
          bits.push("该型号已存在但属于「" + categoryLabel(hit.category) + "」，提交会被拒绝");
        } else {
          bits.push("已有型号「" + model + "」，将更新其参数");
        }
      }
    }

    resolveHint.textContent = bits.join(" · ");
    resolveHint.className = cls;
  }

  function categoryLabel(code) {
    if (!categorySel) return code;
    for (var i = 0; i < categorySel.options.length; i++) {
      if (categorySel.options[i].value === code) return categorySel.options[i].textContent.trim();
    }
    return code;
  }

  // ── 就绪状态 ────────────────────────────────────────────────
  // 按钮不用 disabled：禁用态点击没有任何反馈，用户无法判断是「没填齐」还是「页面坏了」。
  function currentProblem() {
    if (!companySel || !companySel.value) return "请先选择公司（或新建公司）";
    if (isNewCompany() && !companyName()) return "请填写新公司名称";
    if (!categorySel || !categorySel.value) return "请选择产品大类";
    if (!String((modelInput && modelInput.value) || "").trim()) return "请填写型号";
    if (!state.file) return "请选择规格书文件（PDF / TXT / Markdown）";
    var mb = state.file.size / 1024 / 1024;
    if (mb > MAX_MB) return "文件 " + mb.toFixed(1) + " MB，超过上限 " + MAX_MB + " MB";
    return null;
  }

  function updateStatus() {
    var problem = currentProblem();
    if (dsStatus) {
      dsStatus.textContent = problem ? "待完成：" + problem : "已就绪，可以开始识别";
      dsStatus.className = "ds-status" + (problem ? "" : " ready");
    }
    if (analyzeBtn) analyzeBtn.setAttribute("aria-disabled", problem ? "true" : "false");
    return problem;
  }

  function flashNeed(el) {
    if (!el) return;
    el.classList.add("need-attention");
    setTimeout(function () {
      el.classList.remove("need-attention");
    }, 1600);
  }

  // ── 文件选择 ────────────────────────────────────────────────
  function setFile(f) {
    state.file = f || null;
    if (fileName) {
      fileName.textContent = f
        ? f.name + "（" + (f.size / 1024).toFixed(0) + " KB）"
        : "点击选择文件，或拖拽到此处";
    }
    if (dropZone) dropZone.classList.toggle("has-file", !!f);
    if (uploadMsg) uploadMsg.textContent = "";
    updateStatus();
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      setFile(fileInput.files && fileInput.files[0]);
    });
  }
  if (dropZone) {
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
  }

  ["change", "input"].forEach(function (ev) {
    if (companySel) companySel.addEventListener(ev, onChange);
    if (newCompanyInput) newCompanyInput.addEventListener(ev, onChange);
    if (categorySel) categorySel.addEventListener(ev, onChange);
    if (modelInput) modelInput.addEventListener(ev, onChange);
  });

  function onChange() {
    if (isNewCompany() && newCompanyWrap) newCompanyWrap.hidden = false;
    if (!isNewCompany() && newCompanyWrap) newCompanyWrap.hidden = true;
    if (uploadMsg) uploadMsg.textContent = "";
    refreshResolve();
    updateStatus();
  }

  refreshResolve();
  updateStatus();

  // ── 响应解析：非 JSON 也要给出人话 ──────────────────────────
  function readJson(res) {
    return res.text().then(function (text) {
      var data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (e) {
        data = null;
      }
      if (!res.ok) {
        var detail =
          (data && (data.detail || data.message)) ||
          (res.status === 401
            ? "登录已过期，请刷新页面重新登录"
            : res.status === 403
            ? "没有权限执行该操作"
            : "HTTP " + res.status);
        var err = new Error(typeof detail === "string" ? detail : "请求失败");
        err.status = res.status;
        throw err;
      }
      if (!data) throw new Error("服务端返回了非 JSON 响应（HTTP " + res.status + "）");
      return data;
    });
  }

  // ── 识别 ────────────────────────────────────────────────────
  if (analyzeBtn) {
    analyzeBtn.addEventListener("click", function () {
      var problem = currentProblem();
      if (problem) {
        uploadMsg.className = "ds-msg warn";
        uploadMsg.textContent = problem;
        if (!companySel.value) {
          companySel.focus();
          flashNeed(companySel);
        } else if (isNewCompany() && !companyName()) {
          newCompanyInput.focus();
          flashNeed(newCompanyInput);
        } else if (!categorySel.value) {
          categorySel.focus();
          flashNeed(categorySel);
        } else if (!String(modelInput.value || "").trim()) {
          modelInput.focus();
          flashNeed(modelInput);
        } else {
          flashNeed(dropZone);
          dropZone.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        return;
      }

      var fd = new FormData();
      fd.append("file", state.file);
      var cid = companyId();
      if (cid) fd.append("entity_id", cid);
      else fd.append("entity_name", companyName());
      fd.append("category", categorySel.value);
      fd.append("model", String(modelInput.value).trim());

      analyzeBtn.setAttribute("aria-disabled", "true");
      uploadMsg.className = "ds-msg";
      uploadMsg.textContent = "正在解析文档并匹配指标…";

      fetch("/api/datasheet/analyze", {
        method: "POST",
        body: fd,
        headers: { "X-CSRF-Token": CSRF }
      })
        .then(readJson)
        .then(render)
        .catch(function (err) {
          uploadMsg.className = "ds-msg warn";
          uploadMsg.textContent = "识别失败：" + err.message;
        })
        .finally(function () {
          updateStatus();
        });
    });
  }

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
    var t = data.target || {};
    state.docId = data.doc_id;
    state.target = t;
    state.metrics = data.metrics || [];
    state.metricMap = {};
    state.metrics.forEach(function (m) { state.metricMap[m.key] = m; });

    var created = [];
    if (t.entity_is_new) created.push("新建公司");
    if (t.product_is_new) created.push("新建型号");

    uploadMsg.className = data.warning ? "ds-msg warn" : "ds-msg ok";
    uploadMsg.textContent =
      "已解析 " + data.pages + " 页 / " + data.chars + " 字符，在 " + data.metric_total +
      " 个适用指标中识别出 " + data.candidates.length + " 项。" +
      (data.warning ? " " + data.warning : "");

    reviewBody.innerHTML = "";
    if (!data.candidates.length) {
      reviewHint.textContent = "没有识别到任何参数。可能是扫描版 PDF（图片）或版式特殊。";
      reviewPanel.hidden = false;
      updateSelStat();
      return;
    }

    data.candidates.forEach(function (c) {
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
          (c.confidence >= MIN_CONF ? " checked" : "") + "></td>" +
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

    reviewHint.textContent =
      t.entity_name + " · " + t.model + " · " + t.category_label +
      (created.length ? " · " + created.join("、") : " · 已有型号") +
      " · " + data.filename;
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

  if (headCheck) {
    headCheck.addEventListener("change", function () {
      setAll(function () { return headCheck.checked; });
    });
  }
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
  function commitPayload(rowsOut) {
    var t = state.target || {};
    return {
      doc_id: state.docId,
      target: {
        entity_id: t.entity_id || null,
        entity_name: t.entity_id ? null : t.entity_name,
        category: t.category,
        model: t.model,
        // 仅在用户显式勾选时置真：默认宁可拦一次，也不让同一家建成两份档案
        force_new_entity: !!(forceNew && forceNew.checked)
      },
      rows: rowsOut
    };
  }

  if (commitBtn) {
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
        if (isNum && isNaN(num)) { bad += 1; return; }
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
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify(commitPayload(payloadRows))
      })
        .then(readJson)
        .then(function (res) {
          var parts = [];
          if (res.entity_created) parts.push("新建公司「" + res.entity + "」");
          if (res.product_created) parts.push("新建型号「" + res.product + "」");
          if (res.written) parts.push("新增 " + res.written + " 项");
          if (res.updated) parts.push("更新 " + res.updated + " 项");
          if (res.skipped) parts.push("跳过 " + res.skipped + " 项");
          if (bad) parts.push("无效 " + bad + " 项");

          commitMsg.className = "ds-msg ok";
          commitMsg.innerHTML =
            esc(res.entity) + " · " + esc(res.product) + " · " + esc(res.category_label) + "：" +
            parts.join("，") + '。 <a href="/compare">去对比页核对</a>';
          if (res.errors && res.errors.length) {
            commitMsg.innerHTML += '<br><span class="ds-err">' + esc(res.errors.join("；")) + "</span>";
          }
          commitBtn.disabled = false;
        })
        .catch(function (err) {
          commitMsg.className = "ds-msg warn";
          commitMsg.textContent = "写入失败：" + err.message;
          if (err.status === 409 && forceWrap) {
            // 相似公司被拦：把「确认新建独立档案」露出来，让用户显式决定
            forceWrap.hidden = false;
            if (forceNew) forceNew.focus();
          }
          commitBtn.disabled = false;
        });
    });
  }
})();
