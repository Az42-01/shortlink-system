"""视图测试：创建短链、缓存命中/回源、301 跳转、点击日志、健康检查。"""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from shortener import views
from shortener.models import ClickLog, ShortLink
from shortener.redis_client import ShortLinkCache

from .fakes import BrokenRedisClient, FakeRedisClient


class ViewTestBase(TestCase):
    def setUp(self):
        self.fake = FakeRedisClient()
        self.cache = ShortLinkCache(client_factory=lambda: self.fake)
        patcher = mock.patch.object(views, "shortlink_cache", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)


class CreateLinkViewTests(ViewTestBase):
    def test_get_renders_form(self):
        response = self.client.get(reverse("shortener:create_link"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "把长链接变短")
        self.assertContains(response, 'name="original_url"')

    def test_post_creates_link_and_writes_cache(self):
        response = self.client.post(
            reverse("shortener:create_link"),
            {"original_url": "example.com/landing?x=1"},
        )
        self.assertEqual(response.status_code, 200)

        link = ShortLink.objects.get()
        self.assertEqual(link.original_url, "http://example.com/landing?x=1")
        self.assertContains(response, "短链接已生成")
        self.assertContains(response, f"http://testserver/{link.short_code}/")
        # 写穿缓存
        self.assertIn(f"shortlink:code:{link.short_code}", self.fake.store)

    def test_post_rejects_unsupported_scheme(self):
        response = self.client.post(
            reverse("shortener:create_link"), {"original_url": "ftp://example.com/file"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ShortLink.objects.count(), 0)
        self.assertContains(response, "仅支持 http / https 链接")

    def test_post_rejects_empty_url(self):
        response = self.client.post(
            reverse("shortener:create_link"), {"original_url": "   "}
        )
        self.assertEqual(ShortLink.objects.count(), 0)
        self.assertContains(response, "请填写需要缩短的链接")

    def test_recent_links_are_listed(self):
        link = ShortLink.create_with_short_code("https://example.com/recent")
        response = self.client.get(reverse("shortener:create_link"))
        self.assertContains(response, link.short_code)


@override_settings(CLICK_LOG_ASYNC=False)
class RedirectLinkViewTests(ViewTestBase):
    def setUp(self):
        super().setUp()
        self.link = ShortLink.create_with_short_code("https://example.com/target?utm=1")
        self.url = reverse("shortener:redirect", args=[self.link.short_code])

    def test_redirects_with_301_and_backfills_cache(self):
        first = self.client.get(self.url)
        self.assertEqual(first.status_code, 301)
        self.assertEqual(first["Location"], "https://example.com/target?utm=1")
        self.assertEqual(first["X-Shortlink-Cache"], "MISS")
        self.assertIn(f"shortlink:code:{self.link.short_code}", self.fake.store)

        second = self.client.get(self.url)
        self.assertEqual(second.status_code, 301)
        self.assertEqual(second["X-Shortlink-Cache"], "HIT")

    def test_cache_hit_skips_database(self):
        self.cache.set(self.link.short_code, self.link.pk, self.link.original_url)
        with mock.patch.object(views, "record_click") as record_click_mock:
            with self.assertNumQueries(0):
                response = self.client.get(self.url)
        self.assertEqual(response.status_code, 301)
        record_click_mock.assert_called_once()
        self.assertEqual(record_click_mock.call_args.kwargs["link_id"], self.link.pk)

    def test_cache_takes_precedence_over_database(self):
        self.cache.set(self.link.short_code, self.link.pk, "https://cached.example.com/")
        response = self.client.get(self.url)
        self.assertEqual(response["Location"], "https://cached.example.com/")

    def test_unknown_code_returns_404(self):
        response = self.client.get(reverse("shortener:redirect", args=["zzzz"]))
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "短链接不存在", status_code=404)

    def test_inactive_link_returns_404(self):
        ShortLink.objects.filter(pk=self.link.pk).update(is_active=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_click_log_is_recorded_with_ip_and_user_agent(self):
        self.client.get(
            self.url,
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            REMOTE_ADDR="203.0.113.42",
            HTTP_X_FORWARDED_FOR="198.51.100.7",
        )
        log = ClickLog.objects.get()
        self.assertEqual(log.ip_address, "198.51.100.7")
        self.assertIn("Windows NT 10.0", log.user_agent)
        self.link.refresh_from_db()
        self.assertEqual(self.link.click_count, 1)

    def test_redirect_still_works_when_redis_is_down(self):
        broken = ShortLinkCache(client_factory=lambda: BrokenRedisClient())
        with mock.patch.object(views, "shortlink_cache", broken):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], self.link.original_url)



