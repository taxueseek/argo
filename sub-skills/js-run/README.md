# js-run（argo 内部 V8 算车道，v0 骨架）

> argo 的 JS 运行时车道：不开浏览器跑网页 JS（`mini-racer` + 环境垫片），已接入 fetch 降级链第一级 E（`scripts/fetch_jsrun.py`，`ARGO_FETCH_JSRUN` 默认开启）。无独立 Skill 触发入口。

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
