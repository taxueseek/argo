# 引擎：`keenable`

- 准入时间: 2026-08-26T08:57:39+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 669.3
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| `ARGO_KEENABLE_API_KEY` | 是 |

## 最近验证

- health: pass · latency=666.1ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine keenable
python3 scripts/engine_validate.py --engine keenable --stage health
```