class HealthCheckViewTests(ViewTestBase):
    def test_reports_degraded_when_redis_is_down(self):
        with mock.patch.object(views, "ping_redis", return_value=False):
            response = self.client.get(reverse("shortener:healthz"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["database"])
        self.assertFalse(payload["redis"])
        self.assertTrue(payload["cache_degraded"])

    def test_reports_ok_when_redis_is_up(self):
        with mock.patch.object(views, "ping_redis", return_value=True):
            payload = self.client.get(reverse("shortener:healthz")).json()
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["redis"])
        self.assertFalse(payload["cache_degraded"])


class StatsViewTests(ViewTestBase):
    def test_stats_page_shows_clicks_and_unique_visitors(self):
        link = ShortLink.create_with_short_code("https://example.com/a")
        ShortLink.objects.filter(pk=link.pk).update(click_count=7)
        ClickLog.objects.create(link=link, ip_address="203.0.113.9", user_agent="UA-Test")
        ClickLog.objects.create(link=link, ip_address="203.0.113.9", user_agent="UA-Test")

        response = self.client.get(reverse("shortener:link_stats", args=[link.short_code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "累计点击")
        self.assertContains(response, "203.0.113.9")
        self.assertContains(response, "UA-Test")
        self.assertEqual(response.context["link"].click_count, 7)
        self.assertEqual(response.context["unique_visitors"], 1)

    def test_unknown_short_code_returns_404(self):
        response = self.client.get(reverse("shortener:link_stats", args=["zzzz"]))
        self.assertEqual(response.status_code, 404)


class StatsDashboardTests(ViewTestBase):
    """看板页面：ECharts 容器、JSON 接口地址、设备标签。"""

    def test_dashboard_renders_chart_containers_and_api_url(self):
        link = ShortLink.create_with_short_code("https://example.com/dashboard")
        ClickLog.objects.create(
            link=link,
            ip_address="203.0.113.8",
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) Mobile/15E148",
        )

        response = self.client.get(
            reverse("shortener:link_stats", args=[link.short_code])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="trend-chart"')
        self.assertContains(response, 'id="device-chart"')
        self.assertContains(response, "echarts.min.js")
        self.assertContains(
            response, reverse("shortener:link_stats_api", args=[link.short_code])
        )
        self.assertContains(response, "点击趋势")
        self.assertContains(response, "访问设备分布")
        # 唯一的点击是 iPhone，主要设备应为「移动端」
        self.assertEqual(response.context["top_device_label"], "移动端")
        self.assertEqual(response.context["trend_total"], 1)
        self.assertEqual(response.context["device_total"], 1)


class StatsApiViewTests(ViewTestBase):
    """JSON 接口：近 7 天趋势 + 设备分布。"""

    PC_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Mobile/15E148"
    )

    def setUp(self):
        super().setUp()
        self.link = ShortLink.create_with_short_code("https://example.com/api")
        self.url = reverse("shortener:link_stats_api", args=[self.link.short_code])

    def _log(self, user_agent, ip_address):
        return ClickLog.objects.create(
            link=self.link, user_agent=user_agent, ip_address=ip_address
        )

    def test_returns_trend_and_device_distribution(self):
        self._log(self.PC_UA, "203.0.113.1")
        self._log(self.PC_UA, "203.0.113.2")
        self._log(self.MOBILE_UA, "203.0.113.3")

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "no-store")

        payload = response.json()
        self.assertEqual(payload["short_code"], self.link.short_code)
        self.assertEqual(payload["original_url"], self.link.original_url)

        self.assertEqual(payload["trend"]["days"], 7)
        self.assertEqual(len(payload["trend"]["labels"]), 7)
        self.assertEqual(len(payload["trend"]["values"]), 7)
        # 三条日志都是今天写入的，应该全部落在最后一天
        self.assertEqual(sum(payload["trend"]["values"]), 3)
        self.assertEqual(payload["trend"]["values"][-1], 3)

        self.assertEqual(payload["devices"]["keys"], ["pc", "mobile", "other"])
        self.assertEqual(payload["devices"]["labels"], ["PC", "移动端", "其他"])
        self.assertEqual(payload["devices"]["values"], [2, 1, 0])
        self.assertEqual(payload["devices"]["total"], 3)
        self.assertEqual(payload["devices"]["top"], "pc")
        self.assertEqual(payload["summary"]["unique_visitors"], 3)

    def test_unknown_short_code_returns_json_404(self):
        response = self.client.get(reverse("shortener:link_stats_api", args=["zzzz"]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("不存在", response.json()["detail"])

    def test_only_get_is_allowed(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 405)

