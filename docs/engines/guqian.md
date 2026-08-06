# 引擎：`guqian`

- 准入时间: 2026-08-05T21:56:03+08:00
- 状态: admitted
- cost_tier: free
- type: cli
- quality_score: 1.0
- avg_latency_ms: 32.0
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=36.7ms · count=3
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine guqian
python3 scripts/engine_validate.py --engine guqian --stage health
```
