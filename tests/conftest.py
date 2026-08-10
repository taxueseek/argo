#!/usr/bin/env python3
"""pytest 全局配置。

存量测试大量 mock urllib.request.urlopen 验证引擎 URL 构造（setlang/lang 等），
HttpClient 接入后这些 mock 不再生效。默认回退 urllib 路径，保证存量断言行为
不变；HttpClient 新行为的专项测试显式 monkeypatch.setenv 开启。
"""

import os

os.environ.setdefault("ARGO_ENGINE_HTTP_CLIENT", "0")
