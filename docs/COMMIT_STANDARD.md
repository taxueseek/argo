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

**带 scope 的提交**：scope 在下面这张白名单里即高风险。`scripts/check_commit_report.py`
按同一份白名单做机械检查，`tests/test_check_commit_report.py` 会对拍两者。

`engines` / `engine` / `route` / `search` / `rank` / `states` / `state` /
`agents` / `agent` / `release` / `deps`

典型形态与要求：

- `feat(engines)` / `fix(engines)` / `chore(engines)`：新增或改引擎，必须真机验过
- `feat(route)` / `fix(search)` / `fix(rank)` / `refactor(states)`：动路由、排序或
  状态词表，必须跑对应的离线评测与词表门禁
- `feat(agents)` / `fix(agents)`：动 agent 路由表、提交规范、门禁本身——**门禁管
  不到自己正是它最该管的地方**，2026-11-10 首次落地时就漏了这一项

**无 scope 或特殊形态**（命中任一即高风险）：

- `perf(...)`：动性能相关代码，必须给实测数字
- `fix(release)` / `chore(release)`：动发版路径
- `build(deps)`：动依赖链

其余提交（`docs` / `test` / `refactor` / `style` / `chore` / `feat(local-seek)`
等子技能改动）不强制，但鼓励写。

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
