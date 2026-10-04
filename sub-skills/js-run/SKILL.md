---
name: js-run
description: >-
  js-run 是 argo 的 JS 运行时车道子技能：不开浏览器跑网页 JS。基于 mini-racer（ISC）
  + 自研浏览器环境垫片，让「环境探测 + 纯计算」型网页脚本（反爬挑战通行证、混淆数据
  解算、JS 内嵌状态提取）在毫秒级、可复现、无真实网络的沙箱里执行。定位是取数链
  curl_cffi 与渲染级之间的「算」车道，不替代浏览器（登录态/视觉/真实网络数据不在此车道）。
  v0 为独立骨架，未接入主链。Triggers include 跑网页JS、挑战脚本、通行证、JS解密、
  逻辑时间、无浏览器执行。
metadata:
  version: "0.1.0"
  date: "2026-10-03"
  parent: argo
  upstream: 原创（底座 py-mini-racer ISC；设计参照 iv8 思路，未使用其代码/二进制）
  license_note: 底座 mini-racer 为 ISC；本目录代码为原创
---

# js-run（argo 子技能，v0 骨架）

**一句话**：给 HTML 与资源，还你 JS 算完的结果——不开浏览器。

## 它解决什么

网页 JS 依赖浏览器环境（navigator/document/crypto…）时，传统上只有两条路：
开真浏览器（秒级、百 MB、重）或放弃。js-run 把「浏览器环境的 JS 语义」做成库：
V8 执行 + 环境垫片，单次上下文 P50 ≈ 1ms，逻辑时间让 `setTimeout(5000)` 瞬间完成。

## v0 明确不做什么

| 不做 | 原因 | 归属 |
|---|---|---|
| DOM 解析/布局 | v0 面只保「探测+计算」型脚本 | 后续按考卷生长 |
| 真实网络请求 | 它一个字节都不取，取数归 HTTP 车道 | argo 前两级 |
| 登录态/指纹连续性 | 合成环境无会话可继承 | ego-search |
| 截图/视觉 | 无布局引擎 | ego-browser |

## 用法

```python
import sys; sys.path.insert(0, "sub-skills/js-run/scripts")
from jsrun import JsRun

with JsRun() as jr:
    jr.run(open("challenge.js").read())     # 跑目标脚本
    jr.advance(3000)                         # 逻辑时间推进
    cookie = jr.get_cookie()                 # 收通行证
```

```bash
python3 tests/test_jsrun.py   # 7 项金标测试（含速度门 <100ms，实测 P50 1.0ms）
```

## 依赖

- `mini-racer`（ISC，V8 绑定）：`pip install mini-racer`
- 无其他第三方依赖；Python ≥ 3.10

## 纪律

1. **产物只当线索不当正文**：网络数据在垫片里没有真源，页面 JS 拿到的任何
   「网络响应」都不可信；需要数据必须 Python 侧真取。
2. **反爬对抗谨慎**：本车道用于已授权/自有场景的通行证计算；对第三方站点的
   挑战绕过默认关闭，启用须单独拍板。
3. **考卷驱动生长**：环境面只按 tests/fixtures/ 里的真实考卷长，不为想象中的
   浏览器写代码（详见 docs-local 设计文档）。
