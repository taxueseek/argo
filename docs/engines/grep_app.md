# 引擎：`grep_app`

- 准入时间: 2026-11-10T00:02:11+08:00
- 状态: admitted
- cost_tier: free
- type: grep_app
- quality_score: 0.4
- avg_latency_ms: 6757.1
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=855.1ms · count=10
- quality: pass · score=0.4 · empty_rate=0.6

## 调用

```bash
python3 scripts/search.py "查询词" --engine grep_app
python3 scripts/engine_validate.py --engine grep_app --stage health
```
