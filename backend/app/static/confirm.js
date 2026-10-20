/* 危险操作确认：把「确认文案」放进 data-confirm，而不是写在 onsubmit 里拼字符串。
 *
 * 为什么不用 onsubmit="return confirm('删除 {{ model }}？')"：
 * Jinja 会把 ' 转义成 &#39;，但 HTML 实体在属性值被解析时会先解码，
 * JS 引擎看到的仍是裸引号 —— 型号里写 X');alert(1);// 就能执行脚本。
 * data-* 属性的值是纯文本，浏览器不会把它当代码。
 */
(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.getAttribute) return;
    var message = form.getAttribute("data-confirm");
    if (!message) return;
    if (!window.confirm(message)) {
      event.preventDefault();
      event.stopPropagation();
    }
  });
})();
