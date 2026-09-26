#!/usr/bin/env python3
"""test_conn_pool.py — HTTP 连接池的单元 + 端到端测试。

锁定的契约：
  1. 借还语义：give_back 后的连接被下次 borrow 复用（factory 不再调用）；
  2. discard 的连接不进池（陈旧 / 异常路径必须弃用，不能把死连接发给下一个人）；
  3. idle TTL：超过 _IDLE_TTL_S 的连接借出前即被关闭；
  4. 每键 idle 上限：超限还回即关闭（有界，不长驻进程无界囤积）；
  5. ARGO_HTTP_POOL=0：整池退化为「一次请求一条连接」（对拍/应急开关）；
  6. 端到端：同一主机两次 GET 只建一条 TCP 连接（keep-alive 真复用），
     且第二次请求仍拿到正确响应（连接复用没有串响应体）。
"""

import http.client
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import conn_pool  # noqa: E402


class _FakeConn:
    """记录 close 次数的假连接。"""

    def __init__(self, tag=""):
        self.tag = tag
        self.closed = False
        self.sock = _FakeSock()

    def close(self):
        self.closed = True


class _FakeSock:
    def __init__(self):
        self._fd = 5
        self.timeout = None

    def fileno(self):
        return self._fd

    def settimeout(self, t):
        self.timeout = t


def _parsed(url):
    from urllib.parse import urlparse
    return urlparse(url)


