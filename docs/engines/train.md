# 引擎：`train`

- 准入时间: 2026-10-06T08:42:42+08:00
- 状态: admitted
- cost_tier: free
- type: cli
- quality_score: 1.0
- avg_latency_ms: 414.1
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=414.1ms · count=10
- quality: None · score=None · empty_rate=None

## 调用

```bash
python3 scripts/search.py "查询词" --engine train
python3 scripts/engine_validate.py --engine train --stage health
```
