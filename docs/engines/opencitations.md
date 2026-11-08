# 引擎：`opencitations`

- 准入时间: 2026-11-08T19:58:34+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 425.5
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1702.6ms · count=1
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine opencitations
python3 scripts/engine_validate.py --engine opencitations --stage health
```