class TestBorrowGiveBack(unittest.TestCase):
    def setUp(self):
        conn_pool.clear()
        self.addCleanup(conn_pool.clear)

    def test_borrow_creates_via_factory(self):
        calls = []

        def factory(parsed, timeout, proxy):
            calls.append((parsed.hostname, timeout, proxy))
            return _FakeConn("c1")

        conn, key, reused = conn_pool.borrow(
            _parsed("https://a.example/x"), 5.0, None, factory)
        self.assertFalse(reused)
        self.assertEqual(len(calls), 1)
        self.assertEqual(key, ("https", "a.example", 443, ""))
        self.assertFalse(conn.closed)

    def test_give_back_then_borrow_reuses(self):
        made = []

        def factory(parsed, timeout, proxy):
            c = _FakeConn(f"c{len(made)}")
            made.append(c)
            return c

        c1, k1, r1 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        conn_pool.give_back(k1, c1)
        c2, k2, r2 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        self.assertTrue(r2, "还回后应复用")
        self.assertIs(c1, c2, "复用的必须是同一条连接")
        self.assertEqual(len(made), 1, "复用路径不应调用 factory")
        # 复用连接按本次 timeout 重设 socket 超时
        self.assertEqual(c2.sock.timeout, 5.0)

    def test_discarded_conn_not_reused(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn()

        c1, k1, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        conn_pool.discard(k1, c1)
        self.assertTrue(c1.closed, "discard 必须关闭连接")
        c2, _, r2 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        self.assertFalse(r2, "discard 过的连接不能被复用")
        self.assertIsNot(c1, c2)

    def test_different_hosts_do_not_share(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn(parsed.hostname)

        c1, k1, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        conn_pool.give_back(k1, c1)
        c2, _, r2 = conn_pool.borrow(_parsed("https://b.example/"), 5.0, None, factory)
        self.assertFalse(r2, "不同主机不共享连接")

    def test_proxy_in_key(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn()

        c1, k1, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0,
                                     "http://p:8080", factory)
        conn_pool.give_back(k1, c1)
        _, k2, r2 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        self.assertFalse(r2, "代理与直连不共享连接（出口不同，隧道/Host 语义不同）")
        self.assertNotEqual(k1, k2)


class TestBounds(unittest.TestCase):
    def setUp(self):
        conn_pool.clear()
        self.addCleanup(conn_pool.clear)

    def test_idle_ttl_expires(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn()

        c1, k1, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        conn_pool.give_back(k1, c1)
        # 把归还时间戳改到很久以前（绕过真实等待）
        q = conn_pool._pools[k1]
        old_conn, _ = q.get_nowait()
        conn_pool._total_idle -= 1
        q.put((old_conn, time.monotonic() - conn_pool._IDLE_TTL_S - 1))
        conn_pool._total_idle += 1
        c2, _, r2 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
        self.assertFalse(r2, "超过 idle TTL 的连接必须关闭不借出")
        self.assertTrue(old_conn.closed, "过期连接应被关闭")

    def test_max_idle_per_key(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn()

        conns = []
        for _ in range(conn_pool._MAX_IDLE_PER_KEY + 2):
            c, k, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
            conns.append((c, k))
        for c, k in conns:
            conn_pool.give_back(k, c)
        # 超限的那些应被关闭
        closed = sum(1 for c, _ in conns if c.closed)
        self.assertGreaterEqual(closed, 2, "超过每键 idle 上限的连接应被关闭")
        self.assertLessEqual(conn_pool._pools[conns[0][1]].qsize(),
                             conn_pool._MAX_IDLE_PER_KEY)


class TestPoolSwitch(unittest.TestCase):
    def setUp(self):
        conn_pool.clear()
        self.addCleanup(conn_pool.clear)

    def test_disable_env_bypasses_pool(self):
        def factory(parsed, timeout, proxy):
            return _FakeConn()

        with patch.dict(os.environ, {"ARGO_HTTP_POOL": "0"}):
            c1, k1, _ = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
            conn_pool.give_back(k1, c1)  # 关闭而非入池
            self.assertTrue(c1.closed, "开关关闭时 give_back 应立即关闭")
            c2, _, r2 = conn_pool.borrow(_parsed("https://a.example/"), 5.0, None, factory)
            self.assertFalse(r2, "开关关闭时不复用")


class _CountingHandler(BaseHTTPRequestHandler):
    connections = 0
    requests = 0
    # HTTP/1.1 + 显式 Content-Length 才启用 keep-alive；默认的 HTTP/1.0
    # 每条响应都带 Connection: close，池化无从验证。
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        type(self).connections += 1

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).requests += 1
        body = b"hello-argo"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestEndToEndKeepAlive(unittest.TestCase):
    """端到端：HttpClient 对同一主机两次 GET 只建一条 TCP 连接。"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CountingHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        conn_pool.clear()
        _CountingHandler.connections = 0
        _CountingHandler.requests = 0
        self.addCleanup(conn_pool.clear)

    def test_second_get_reuses_connection(self):
        from http_client import HttpClient
        url = f"http://127.0.0.1:{self.port}/x"
        client = HttpClient(timeout=5.0, max_retries=0, jitter=False)
        # 127.0.0.1 是私有地址：SSRF 防护默认拒绝，测试显式放行
        with patch.dict(os.environ, {"ARGO_ALLOW_PRIVATE_URLS": "1"}):
            r1 = client.get(url)
            r2 = client.get(url)

        self.assertEqual(r1["status"], 200)
        self.assertEqual(r2["status"], 200)
        self.assertEqual(r1["text"], "hello-argo")
        self.assertEqual(r2["text"], "hello-argo",
                         "复用连接串了响应体——读干/归还契约被破坏")
        self.assertEqual(_CountingHandler.requests, 2, "两次请求都应到达服务端")
        # 服务端视角只 accept 过一条连接（第二次走 keep-alive）
        self.assertLessEqual(_CountingHandler.connections, 1,
                             "第二次 GET 没有复用连接（又建了一条 TCP）")

    def test_pool_stats_visible(self):
        from http_client import HttpClient
        url = f"http://127.0.0.1:{self.port}/y"
        client = HttpClient(timeout=5.0, max_retries=0, jitter=False)
        with patch.dict(os.environ, {"ARGO_ALLOW_PRIVATE_URLS": "1"}):
            client.get(url)
        st = conn_pool.stats()
        self.assertTrue(st["enabled"])
        self.assertGreaterEqual(st["idle"], 1, "请求结束后连接应还池")
        self.assertIn(("http", "127.0.0.1", self.port, ""), st["live"])


if __name__ == "__main__":
    unittest.main()
