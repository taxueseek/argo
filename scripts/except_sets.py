"""except 收窄的具名词汇表——收窄从这里的命名集合里选，不许在调用点现编。

把 20+ 处裸 `except Exception` 逐处收窄时产出过两类事故：元组里引用了
未导入的名字（F821，异常发生时才求值元组 → NameError）；收窄过深
（quota 侧信道放进 RuntimeError，直接打断搜索主流程）。本模块把
「允许收窄成什么」收口成具名集合 + 一条判定规则：ruff F821 负责抓
「名字写错」，这里的规则负责抓「语义选错」。

判定规则——选哪种看 **try 块失败的代价**，不看它可能抛什么：

- 侧信道（失败只影响观测/记账/加速提示，不得影响主流程）：
  保持 `except Exception` 不收窄，调用点加一行注释说明为什么。
- IO_BENIGN：文件/网络系统调用，失败语义是「这次没读到/没写成」。
- SHAPE_BENIGN：数据形状不符，失败语义是「这份数据不可用」。
- OPT_IMPORT：可选模块缺失，走降级路径（`except ImportError` 直接写即可，
  本集合只在与其他集合拼接时使用）。

集合之间可以拼接（如 `IO_BENIGN + SHAPE_BENIGN`）。要收窄的异常不在任何
集合里时，先把失败语义想清楚再改这里，不要在调用点现编一个新元组。
"""

IO_BENIGN: tuple[type[Exception], ...] = (OSError,)
SHAPE_BENIGN: tuple[type[Exception], ...] = (
    AttributeError, IndexError, KeyError, TypeError, ValueError)
OPT_IMPORT: tuple[type[Exception], ...] = (ImportError,)
