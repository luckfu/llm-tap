import json
import os
import socket
import tempfile
import unittest

from proxy_oneapi import (
    ProxyServer,
    SafeUpstreamResolver,
    _call_filter_clause,
    normalize_upstream_host,
    upstream_host_allowed,
    verify_auth,
)
from raw_storage import _sanitize_headers


class UpstreamHostTests(unittest.TestCase):
    def test_normalizes_dns_and_ipv4_hosts(self):
        self.assertEqual(normalize_upstream_host("API.OpenAI.com."), "api.openai.com")
        self.assertEqual(normalize_upstream_host("127.0.0.1"), "127.0.0.1")

    def test_rejects_ambiguous_or_path_like_hosts(self):
        for host in ("localhost", "example", "user@example.com", "example.com:443", "../tmp", "::1"):
            with self.subTest(host=host):
                self.assertIsNone(normalize_upstream_host(host))

    def test_allowlist_supports_exact_and_subdomain_patterns(self):
        allowlist = ["api.openai.com", "*.example.com"]
        self.assertTrue(upstream_host_allowed("api.openai.com", allowlist))
        self.assertTrue(upstream_host_allowed("llm.example.com", allowlist))
        self.assertFalse(upstream_host_allowed("example.com", allowlist))
        self.assertFalse(upstream_host_allowed("notexample.com", allowlist))

    def test_private_ip_requires_an_explicit_allowlist_entry(self):
        self.assertFalse(upstream_host_allowed("127.0.0.1", []))
        self.assertFalse(upstream_host_allowed("192.168.1.10", ["api.example.com"]))
        self.assertTrue(upstream_host_allowed("192.168.1.10", ["192.168.1.10"]))


class CallFilterTests(unittest.TestCase):
    def test_uses_exact_distinct_fields_and_inclusive_time_range(self):
        where, params = _call_filter_clause({
            "host": "api.example.com",
            "model": "model-a",
            "start_time": "2026-07-18T12:00:00",
            "end_time": "2026-07-18T13:30:59.999999",
        })
        self.assertIn("upstream_provider = ?", where)
        self.assertIn("upstream_model = ?", where)
        self.assertIn("started_at >= ?", where)
        self.assertIn("started_at <= ?", where)
        self.assertEqual(params, [
            "api.example.com", "model-a",
            "2026-07-18T12:00:00", "2026-07-18T13:30:59.999999",
        ])

    def test_rejects_invalid_or_reversed_time_range(self):
        with self.assertRaisesRegex(ValueError, "invalid start_time"):
            _call_filter_clause({"start_time": "not-a-time"})
        with self.assertRaisesRegex(ValueError, "must not be after"):
            _call_filter_clause({
                "start_time": "2026-07-18T14:00:00",
                "end_time": "2026-07-18T13:00:00",
            })


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    class Resolver:
        def __init__(self, address):
            self.address = address
            self.closed = False

        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{
                "hostname": host,
                "host": self.address,
                "port": port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }]

        async def close(self):
            self.closed = True

    async def test_blocks_dns_resolution_to_private_address(self):
        resolver = SafeUpstreamResolver([], delegate=self.Resolver("127.0.0.1"))
        with self.assertRaisesRegex(OSError, "non-public"):
            await resolver.resolve("api.example.com", 443)

    async def test_explicit_allowlist_permits_private_resolution(self):
        delegate = self.Resolver("10.0.0.2")
        resolver = SafeUpstreamResolver(["api.example.com"], delegate=delegate)
        result = await resolver.resolve("api.example.com", 443)
        self.assertEqual(result[0]["host"], "10.0.0.2")
        await resolver.close()
        self.assertTrue(delegate.closed)

    async def test_allows_public_resolution(self):
        resolver = SafeUpstreamResolver([], delegate=self.Resolver("93.184.216.34"))
        result = await resolver.resolve("example.com", 443)
        self.assertEqual(result[0]["host"], "93.184.216.34")


class BindPolicyTests(unittest.TestCase):
    def _config_file(self, config):
        temp = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        json.dump(config, temp)
        temp.close()
        self.addCleanup(lambda: os.unlink(temp.name))
        return temp.name

    def test_loopback_is_the_default_and_applies_request_limit(self):
        server = ProxyServer(self._config_file({"request_max_bytes": 2_000_000}))
        self.assertEqual(server.bind, "127.0.0.1")
        self.assertEqual(server.app._client_max_size, 2_000_000)

    def test_remote_bind_requires_allowlist_but_not_token(self):
        with self.assertRaisesRegex(ValueError, "upstream_allowlist"):
            ProxyServer(self._config_file({}), bind="0.0.0.0")
        with self.assertRaisesRegex(ValueError, "upstream_allowlist"):
            ProxyServer(self._config_file({"proxy_tokens": ["secret"]}), bind="0.0.0.0")

    def test_remote_bind_accepts_allowlist_without_token(self):
        server = ProxyServer(self._config_file({
            "upstream_allowlist": ["api.openai.com"],
        }), bind="0.0.0.0")
        self.assertEqual(server.bind, "0.0.0.0")

    def test_remote_bind_accepts_complete_policy(self):
        server = ProxyServer(self._config_file({
            "proxy_tokens": ["secret"],
            "upstream_allowlist": ["api.openai.com"],
        }), bind="0.0.0.0")
        self.assertEqual(server.bind, "0.0.0.0")


class ProxyTokenTests(unittest.TestCase):
    class Request:
        def __init__(self, headers):
            self.headers = headers

    def test_dedicated_token_does_not_reuse_provider_credentials(self):
        config = {"proxy_tokens": ["proxy-secret"]}
        self.assertTrue(verify_auth(self.Request({
            "X-LLM-Tap-Token": "proxy-secret",
            "Authorization": "Bearer provider-secret",
        }), config))
        self.assertFalse(verify_auth(self.Request({
            "Authorization": "Bearer proxy-secret",
        }), config))

    def test_dedicated_token_is_redacted_from_capture(self):
        headers = _sanitize_headers({"X-LLM-Tap-Token": "proxy-secret", "X-Test": "ok"})
        self.assertEqual(headers["X-LLM-Tap-Token"], "<redacted:len=12>")
        self.assertEqual(headers["X-Test"], "ok")

    def test_dedicated_token_is_not_forwarded_upstream(self):
        server = ProxyServer("/path/that/does/not/exist")
        request = self.Request({
            "X-LLM-Tap-Token": "proxy-secret",
            "Authorization": "Bearer provider-secret",
        })
        request.can_read_body = True
        headers = server._build_upstream_headers(request)
        self.assertNotIn("X-LLM-Tap-Token", headers)
        self.assertEqual(headers["Authorization"], "Bearer provider-secret")


if __name__ == "__main__":
    unittest.main()
