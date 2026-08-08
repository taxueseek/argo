# 引擎：`realtime_index`

- 准入时间: 2026-08-08T14:55:14+08:00
- 状态: admitted
- cost_tier: free
- type: cli
- quality_score: None
- avg_latency_ms: 812.8
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=812.8ms · count=10
- quality: None · score=None · empty_rate=None

## 调用

```bash
python3 scripts/search.py "查询词" --engine realtime_index
python3 scripts/engine_validate.py --engine realtime_index --stage health
```
