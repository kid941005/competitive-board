/* 主题切换：跟随系统 / 浅色 / 深色。
 *
 * 存储只有一个键 cib-theme，取值 auto | light | dark，缺省 auto。
 * <html data-theme> 始终是解析后的实际值（light/dark），供 app.css 选中令牌；
 * <html data-theme-pref> 保存用户原始选择，纯粹给调试/样式挂钩用。
 *
 * base.html 里有一段内联脚本在首个样式表之前先按同样的逻辑设一次，
 * 避免深色用户看到白底闪一下（FOUC）。这里的职责是：把下拉框同步成
 * 已存的选择、处理切换、以及系统外观变化时仅在「跟随系统」下重新解析。
 */
(function () {
  "use strict";

  var KEY = "cib-theme";
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function readPref() {
    try {
      var v = localStorage.getItem(KEY);
      return v === "dark" || v === "light" ? v : "auto";
    } catch (e) {
      return "auto";
    }
  }

  function resolve(pref) {
    if (pref === "dark") return "dark";
    if (pref === "light") return "light";
    return mq && mq.matches ? "dark" : "light";
  }

  function apply(pref) {
    var el = document.documentElement;
    el.setAttribute("data-theme", resolve(pref));
    el.setAttribute("data-theme-pref", pref);
  }

  function onReady() {
    var sel = document.getElementById("themeSelect");
    if (sel) {
      sel.value = readPref();
      sel.addEventListener("change", function () {
        var pref = sel.value === "dark" || sel.value === "light" ? sel.value : "auto";
        try {
          localStorage.setItem(KEY, pref);
        } catch (e) {
          /* 隐私模式下存储不可写，切换仍然生效，只是不记忆 */
        }
        apply(pref);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }

  if (mq) {
    var onChange = function () {
      if (readPref() === "auto") apply("auto");
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange); /* Safari < 14 */
  }
})();
