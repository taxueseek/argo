# 引擎：`zenodo`

- 准入时间: 2026-08-05T21:55:49+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 1.0
- avg_latency_ms: 1873.1
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=1554.0ms · count=3
- quality: pass · score=1.0 · empty_rate=0.0

## 调用

```bash
python3 scripts/search.py "查询词" --engine zenodo
python3 scripts/engine_validate.py --engine zenodo --stage health
```
