// 市场消息时间线：按天分组的单列倒序 + 无限滚动 + 日期组折叠。
//
// 为什么不用第三方库（Masonry / @egjs/infinitegrid / react-masonry）：
// 本项目刻意没有前端构建链（见 backend/app/main.py 顶部说明），这里只需要
// 「滚到底再取一页」和「把新卡片接到正确的日期组」，自己写几十行就够。
//
// 版式取舍：参考 aihot.news 的单列时间线。多列瀑布流看着热闹，但倒序时间线
// 被拆到多列后，读者的视线要来回跳，越往下越难判断哪条更新。
// DOM 顺序始终等于时间倒序，读屏和 Tab 顺序也正确。
(function () {
  "use strict";

  var wall = document.getElementById("newsWall");
  if (!wall) return;

  var more = document.getElementById("newsMore");
  var moreBtn = document.getElementById("newsMoreBtn");
  var cursor = wall.getAttribute("data-cursor") || "";
  var entity = wall.getAttribute("data-entity") || "";
  var source = wall.getAttribute("data-source") || "";
  var keyword = wall.getAttribute("data-q") || "";
  var loading = false;
  var observer = null;

  // 当前正在追加的日期组：首屏最后一组的日期 + 它的卡片容器
  var currentDay = wall.getAttribute("data-last-day") || "";
  var sections = wall.querySelectorAll(".news-day");
  var currentBody = sections.length
    ? sections[sections.length - 1].querySelector(".news-day-body")
    : null;

  function openSection(day, label) {
    var section = document.createElement("section");
    section.className = "news-day";
    section.setAttribute("data-day", day);

    var head = document.createElement("button");
    head.type = "button";
    head.className = "news-day-head";
    head.setAttribute("aria-expanded", "true");

    var date = document.createElement("span");
    date.className = "news-day-date";
    date.textContent = label || day;

    var count = document.createElement("span");
    count.className = "news-day-count";

    var caret = document.createElement("span");
    caret.className = "news-day-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "▾";

    head.appendChild(date);
    head.appendChild(count);
    head.appendChild(caret);

    var body = document.createElement("div");
    body.className = "news-day-body";

    section.appendChild(head);
    section.appendChild(body);
    wall.appendChild(section);
    return body;
  }

  function bumpCount(body) {
    var section = body.parentNode;
    var count = section && section.querySelector(".news-day-count");
    if (count) count.textContent = body.querySelectorAll(".news-card").length + " 条";
  }

  // 分片里的卡片没有日期头：按 data-day 接到已有组，或新开一组。
  // 一页可能正好从某天中间开始，所以必须由前端判断，服务端无法预先分组。
  function appendCards(html) {
    var holder = document.createElement("div");
    holder.innerHTML = html;
    var cards = Array.prototype.slice.call(holder.querySelectorAll(".news-card"));
    var touched = [];
    for (var i = 0; i < cards.length; i += 1) {
      var card = cards[i];
      var day = card.getAttribute("data-day") || "";
      if (!currentBody || day !== currentDay) {
        currentDay = day;
        currentBody = openSection(day, card.getAttribute("data-day-label"));
      }
      currentBody.appendChild(card);
      if (touched.indexOf(currentBody) === -1) touched.push(currentBody);
    }
    for (var j = 0; j < touched.length; j += 1) bumpCount(touched[j]);
  }

  function finish(message) {
    if (observer) {
      observer.disconnect();
      observer = null;
    }
    if (more) {
      more.textContent = message;
      more.classList.add("is-done");
    }
  }

  function loadMore() {
    if (loading || !cursor) return;
    loading = true;
    if (moreBtn) {
      moreBtn.disabled = true;
      moreBtn.textContent = "加载中…";
    }
    var params = ["cursor=" + encodeURIComponent(cursor), "limit=24"];
    if (entity) params.push("entity=" + encodeURIComponent(entity));
    if (source) params.push("source=" + encodeURIComponent(source));
    if (keyword) params.push("q=" + encodeURIComponent(keyword));

    fetch("/news/items?" + params.join("&"), {
      headers: { Accept: "application/json" },
      credentials: "same-origin"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (data.html) appendCards(data.html);
        cursor = data.next_cursor || "";
        wall.setAttribute("data-cursor", cursor);
        if (!cursor) {
          finish("没有更多了");
        } else if (moreBtn) {
          moreBtn.disabled = false;
          moreBtn.textContent = "加载更多";
        }
      })
      .catch(function (error) {
        // 常见原因：登录过期被重定向到登录页（返回的不是 JSON）
        finish("加载失败，请刷新页面重试（" + error.message + "）");
      })
      .then(function () {
        loading = false;
      });
  }

  function watch() {
    if (!more || !cursor) return;
    if (!("IntersectionObserver" in window)) return; // 老浏览器退回手动点「加载更多」
    observer = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) loadMore();
    }, { rootMargin: "600px 0px" });
    observer.observe(more);
  }

  // 折叠：组头是按钮，点击切换 is-collapsed；aria-expanded 同步给读屏
  wall.addEventListener("click", function (event) {
    var head = event.target.closest ? event.target.closest(".news-day-head") : null;
    if (!head) return;
    var section = head.parentNode;
    var collapsed = section.classList.toggle("is-collapsed");
    head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  });

  if (moreBtn) moreBtn.addEventListener("click", loadMore);
  watch();
})();
