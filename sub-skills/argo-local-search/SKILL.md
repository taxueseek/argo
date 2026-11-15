---
name: argo-local-search
description: Zero-cost local file search, 32 keyless aggregation engines, and hybrid BM25 + vector semantic note search for AI agents via Argo. (本地文件检索、零成本聚合与本地知识库语义搜索)
version: 2.14.1
---

# Argo Local & Zero-Cost Search

Use this skill when you need to search local codebases, notes, or knowledge bases, or run web/academic/code queries without paid API keys.

## Commands

1. **Zero-cost web & vertical aggregation** (32 keyless local parsers):
   ```bash
   argo search "<query>" --local-first --json --fields agent
   ```
2. **Merge local workspace files with web hits**:
   ```bash
   argo search "<query>" --include-local --json --fields agent
   ```
3. **Hybrid BM25 + vector semantic search over local notes & markdown** (`local-seek`):
   ```bash
   python3 sub-skills/local-seek/scripts/seek.py "<query>" --path <dir> --json
   ```
4. **Cost-free fast mode** (skips paid engines automatically):
   ```bash
   argo search "<query>" --mode fast --json --fields agent
   ```

## Notes

- Zero API keys required; paid engines are skipped automatically when keys are absent.
- See `sub-skills/local-search/SKILL.md` and `sub-skills/local-seek/SKILL.md` for detailed options.
