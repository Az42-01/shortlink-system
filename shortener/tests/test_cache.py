"""Redis 缓存层测试：读写、TTL、降级、脏数据处理。"""

from django.test import SimpleTestCase, override_settings

from shortener.redis_client import CachedLink, ShortLinkCache

from .fakes import BrokenRedisClient, FakeRedisClient


@override_settings(SHORTLINK_CACHE_TTL=3600)
class ShortLinkCacheTests(SimpleTestCase):
    def setUp(self):
        self.fake = FakeRedisClient()
        self.cache = ShortLinkCache(client_factory=lambda: self.fake)

    def test_set_then_get_round_trip(self):
        self.assertTrue(self.cache.set("aB3x", 12345, "https://example.com/landing"))
        cached = self.cache.get("aB3x")
        self.assertEqual(cached, CachedLink(link_id=12345, original_url="https://example.com/landing"))
        # link_id 也进了缓存，跳转时不需要回查数据库
        self.assertEqual(self.fake.get_calls, ["shortlink:code:aB3x"])

    def test_setex_uses_configured_ttl_and_namespaced_key(self):
        self.cache.set("aB3x", 1, "https://example.com")
        key, ttl, payload = self.fake.setex_calls[0]
        self.assertEqual(key, "shortlink:code:aB3x")
        self.assertEqual(ttl, 3600)
        self.assertIn('"id":1', payload)

    def test_get_miss_returns_none(self):
        self.assertIsNone(self.cache.get("nope"))

    def test_empty_code_is_ignored(self):
        self.assertIsNone(self.cache.get(""))

    def test_delete_removes_key(self):
        self.cache.set("aB3x", 1, "https://example.com")
        self.assertTrue(self.cache.delete("aB3x"))
        self.assertIsNone(self.cache.get("aB3x"))

    def test_legacy_plain_url_value_is_supported(self):
        self.fake.store["shortlink:code:legacy"] = "https://example.com/legacy"
        cached = self.cache.get("legacy")
        self.assertEqual(cached.original_url, "https://example.com/legacy")
        self.assertEqual(cached.link_id, 0)

    def test_corrupted_json_returns_none(self):
        self.fake.store["shortlink:code:broken"] = "{not-json"
        self.assertIsNone(self.cache.get("broken"))

    def test_bytes_value_is_decoded(self):
        self.fake.store["shortlink:code:raw"] = b"https://example.com/raw"
        self.assertEqual(self.cache.get("raw").original_url, "https://example.com/raw")

    def test_get_many(self):
        self.cache.set("a1", 1, "https://example.com/1")
        self.cache.set("a2", 2, "https://example.com/2")
        result = self.cache.get_many(["a1", "a2", "missing"])
        self.assertEqual(set(result), {"a1", "a2"})
        self.assertEqual(result["a2"].link_id, 2)

    def test_custom_ttl_overrides_settings(self):
        cache = ShortLinkCache(client_factory=lambda: self.fake, ttl=60)
        cache.set("a1", 1, "https://example.com")
        self.assertEqual(self.fake.setex_calls[-1][1], 60)


class ShortLinkCacheDegradationTests(SimpleTestCase):
    """Redis 不可用时必须优雅降级，不能把异常抛给业务代码。"""

    def setUp(self):
        self.cache = ShortLinkCache(client_factory=lambda: BrokenRedisClient())

    def test_get_returns_none_when_redis_is_down(self):
        self.assertIsNone(self.cache.get("aB3x"))

    def test_set_returns_false_when_redis_is_down(self):
        self.assertFalse(self.cache.set("aB3x", 1, "https://example.com"))

    def test_delete_returns_false_when_redis_is_down(self):
        self.assertFalse(self.cache.delete("aB3x"))

    def test_get_many_returns_empty_when_redis_is_down(self):
        self.assertEqual(self.cache.get_many(["aB3x", "b4Yz"]), {})
