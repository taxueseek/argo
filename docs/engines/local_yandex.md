# 引擎：`local_yandex`

- 准入时间: 2026-11-10T00:35:08+08:00
- 状态: admitted
- cost_tier: free
- type: cli
- quality_score: 1.0
- avg_latency_ms: 84.1
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=6250.5ms · count=8
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine local_yandex
python3 scripts/engine_validate.py --engine local_yandex --stage health
```
