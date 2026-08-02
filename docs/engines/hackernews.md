# 引擎：`hackernews`

- 准入时间: 2026-07-30T08:53:49+08:00
- 状态: admitted
- cost_tier: free
- type: hackernews
- quality_score: None
- avg_latency_ms: 519.6
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=519.6ms · count=3
- quality: None · score=None · empty_rate=None

## 调用

```bash
python3 scripts/search.py "查询词" --engine hackernews
python3 scripts/engine_validate.py --engine hackernews --stage health
```
