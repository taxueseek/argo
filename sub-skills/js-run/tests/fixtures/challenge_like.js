// challenge_like.js — 金标考卷 #1：挑战页形态的 JS（离线可复跑）
// 形态参照真实反爬挑战脚本的三段式：环境探测 → 字符串运算 → 发通行证。
// 只用 v0 垫片保证过的面：navigator/screen/btoa/document.cookie/定时器。
(function () {
  var probe = [
    navigator.userAgent.length,
    navigator.platform,
    navigator.language,
    screen.width * screen.height,
    navigator.hardwareConcurrency,
    Number(!navigator.webdriver),
  ].join("|");

  // 字符串运算（模拟混淆层的算子链）
  var salt = "";
  for (var i = 0; i < probe.length; i += 7) salt += String.fromCharCode(probe.charCodeAt(i) ^ 0x1f);
  var token = btoa(salt + "|" + probe.length);

  // 定时器：真浏览器里等 3 秒才发 cookie；逻辑时间下瞬间完成
  var issued = false;
  setTimeout(function () {
    document.cookie = "__jsl_clearance=" + token + "; path=/";
    issued = true;
  }, 3000);

  return { probe: probe, token: token, issued: issued };
})();
