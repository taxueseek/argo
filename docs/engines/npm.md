# 引擎：`npm`

- 准入时间: 2026-09-18T22:58:01+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 2058.7
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=611.6ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine npm
python3 scripts/engine_validate.py --engine npm --stage health
```
