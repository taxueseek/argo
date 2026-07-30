# 引擎：`fxtwitter`

- 准入时间: 2026-07-30T18:40:00+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 948.3
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1000.0ms · count=3
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine fxtwitter
python3 scripts/engine_validate.py --engine fxtwitter --stage health
```
