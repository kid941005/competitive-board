/* 批量导入页：上传 Excel / CSV，展示导入报告。零依赖。 */
(function () {
  "use strict";

  var CSRF = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  var form = document.getElementById("uploadForm");
  var fileInput = document.getElementById("fileInput");
  var fileName = document.getElementById("fileName");
  var dropZone = document.getElementById("dropZone");
  var btn = document.getElementById("uploadBtn");
  var status = document.getElementById("importStatus");
  var msg = document.getElementById("importMsg");
  var report = document.getElementById("report");
  var grid = document.getElementById("reportGrid");
  var errBox = document.getElementById("reportErrors");

  var picked = null;

  function setFile(f) {
    picked = f || null;
    fileName.textContent = f
      ? f.name + "（" + (f.size / 1024).toFixed(0) + " KB）"
      : "点击选择 Excel / CSV 文件，或拖拽到此处";
    dropZone.classList.toggle("has-file", !!f);
    msg.textContent = "";
    if (status) {
      status.textContent = f ? "已选择文件，点「开始导入」" : "待选择文件";
      status.className = "ds-status" + (f ? " ready" : "");
    }
  }

  fileInput.addEventListener("change", function () {
    setFile(fileInput.files && fileInput.files[0]);
  });

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

  function readJson(res) {
    return res.text().then(function (text) {
      var data = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch (e) {
        data = null;
      }
      if (!res.ok) {
        var detail = (data && (data.detail || data.message)) ||
          (res.status === 401 ? "登录已过期，请刷新页面" :
           res.status === 403 ? "没有权限执行该操作" : "HTTP " + res.status);
        throw new Error(typeof detail === "string" ? detail : "导入失败");
      }
      if (!data) throw new Error("服务端返回了非 JSON 响应（HTTP " + res.status + "）");
      return data;
    });
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (m) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m];
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!picked) {
      msg.className = "ds-msg warn";
      msg.textContent = "请先选择 .xlsx 或 .csv 文件";
      dropZone.scrollIntoView({ behavior: "smooth", block: "center" });
      dropZone.classList.add("need-attention");
      setTimeout(function () { dropZone.classList.remove("need-attention"); }, 1600);
      return;
    }

    var fd = new FormData();
    fd.append("file", picked);

    btn.disabled = true;
    msg.className = "ds-msg";
    msg.textContent = "正在导入…";
    report.hidden = true;

    fetch("/api/import", { method: "POST", body: fd, headers: { "X-CSRF-Token": CSRF } })
      .then(readJson)
      .then(function (r) {
        msg.className = "ds-msg ok";
        var bits = [];
        if (r.created_entities) bits.push("新建公司 " + r.created_entities);
        if (r.created_products) bits.push("新建型号 " + r.created_products);
        bits.push("写入参数 " + r.written_specs);
        if (r.skipped_missing) bits.push("数值为空跳过 " + r.skipped_missing);
        if (r.skipped_unchanged) bits.push("无变化跳过 " + r.skipped_unchanged);
        if (r.converted_units) bits.push("换算单位 " + r.converted_units);
        if ((r.needs_review || []).length) bits.push("单位待确认 " + r.needs_review.length);
        msg.innerHTML = "导入完成：" + esc(bits.join("，")) +
          '。<a href="/products">去产品管理复核 →</a>';

        var cells = [
          ["读取行数", r.rows_read],
          ["新建公司", r.created_entities],
          ["新建型号", r.created_products],
          ["写入参数", r.written_specs],
          ["数值为空跳过", r.skipped_missing],
          ["无变化跳过", r.skipped_unchanged || 0],
          ["单位换算", r.converted_units || 0],
          ["报错行数", (r.errors || []).length],
        ];
        grid.innerHTML = cells.map(function (c) {
          return '<div class="rcell"><span class="rk">' + esc(c[0]) + '</span>' +
                 '<span class="rv">' + esc(c[1]) + "</span></div>";
        }).join("");

        var html = "";
        if (r.errors && r.errors.length) {
          html += '<div class="rerr-head">以下行未写入（前 50 条）：</div><ul class="plainlist">' +
            r.errors.slice(0, 50).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
            "</ul>";
        } else {
          html += '<p class="dim">没有报错行。</p>';
        }
        if (r.conversions && r.conversions.length) {
          html += '<div class="rerr-head">已按单位换算（前 50 条）：</div><ul class="plainlist">' +
            r.conversions.slice(0, 50).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
            "</ul>";
        }
        if (r.needs_review && r.needs_review.length) {
          html += '<div class="rerr-head">单位口径需人工确认（值按原文保留）：</div>' +
            '<ul class="plainlist">' +
            r.needs_review.slice(0, 50).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("") +
            "</ul>";
        }
        errBox.innerHTML = html;
        report.hidden = false;
        report.scrollIntoView({ behavior: "smooth", block: "start" });
        btn.disabled = false;
      })
      .catch(function (err) {
        msg.className = "ds-msg warn";
        msg.textContent = "导入失败：" + err.message;
        btn.disabled = false;
      });
  });
})();
