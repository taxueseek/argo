# 引擎：`sec_edgar`

- 准入时间: 2026-08-05T21:55:38+08:00
- 状态: admitted
- cost_tier: free
- type: http
- quality_score: 0.8
- avg_latency_ms: 956.6
- blocked: False

## 环境变量

| 变量 | 必填 |
|------|------|
| （无） | 否 |

## 最近验证

- health: pass · latency=860.8ms · count=3
- quality: pass · score=0.8 · empty_rate=0.2

## 调用

```bash
python3 scripts/search.py "查询词" --engine sec_edgar
python3 scripts/engine_validate.py --engine sec_edgar --stage health
```
