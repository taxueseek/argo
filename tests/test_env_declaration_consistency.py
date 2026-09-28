#!/usr/bin/env python3
"""test_env_declaration_consistency.py — 密钥声明一致性（防「状态说谎」）。

## 背景：实测发现的一类真 bug

`you` 与 `parallel` 两个引擎的 builder 需要 API 密钥，但 config 里**未声明
required_env**。后果是状态机报 `ready` / `routable=True`，路由会选中它们，
而实际调用恒定失败：

    you      -> {"error": "YDC_API_KEY 未设置"}
    parallel -> {"error": "PARALLEL_API_KEY 未设置"}

这是「失败伪装成成功」在**状态层**的又一次出现（前两次：V2EX 旧实现产出
幻觉结果、缓存软命中跨引擎串味）。用户看到 `--list-engines` 说 ready，
却永远拿不到结果，且无从知道原因。

## 本测试的作用

把「builder 读取的密钥」与「spec 声明/别名表」做一致性核对。
新增 builder 若忘记声明密钥，会在这里被拦下——而不是等用户发现状态说谎。

判定计算方式（任一满足即认为已覆盖）：
  a) spec.required_env 中声明（直接或别名形式）
  b) KNOWN_ENV_ALIASES 覆盖该 engine_id
"""

import ast
import os
import re
import sys

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from config import load_config  # noqa: E402
from engine_env import (  # noqa: E402
    KNOWN_ENV_ALIASES, PLACEHOLDER_ALIASES, required_env_for, get_env,
    missing_env_for,
)

# 密钥类变量名的形态：结尾为这些后缀
_SECRET_SUFFIXES = ("_API_KEY", "_APIKEY", "_KEY", "_TOKEN", "_SECRET", "_APPID")
# 明确不需要声明的：非密钥用途的常规环境变量
_NOT_SECRETS = frozenset({
    "ARGO_STATE_DIR", "ARGO_ENGINE_HTTP_CLIENT", "ARGO_ENABLE_ENGINES",
    "ARGO_DISABLE_ENGINES", "ARGO_PYTHON", "PATH", "HOME", "TMPDIR",
    "ARGO_DEBUG", "ARGO_LOG_LEVEL", "LANG",
})


def _builders_module_path() -> str:
    return os.path.join(SCRIPTS_DIR, "engines.py")


def _builder_env_reads(engine_id: str) -> set[str]:
    """返回某引擎 builder 及其调用的本地 helper 直读的环境变量名（名集合）。"""
    return set(_builder_env_reads_kind(engine_id))


