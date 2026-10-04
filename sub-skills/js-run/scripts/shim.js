// shim.js — jsrun 浏览器环境垫片 v0（原创实现）
// 目标：让「环境探测 + 纯计算」型网页 JS 在裸 V8 里以为自己在 Chrome 里。
// v0 面：BOM(navigator/screen/location/window) + document.cookie/title/referrer
//       + crypto.getRandomValues + btoa/atob + 逻辑时间定时器。
// 明确不做（v0）：DOM 解析、布局、真实网络、Canvas/WebGL。
// 保真 TODO：属性 getter 化、Function.toString 伪装、toString 标签一致性。
(function () {
  "use strict";
  // 配置由 Python 侧以全局 var 注入，此处用 globalThis 直读——
  // 此时 window 尚未挂载（挂载在本文件后段），不能经 window 取。
  var env = (typeof globalThis.__jsrun_env__ !== "undefined" && globalThis.__jsrun_env__) || {};

  function merge(base, over) {
    return Object.assign(Object.create(null), base, over || {});
  }

  // ── navigator ──────────────────────────────────────────────
  var navigator = merge({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    appVersion: "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    appName: "Netscape",
    platform: "Win32",
    vendor: "Google Inc.",
    language: "zh-CN",
    languages: ["zh-CN", "en-US"],
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    webdriver: false,
    cookieEnabled: true,
    onLine: true,
    doNotTrack: null,
    plugins: { length: 5 },
    mimeTypes: { length: 2 },
  }, env.navigator);

  // ── screen / location ──────────────────────────────────────
  var screen = merge({
    width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
    colorDepth: 24, pixelDepth: 24, availLeft: 0, availTop: 0,
  }, env.screen);

  var loc = Object.assign({
    href: "https://example.com/", protocol: "https:", host: "example.com",
    hostname: "example.com", port: "", pathname: "/", search: "", hash: "",
    origin: "https://example.com", ancestorOrigins: { length: 0 },
  }, env.location || {});
  loc.assign = function () {}; loc.replace = function () {}; loc.reload = function () {};

  // ── document（v0 仅 cookie/title/referrer/属性壳）───────────
  var document = {
    cookie: "",
    title: "",
    referrer: "",
    URL: loc.href,
    documentURI: loc.href,
    domain: loc.hostname,
    hidden: false,
    visibilityState: "visible",
    readyState: "complete",
    characterSet: "UTF-8",
    contentType: "text/html",
    head: null, body: null, documentElement: null,
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    createElement: function () { throw new Error("jsrun v0: DOM 未实现"); },
    addEventListener: function () {},
    removeEventListener: function () {},
    write: function () {},
  };

  // ── 加密 / 编码 ────────────────────────────────────────────
  // 熵：Python 侧每次上下文经 secrets 注入 128bit 种子（__jsrun_entropy__），
  // JS 内用 mulberry32 展开。保证「不可预测」与「调用不抛错」；
  // 非密码学强度 PRNG，只求 API 形状正确，注释即口径。
  function _prng(seedStr) {
    var h = 1779033703 ^ seedStr.length;
    for (var i = 0; i < seedStr.length; i++) {
      h = Math.imul(h ^ seedStr.charCodeAt(i), 3432918353);
      h = (h << 13) | (h >>> 19);
    }
    var a = h >>> 0;
    return function () {
      a |= 0; a = (a + 0x6D2B79F5) | 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  var _rand = _prng(String(typeof globalThis.__jsrun_entropy__ !== "undefined" ? globalThis.__jsrun_entropy__ : "jsrun"));
  var crypto = {
    getRandomValues: function (arr) {
      for (var i = 0; i < arr.length; i++) arr[i] = Math.floor(_rand() * 256);
      return arr;
    },
    randomUUID: function () {
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
        var r = (_rand() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
      });
    },
  };

  var B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  function btoa(s) {
    var out = [];
    for (var i = 0; i < s.length; i += 3) {
      var b = [(s.charCodeAt(i) || 0), (s.charCodeAt(i + 1) || 0), (s.charCodeAt(i + 2) || 0)];
      var n = (b[0] << 16) | (b[1] << 8) | b[2];
      out.push(B64[(n >> 18) & 63], B64[(n >> 12) & 63],
               isNaN(s.charCodeAt(i + 1)) ? "=" : B64[(n >> 6) & 63],
               isNaN(s.charCodeAt(i + 2)) ? "=" : B64[n & 63]);
    }
    return out.join("");
  }
  function atob(s) {
    var clean = String(s).replace(/=+$/, ""), out = [];
    for (var i = 0; i < clean.length; i += 4) {
      var n = (B64.indexOf(clean[i]) << 18) | (B64.indexOf(clean[i + 1]) << 16) |
              ((B64.indexOf(clean[i + 2]) + 64 || 0) << 8) | (B64.indexOf(clean[i + 3]) + 64 || 0);
      out.push(String.fromCharCode((n >> 16) & 255, (n >> 8) & 255, n & 255));
    }
    return out.join("");
  }

  // ── 逻辑时间定时器（sleep(5000) 瞬间完成）──────────────────
  var _now = 0, _seq = 1, _timers = [];
  function setTimeout(fn, ms) { _timers.push({ at: _now + (ms || 0), fn: fn, id: _seq }); return _seq++; }
  function setInterval(fn, ms) { _timers.push({ at: _now + (ms || 0), fn: fn, id: _seq, every: ms || 0 }); return _seq++; }
  function clearTimeout(id) { _timers = _timers.filter(function (t) { return t.id !== id; }); }
  var clearInterval = clearTimeout;
  function performanceNow() { return _now + 0.001; }

  // ── 挂载到全局（裸 navigator/document/window 都能引用）──────
  var g = globalThis;
  g.window = g;
  g.self = g;
  g.top = g;
  g.parent = g;
  g.frames = g;
  g.navigator = navigator;
  g.screen = screen;
  g.location = loc;
  g.document = document;
  g.crypto = crypto;
  g.btoa = btoa;
  g.atob = atob;
  g.setTimeout = setTimeout;
  g.setInterval = setInterval;
  g.clearTimeout = clearTimeout;
  g.clearInterval = clearInterval;
  try {
    Object.defineProperty(g.performance, "now", { value: performanceNow, configurable: true });
  } catch (e) { /* performance 只读时保持系统实现 */ }

  // ── 控制面：__jsrun__ 设计为不可枚举，目标 JS 不应看见 ──────
  g.__jsrun__ = {
    advance: function (ms) {
      var deadline = _now + ms, fired = 0;
      for (;;) {
        _timers.sort(function (a, b) { return a.at - b.at; });
        var next = _timers[0];
        if (!next || next.at > deadline) break;
        _now = Math.max(_now, next.at);
        _timers.shift();
        if (next.every) _timers.push({ at: _now + next.every, fn: next.fn, id: next.id, every: next.every });
        try { next.fn(); fired++; } catch (e) { fired++; }
        if (fired > 10000) break; // 防失控循环
      }
      _now = deadline;
      return fired;
    },
    now: function () { return _now; },
    pending: function () { return _timers.length; },
  };
})();
