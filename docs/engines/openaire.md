# 引擎：`openaire`

- 准入时间: 2026-11-09T23:59:27+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 0.994
- avg_latency_ms: 747.8
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=2925.2ms · count=8
- quality: pass · score=0.994 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine openaire
python3 scripts/engine_validate.py --engine openaire --stage health
```
