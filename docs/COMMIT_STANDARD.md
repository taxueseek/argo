# 提交与验收规范

> 本文件是 argo 提交信息格式的**唯一真源**。`scripts/check_commit_report.py`
> 按本文件的规则做机械检查，pre-commit 会在高风险提交上跑它。

## 为什么需要

argo 的提交历史里有大量高质量的根因记录（「同根因累计 9 处灭类」「两笔都过了
hook 却在全量回归里红」），但**这些记录只存在于 commit message 里，没有结构**。
两个真实代价：

1. **没跑的检查被省略不写**。`f702ee2` 把 SKILL.md 压到 8049B、`4a71693` 让
   deepseek_search 成死源，两笔都过了 pre-commit hook 却在全量回归里红——跑了的
   门禁全绿，没跑的那条门没人知道它该跑。
2. **复发只能靠人记**。同一类根因在 3 个不同日子出现，靠人回想「这个是不是
   以前犯过」。`scripts/badcase_ledger.py` 现在能自动判定（见 `check --promote-days`），
   但前提是每次提交都说清跑了什么。

一句话：**跑了的检查要写，没跑的检查也要写。**

## 提交信息格式

```
<type>(<scope>): <一句话说清改了什么>

<可选正文：根因、实测数据、影响面>
```

`type` 用 `feat` / `fix` / `perf` / `refactor` / `docs` / `test` / `build` /
`chore` / `ci`。`scope` 是可选的模块名（`engines` / `states` / `route` / `search` …）。

### 正文里的「检查清单」段

高风险提交**必须**带一段检查清单，三段齐全：

```
检查：
- 跑了：<script/replay_eval.py --check>
- 在 commit：<full-sha>
- 未跑：<live 引擎实测（需配额）> / <三平台编译> / 无
```

三段固定用这三个名字：`跑了`、`在 commit`、`未跑`。

- **跑了**：实际执行过的检查命令。写命令本身，不写「已验证」这种结论。
- **在 commit**：跑这些检查时所在的 commit（full sha）。换了 commit 要重跑。
- **未跑**：没跑的检查，写明是哪一项。**确实没漏就写 `无`**，不能整段省略。

## 哪些提交算高风险

满足任一条件即高风险，`scripts/check_commit_report.py` 会要求检查清单：

- `feat(engines)` / `fix(engines)` / `chore(engines)`：新增或改引擎，必须真机验过
- 含 `fix(route` / `fix(search)` / `feat(search)` / `fix(rank)`：动路由或排序，
  必须跑 `scripts/replay_eval.py --check`
- 含 `feat(states)` / `refactor(states)` / `fix(states)`：动状态词表，必须跑
  `tests/test_state_literal_drift.py` 与 `tests/test_state_registry.py`
- 含 `perf(`：动性能相关代码，必须给实测数字
- 含 `fix(release)` / `chore(release)` / `build(deps)`：动发版或依赖链

其余提交（`docs` / `test` / `refactor` 等）不强制，但鼓励写。

## 回退测试要求

新增测试时，**必须验证它真能抓到退步**：把旧实现放回去，新测试要挂。

做法：

```bash
# 1. 存新实现
cp scripts/<file>.py /tmp/keep.py
# 2. 换成旧实现（或删掉你加的判定）
# 3. 跑测试，确认挂
python3 -m pytest tests/test_<file>.py -q
# 4. 恢复
cp /tmp/keep.py scripts/<file>.py
```

「测试存在」不等于「测试有效」。argo 的 `tests/test_domain_rule_schema.py` 里有
现成的形态：每条 `unless` 必须配一条反例，就是同一个道理。

## 合并 PR 时

- 用 `--match-head-commit <审过的 sha>` 合并。审的过程中有人再 push，合并直接失败，
  确保没审过的代码进不来。
- PR 描述里同样写「跑了什么、没跑什么」，不要只写结论。
- 回退测试结果贴进 PR 描述（「旧实现下 `test_x` 挂，新实现下过」）。
