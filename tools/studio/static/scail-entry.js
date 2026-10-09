/* SCAIL 구간 수정 진입점 (외부 스크립트: 사이트 CSP가 인라인 script를 막음)
 * 1) "제작·도구" 더보기 메뉴에 '구간 수정', '생성 중인 구간 보기' 항목 추가
 * 2) SCAIL 결과 영상 옆에 '구간 수정' 링크 (재생 위치로 바로 이동)
 * 3) 생성 중인 SCAIL 작업 카드에 '생성된 구간 보기' 링크
 */
(function () {
  "use strict";
  var RE = /scail_user\/(scail_[a-f0-9]{32})\//;

  /* ---------- 1) 제작·도구 메뉴 항목 ---------- */
  var MENU = [
    { attr: "data-scail-live", href: "/scail-live", text: "🎞 생성 중인 구간 보기" },
    { attr: "data-scail-repair", href: "/scail-repair", text: "🔧 SCAIL 구간 수정" },
    { attr: "data-ops-assistant", href: "/ops-assistant", text: "🛠 Gemma 정비 도우미" }
  ];
  function addMenuItems() {
    var menu = document.querySelector(".nav-more-menu");
    if (!menu) return false;
    var sample = menu.querySelector("button, a");
    var host = menu.querySelector("[role=group], .nav-group, ul, div") || menu;
    // 맨 앞에 하나씩 끼워 넣으므로 역순으로 넣어야 배열 순서대로 보인다
    MENU.slice().reverse().forEach(function (m) {
      if (menu.querySelector("[" + m.attr + "]")) return;
      var a = document.createElement("a");
      a.setAttribute(m.attr, "1");
      a.href = m.href;
      a.textContent = m.text;
      if (sample && sample.className) a.className = sample.className;
      a.style.cssText = "display:block;text-decoration:none;cursor:pointer";
      host.insertBefore(a, host.firstElementChild || null);
    });
    return true;
  }

  /* ---------- 2) 결과 영상 -> 수정 바로가기 (영상 1개 / 작업 ID 1개당 버튼 1개) ---------- */
  function srcOf(v) {
    var c = [v.currentSrc, v.src];
    var s = v.querySelector && v.querySelector("source[src]");
    if (s) c.push(s.src);
    for (var i = 0; i < c.length; i++) if (c[i] && RE.test(c[i])) return c[i];
    return "";
  }
  function idOf(s) { var m = RE.exec(s || ""); return m ? m[1] : ""; }
  function makeLink(id, getT) {
    var a = document.createElement("a");
    a.setAttribute("data-scail-fix", id);
    a.textContent = "🔧 구간 수정";
    a.href = "/scail-repair?id=" + id;
    a.title = "이 영상에서 마음에 안 드는 구간만 다시 생성";
    a.style.cssText =
      "display:inline-block;margin:6px 6px 0 0;padding:6px 11px;border-radius:999px;font-size:13px;" +
      "font-weight:600;text-decoration:none;background:#4a6bff;color:#fff;line-height:1.2";
    a.addEventListener("click", function () {
      var t = getT && getT();
      if (t && t > 0.2) a.href = "/scail-repair?id=" + id + "&t=" + t.toFixed(2);
    });
    return a;
  }
  function inject() {
    var seen = {};
    // 이미 붙어 있는 링크는 ID별로 하나만 남기고 나머지는 제거 (중복 방지)
    [].slice.call(document.querySelectorAll("[data-scail-fix]")).forEach(function (a) {
      var id = a.getAttribute("data-scail-fix");
      if (seen[id] || !a.isConnected) { a.remove(); return; }
      seen[id] = true;
    });
    // 영상 요소 기준으로만 주입 (앱이 만든 mp4 링크에는 붙이지 않음)
    [].slice.call(document.querySelectorAll("video")).forEach(function (v) {
      var id = idOf(srcOf(v));
      if (!id || seen[id]) return;
      seen[id] = true;
      v.parentNode && v.parentNode.insertBefore(makeLink(id, function () { return v.currentTime; }), v.nextSibling);
    });
  }

  /* ---------- 3) 생성 중인 작업 카드 -> 생성된 구간 보기 ---------- */
  var JOB = /^scail_[a-f0-9]{32}$/;
  function injectLive() {
    [].slice.call(document.querySelectorAll("article.wan-card[data-ui-job-id]")).forEach(function (card) {
      var id = card.getAttribute("data-ui-job-id");
      if (!JOB.test(id)) return;
      var link = card.querySelector("[data-scail-live-card]");
      var running = card.getAttribute("data-state") === "running";
      if (running && !link) {
        link = document.createElement("a");
        link.setAttribute("data-scail-live-card", id);
        link.href = "/scail-live?id=" + id;
        link.textContent = "🎞 생성된 구간 보기";
        link.title = "구간이 끝날 때마다 바로 보고, 마음에 안 들면 중단할 수 있어요";
        link.style.cssText =
          "display:inline-block;margin:8px 0 0;padding:6px 11px;border-radius:999px;font-size:13px;" +
          "font-weight:600;text-decoration:none;background:#2f9e6b;color:#fff;line-height:1.2";
        card.appendChild(link);
      } else if (!running && link) {
        link.remove();
      }
    });
  }

  /* 번들이 메뉴를 늦게 그리므로 잠깐 반복 시도 */
  var tries = 0;
  var menuTimer = setInterval(function () {
    if (addMenuItems() || ++tries > 60) clearInterval(menuTimer);
  }, 500);

  setInterval(function () { inject(); injectLive(); }, 1000);
})();
