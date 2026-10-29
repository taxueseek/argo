"""Argo packaged launcher.

PyPI/uvx 安装的是这个包；真正的运行树在 ``argo_search/_data`` 下，由
``scripts/build_release_assets.sh`` 在构建 wheel 时复制进来。这样
``pip install argo-search`` / ``uvx argo-search`` 安装后，``argo`` 与
``argo-mcp`` 仍能找到完整的 config/engines/sub-skills 树。
"""

__version__ = "2.10.1"
