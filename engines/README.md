# engines/ — 外置引擎声明与插件

| 路径 | 用途 |
|------|------|
| `_template_http.yaml` | L1/L2 HTTP 引擎模板（复制到 `specs/`） |
| `specs/*.yaml` | 声明式引擎（启动时自动 merge） |
| `plugins/*.py` | L3 自定义 builder（`ENGINE_TYPE` + `build_engine`） |
| `plugins/_template_plugin.py` | 插件模板（`_` 前缀不会加载） |

完整流程见 `docs/ADDING_NEW_ENGINE.md`。