def _builder_env_reads_kind(engine_id: str) -> dict[str, bool]:
    """同 _builder_env_reads，但值区分**读取方式是否变体感知**。

    为什么需要这个区分（2026-09-29，issue #12 续发事故）：状态层
    `missing_env_for` 对所有声明名都走 `get_env(candidates)` 候选链——
    用户配 ARGO_X 或裸 X 任一都算 ready。builder 若裸读
    `os.environ.get("X")`，则「声明⊆读取」仍成立（X 确实被读了），
    forward 测试全绿，但用户按文档配 ARGO_X 后执行层取到空串——
    状态说谎的反方向。故反向测试的判据不是「读没读」，而是
    「是否变体感知地读」：get_env/env_flag 是（单名也会经 _name_variants
    展开）；os.environ.get/os.getenv/os.environ[] 不是。

    解析路径：engines.py 的 _BUILDERS[engine_id] → 取函数名 →
    在该函数所在模块的 AST 里定位它 → 收集常量参数（含 get_env 列表形态）。

    **必须跟随本地 helper 调用**（实测教训）：`_build_you_engine` 本身不读
    密钥，它调用同模块的 `_you_key()` 才读 `YDC_API_KEY`。首版扫描只看
    builder 函数体，因此漏判了 you/parallel —— 正是这两个就是被发现的
    真实 bug 案例。故对同模块内的本地函数做传递闭包。
    """
    import engines as eng_mod

    fn = getattr(eng_mod, "_BUILDERS", {}).get(engine_id)
    if fn is None:
        return {}
    fn_name = getattr(fn, "__name__", "")
    if not fn_name:
        return {}

    # 定位包含该函数的模块文件
    mod_file = None
    for cand in ("engines_builders.py", "engines_builders_cn.py",
                 "engines_builders_data.py", "engines_builders_data_macro.py",
                 "engines_builders_intl.py", "engines_builders_search.py",
                 "engines_builders_tech.py"):
        path = os.path.join(SCRIPTS_DIR, cand)
        if not os.path.isfile(path):
            continue
        try:
            src = open(path, encoding="utf-8").read()
        except OSError:
            continue
        if f"def {fn_name}(" in src:
            mod_file = path
            break
    if not mod_file:
        return {}

    try:
        tree = ast.parse(open(mod_file, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return {}

    # 收集**模块顶层**函数（供传递闭包解析）。
    # 关键：不能用 ast.walk 收集全部函数——`_engine` 这类嵌套函数名在各个
    # builder 里重复出现，混在一起会让 A 引擎解析到 B 引擎的嵌套函数体
    # （实测：anysearch 因此误报读了 EXA_API_KEY）。只取 tree.body 顶层。
    funcs: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node

    found: dict[str, bool] = {}
    visited: set[str] = set()

    def _collect(node: ast.AST) -> dict[str, bool]:
        """收集某函数体内的密钥读取（含其嵌套函数，因为它们同属一个 builder）。"""
        reads: dict[str, bool] = {}

        def _record(call: ast.Call, aware: bool) -> None:
            if not call.args:
                return
            a0 = call.args[0]
            consts: list[ast.AST] = []
            if isinstance(a0, ast.Constant):
                consts = [a0]
            elif isinstance(a0, (ast.List, ast.Tuple)):
                # get_env(["ARGO_X", "X"]) 候选链写法：列表整体可变体感知
                consts = list(a0.elts)
            for c in consts:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    # 任一入口变体感知即视为该名被变体感知地读取
                    reads[c.value] = reads.get(c.value, False) or aware

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Attribute) and f.attr in ("get", "getenv"):
                    _record(sub, aware=False)  # os.environ.get / os.getenv：裸读
                elif isinstance(f, ast.Name) and f.id == "getenv":
                    _record(sub, aware=False)
                elif isinstance(f, ast.Name) and f.id in ("get_env", "env_flag"):
                    _record(sub, aware=True)  # 候选链/单名展开：变体感知
            elif isinstance(sub, ast.Subscript):
                v = sub.value
                if (isinstance(v, ast.Attribute) and v.attr == "environ"
                        and isinstance(sub.slice, ast.Constant)
                        and isinstance(sub.slice.value, str)):
                    reads[sub.slice.value] = reads.get(sub.slice.value, False)
        return reads

    def _resolve(fname: str) -> None:
        if fname in visited or fname not in funcs:
            return
        visited.add(fname)
        node = funcs[fname]
        for name, aware in _collect(node).items():
            found[name] = found.get(name, False) or aware
        # 传递：仅跟随**顶层**函数调用（顶层函数才可能是共享 helper）
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) \
                    and sub.id in funcs:
                _resolve(sub.id)

    _resolve(fn_name)
    return found


def _is_secret(name: str) -> bool:
    if not name or name in _NOT_SECRETS:
        return False
    if name.startswith("ARGO_") and name.endswith("_DIR"):
        return False
    return any(name.endswith(s) for s in _SECRET_SUFFIXES)


def _declared_names(engine_id: str, spec: dict) -> set[str]:
    """该引擎已声明的候选密钥名（含别名展开）。"""
    out: set[str] = set()
    for n in required_env_for(engine_id, spec):
        out.add(n)
        if n.startswith("ARGO_"):
            out.add(n[len("ARGO_"):])
        else:
            out.add(f"ARGO_{n}")
    for n in KNOWN_ENV_ALIASES.get(engine_id, []):
        out.add(n)
        if n.startswith("ARGO_"):
            out.add(n[len("ARGO_"):])
    return out


