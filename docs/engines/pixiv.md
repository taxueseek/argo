# 引擎：`pixiv`

- 准入时间: 2026-11-08T07:03:06+08:00
- 状态: admitted
- cost_tier: free
- type: pixiv
- quality_score: 1.0
- avg_latency_ms: 334.1
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=711.2ms · count=10
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine pixiv
python3 scripts/engine_validate.py --engine pixiv --stage health
```
