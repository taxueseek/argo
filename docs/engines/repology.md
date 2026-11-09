# 引擎：`repology`

- 准入时间: 2026-11-09T23:45:05+08:00
- 状态: admitted
- cost_tier: free
- type: repology
- quality_score: 0.6
- avg_latency_ms: 532.7
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1357.7ms · count=10
- quality: pass · score=0.6 · empty_rate=0.4

## 调用

```bash
python3 scripts/search.py "查询词" --engine repology
python3 scripts/engine_validate.py --engine repology --stage health
```