def _candidate_engines() -> list[tuple[str, dict]]:
    cfg = load_config()
    engs = cfg.get("engines") or {}
    return [(k, v) for k, v in engs.items() if isinstance(v, dict)]


def _is_secret(name: str) -> bool:
    if not name or name in _NOT_SECRETS:
        return False
    if name.startswith("ARGO_") and name.endswith("_DIR"):
        return False
    return any(name.endswith(s) for s in _SECRET_SUFFIXES)


def _declared_names(engine_id: str, spec: dict) -> set[str]:
    """该引擎已声明的候选密钥名（含别名展开）。"""
    out: set[str] = set()
    for n in required_env_for(engine_id, spec):
        out.add(n)
        if n.startswith("ARGO_"):
            out.add(n[len("ARGO_"):])
        else:
            out.add(f"ARGO_{n}")
    for n in KNOWN_ENV_ALIASES.get(engine_id, []):
        out.add(n)
        if n.startswith("ARGO_"):
            out.add(n[len("ARGO_"):])
    return out


def _candidate_engines() -> list[tuple[str, dict]]:
    cfg = load_config()
    engs = cfg.get("engines") or {}
    return [(k, v) for k, v in engs.items() if isinstance(v, dict)]


def _spec_placeholder_names(spec: dict) -> set[str]:
    """spec 里 {NAME} 占位符引用的环境变量名。

    声明式 HTTP 引擎（无 Python builder）的取密钥路径：config.yaml 的
    headers/params 写 `X-API-Key: "{ARGO_KEENABLE_API_KEY}"`，请求构建时
    由 engines_base.expand_placeholders 展开——这就是真实消费点。
    """
    out: set[str] = set()

    def _walk(v) -> None:
        if isinstance(v, str):
            out.update(re.findall(r"\{([A-Z][A-Z0-9_]*)\}", v))
        elif isinstance(v, dict):
            for x in v.values():
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)

    _walk(spec)
    return out


