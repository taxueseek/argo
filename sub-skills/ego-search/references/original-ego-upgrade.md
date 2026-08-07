# 原版 ego 技能升级要点（与本子技能配套）

原版 `ego-browser` skill 仍是**通用浏览器自动化**真源；本文件只补「与登录态专业搜索完全态」对齐的用法，不替换原版 SKILL 正文。

## 与 ego-search 分工

| 场景 | 用谁 |
|------|------|
| 搜 SERP / 抓正文 / 已知 API / 登录墙取证 | **ego-search**（双运行时 auto） |
| 复杂点选、填表、多步、截图工作流 | **原版 ego-browser** heredoc |
| 无 ego App、只有用户 Chrome | **ego-search --runtime webbridge** |

## 建议启用但常被忽略的能力

在 `ego-browser nodejs` 中（打开目标站后）：

```js
// 站点经验包（若该站有 learnings）
const ctx = await learnContext()
cliLog(JSON.stringify(ctx))
// 例如 google 搜索抽取（以实际 manifest 为准）
// await runSiteTool('google', 'search_and_extract', { query: '...' })
```

站点包通常在应用内 `learnings/`（google / x-com / github 等）。

## 输出建议（与隔离/融合对齐）

原版 heredoc 若产出可并入分析的结果，请在 `cliLog` 的 JSON 中尽量带：

```json
{
  "login_state_used": true,
  "auth_partition": "login",
  "cache_eligible": false,
  "search_partition": "login",
  "merge_with_public_ok": true,
  "runtime": "ego",
  "source": "ego-browser"
}
```

勿把登录态 body 写入 argo 公共 `SearchCache`。

## 探测

```bash
python3 sub-skills/ego-search/scripts/ego_search.py status
```

`runtimes.preferred` / `login_search_ready` 表示登录态专业搜索是否可跑。
