/* 产品编辑页：复核勾选的批量操作 + 未保存提示。零依赖。 */
(function () {
  "use strict";

  var form = document.getElementById("specForm");
  if (!form) return;

  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[type="checkbox"]'));

  var all = document.getElementById("checkAllOk");
  var none = document.getElementById("checkNoneOk");
  if (all) all.addEventListener("click", function () {
    boxes.forEach(function (b) { b.checked = true; });
    markDirty();
  });
  if (none) none.addEventListener("click", function () {
    boxes.forEach(function (b) { b.checked = false; });
    markDirty();
  });

  var hint = document.getElementById("dirtyHint");
  var dirty = false;
  function markDirty() {
    dirty = true;
    if (hint) {
      hint.textContent = "有未保存的改动";
      hint.className = "hint warn-text";
    }
  }

  form.addEventListener("input", markDirty);
  form.addEventListener("change", markDirty);

  window.addEventListener("beforeunload", function (e) {
    if (!dirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
  form.addEventListener("submit", function () { dirty = false; });
})();