class TestEnvDeclarationConsistency:
    """builder 读取的密钥必须已声明，否则状态会误报 ready。"""

    def test_all_builder_secrets_are_declared(self):
        """核心契约：无「读了但没声明」的密钥。"""
        offenders: list[str] = []
        for engine_id, spec in _candidate_engines():
            reads = {n for n in _builder_env_reads(engine_id) if _is_secret(n)}
            if not reads:
                continue
            declared = _declared_names(engine_id, spec)
            undeclared = {n for n in reads if n not in declared
                          and f"ARGO_{n}" not in declared
                          and n.replace("ARGO_", "") not in declared}
            if undeclared:
                offenders.append(
                    f"{engine_id}: builder 读取 {sorted(undeclared)} "
                    f"但未在 required_env/别名表声明")
        assert not offenders, (
            "以下引擎的状态会误报 ready（实际调用必然失败）：\n  "
            + "\n  ".join(offenders)
            + "\n修法：在 config.yaml 对应引擎下加 required_env，"
              "或把密钥名加入 KNOWN_ENV_ALIASES。"
        )

    def test_declared_secrets_are_read_by_builder(self):
        """反向契约（2026-09-29 新增）：声明了的密钥，必须被**变体感知地**消费。

        原测试只拦「读了但没声明」；issue #12 的续发事故是反方向——
        config 声明 `PARALLEL_API_KEY`（状态层经候选链连 ARGO_ 名也认），
        builder 却裸读 `os.environ.get("PARALLEL_API_KEY")`：用户按文档配
        ARGO_PARALLEL_API_KEY 后状态报 ready、执行层取空串，恒 0 结果，
        失败还被 L2 负缓存回放。「任一形式即算声明」的旧判据从设计上放过
        了这个方向，故补本测试。

        消费路径有三条（覆盖任一即算读到）：
          1. builder 闭包内 **get_env/env_flag** 的读取（单名也会经
             _name_variants 展开为候选链，与状态层同源）；
          2. spec 里的 {NAME} 占位符（声明式 HTTP 引擎路径，展开时经
             PLACEHOLDER_ALIASES 变体感知）；
          3. 停用引擎跳过——不可路由即无「状态说谎」路径。
        裸 os.environ.get/os.getenv 只覆盖它写的那一个名字：声明名的另一
        个变体仍在 uncovered 集合里，正是要被抓的形态。
        """
        offenders: list[str] = []
        for engine_id, spec in _candidate_engines():
            declared = {n for n in _declared_names(engine_id, spec)
                        if _is_secret(n)}
            if not declared:
                continue
            if not spec.get("enabled", True):
                continue

            def _variants(n: str) -> set[str]:
                return {n, n[len("ARGO_"):] if n.startswith("ARGO_")
                        else f"ARGO_{n}"}

            covered: set[str] = set()
            raw_only: set[str] = set()
            reads_kind = _builder_env_reads_kind(engine_id)
            for name, aware in reads_kind.items():
                if aware:
                    covered |= _variants(name)
                elif _is_secret(name):
                    raw_only.add(name)  # 裸读只覆盖写到的这一个名字
            for n in _spec_placeholder_names(spec):
                for cand in [n] + list(PLACEHOLDER_ALIASES.get(n, [])):
                    covered |= _variants(cand)
            unread = declared - covered - raw_only
            if unread:
                offenders.append(
                    f"{engine_id}: 声明了 {sorted(unread)} 但无变体感知的消费"
                    f"路径（builder get_env 读取 "
                    f"{sorted(n for n, a in reads_kind.items() if a and _is_secret(n)) or '无'}，"
                    f"裸读 {sorted(raw_only) or '无'}，"
                    f"spec 占位符 {sorted(_spec_placeholder_names(spec)) or '无'}）")
        assert not offenders, (
            "以下引擎的状态层会误报 ready（声明的密钥没有变体感知的消费路径）：\n  "
            + "\n  ".join(offenders)
            + "\n修法：builder 改走 get_env([\"ARGO_<NAME>\", \"<NAME>\"]) "
              "候选链（见 engine_env._name_variants），与状态层同源。"
        )

    def test_no_callable_state_lies_about_secret(self):
        """回归锁定：you / parallel 缺密钥时必须被判为 missing_key。

        这两个是实测发现的真实案例（状态曾报 ready、实际调用恒失败）。

        实现说明——为什么用 `missing_env_for` 而不是 `engine_detail()`：
        `engine_status.engine_detail()` 内部会触达 **adaptive learner 单例**
        （`_runtime_status` → `get_learner().get_score()`），而该单例是跨用例
        共享的全局状态。实测：本测试若调用 `engine_detail()`，会改变后续
        `test_envsync_anysearch_0907` 的 zhihu 路由检查结果（combo 从
        ['zhihu','zhihu_global'] 变成 ['anysearch','local_bing']）——纯属测试
        间污染，产品行为本身正确（直接调 route_query 结果正确）。

        而「状态会不会误报」的**充分条件**就是「声明的密钥是否真的缺失」
        （状态机 `missing_key` 分支只由 `missing_env` 非空触发，
        见 engine_status.engine_detail 的 status 判定链）。
        故检查 missing_env_for 等价且无副作用。
        """
        cfg = load_config()
        for engine_id, secret in (("you", "YDC_API_KEY"),
                                  ("parallel", "PARALLEL_API_KEY")):
            spec = (cfg.get("engines") or {}).get(engine_id) or {}
            declared = _declared_names(engine_id, spec)
            assert secret in declared or f"ARGO_{secret}" in declared, (
                f"{engine_id} 未声明 {secret} → 状态会误报 ready")

            has_key = bool(get_env([f"ARGO_{secret}", secret]))
            if not has_key:
                # 密钥确实缺失 → 必须出现在 missing_env（否则状态机走不到
                # missing_key 分支，会继续报 ready，即「状态说谎」）
                missing = missing_env_for(engine_id, spec)
                assert missing, (
                    f"{engine_id} 缺密钥但 missing_env 为空 → 状态会误报 ready")
                assert any(secret in m or m.endswith(secret) for m in missing), (
                    f"{engine_id} 的 missing_env={missing} 未反映 {secret}")
