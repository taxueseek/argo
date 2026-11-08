# 引擎：`vam`

- 准入时间: 2026-11-08T17:58:50+08:00
- 状态: admitted
- cost_tier: free
- type: vam
- quality_score: 0.987
- avg_latency_ms: 557.4
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1903.7ms · count=8
- quality: pass · score=0.987 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine vam
python3 scripts/engine_validate.py --engine vam --stage health
```
