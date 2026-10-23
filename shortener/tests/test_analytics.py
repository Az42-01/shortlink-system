"""数据看板聚合逻辑的测试：7 天点击趋势 + 设备分布。"""

from datetime import date, datetime

from django.test import TestCase
from django.utils import timezone

from shortener import analytics
from shortener.models import ClickLog, ShortLink

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)
OTHER_UA = "curl/8.5.0"


class AnalyticsTestBase(TestCase):
    def setUp(self):
        self.link = ShortLink.create_with_short_code("https://example.com/report")
        # 固定「今天」，让 7 天窗口与当前时间无关
        self.today = date(2026, 10, 20)

    def make_click(self, moment=None, user_agent=PC_UA, ip_address="203.0.113.1"):
        click = ClickLog.objects.create(
            link=self.link,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        if moment is not None:
            # created_at 是 auto_now_add，创建后回写才能模拟历史数据
            ClickLog.objects.filter(pk=click.pk).update(created_at=moment)
        return click

    @staticmethod
    def local(*args):
        """构造本地时区的 datetime，例如 local(2026, 10, 18, 10, 0)。"""
        return datetime(*args, tzinfo=timezone.get_current_timezone())


class DailyClickCountTests(AnalyticsTestBase):
    def test_series_has_fixed_length_and_fills_missing_days_with_zero(self):
        self.make_click(self.local(2026, 10, 14, 9, 0))
        series = analytics.daily_click_counts(self.link, days=7, today=self.today)

        self.assertEqual(len(series), 7)
        self.assertEqual(series[0][0], date(2026, 10, 14))
        self.assertEqual(series[-1][0], self.today)
        self.assertEqual([count for _, count in series], [1, 0, 0, 0, 0, 0, 0])

    def test_clicks_are_grouped_per_day(self):
        self.make_click(self.local(2026, 10, 18, 10, 0))
        self.make_click(self.local(2026, 10, 18, 22, 30))
        self.make_click(self.local(2026, 10, 20, 8, 15))

        series = analytics.daily_click_counts(self.link, days=7, today=self.today)
        self.assertEqual([count for _, count in series], [0, 0, 0, 0, 2, 0, 1])

    def test_clicks_outside_window_are_ignored(self):
        self.make_click(self.local(2026, 10, 13, 23, 59))
        self.make_click(self.local(2026, 10, 20, 10, 0))

        series = analytics.daily_click_counts(self.link, days=7, today=self.today)
        self.assertEqual([count for _, count in series], [0, 0, 0, 0, 0, 0, 1])

    def test_clicks_are_grouped_by_local_date(self):
        # 2026-10-19 16:30 UTC == 2026-10-20 00:30 本地时间（Asia/Shanghai）
        utc_moment = datetime(2026, 10, 19, 16, 30, tzinfo=timezone.utc)
        self.make_click(utc_moment)

        series = analytics.daily_click_counts(self.link, days=7, today=self.today)
        counts = dict(series)
        self.assertEqual(counts[date(2026, 10, 20)], 1)
        self.assertEqual(counts[date(2026, 10, 19)], 0)

    def test_days_must_be_positive(self):
        with self.assertRaises(ValueError):
            analytics.daily_click_counts(self.link, days=0)

    def test_other_links_are_not_counted(self):
        other = ShortLink.create_with_short_code("https://example.com/other")
        ClickLog.objects.create(link=other, ip_address="203.0.113.77", user_agent=PC_UA)

        series = analytics.daily_click_counts(self.link, days=7, today=self.today)
        self.assertEqual(sum(count for _, count in series), 0)


class DeviceDistributionTests(AnalyticsTestBase):
    def test_counts_each_device_type(self):
        self.make_click(user_agent=PC_UA)
        self.make_click(user_agent=MAC_UA, ip_address="203.0.113.6")
        self.make_click(user_agent=MOBILE_UA, ip_address="203.0.113.2")
        self.make_click(user_agent=MOBILE_UA, ip_address="203.0.113.3")
        self.make_click(user_agent=OTHER_UA, ip_address="203.0.113.4")
        self.make_click(user_agent="", ip_address="203.0.113.5")

        devices = analytics.device_distribution(self.link)
        self.assertEqual([item["key"] for item in devices], ["pc", "mobile", "other"])
        self.assertEqual([item["label"] for item in devices], ["PC", "移动端", "其他"])
        self.assertEqual([item["value"] for item in devices], [2, 2, 2])

    def test_returns_three_buckets_even_without_clicks(self):
        devices = analytics.device_distribution(self.link)
        self.assertEqual(len(devices), 3)
        self.assertEqual([item["value"] for item in devices], [0, 0, 0])


class StatsPayloadTests(AnalyticsTestBase):
    def test_payload_contains_trend_devices_and_summary(self):
        self.make_click(self.local(2026, 10, 20, 9, 0), PC_UA, "203.0.113.1")
        self.make_click(self.local(2026, 10, 20, 10, 0), PC_UA, "203.0.113.2")
        self.make_click(self.local(2026, 10, 19, 10, 0), MOBILE_UA, "203.0.113.3")
        ShortLink.objects.filter(pk=self.link.pk).update(click_count=3)
        self.link.refresh_from_db()

        payload = analytics.build_stats_payload(self.link, today=self.today)

        self.assertEqual(payload["short_code"], self.link.short_code)
        self.assertEqual(payload["original_url"], self.link.original_url)
        self.assertTrue(payload["generated_at"])

        self.assertEqual(payload["trend"]["days"], 7)
        self.assertEqual(len(payload["trend"]["labels"]), 7)
        self.assertEqual(payload["trend"]["total"], 3)
        self.assertEqual(payload["trend"]["values"][-1], 2)

        self.assertEqual(payload["devices"]["values"], [2, 1, 0])
        self.assertEqual(payload["devices"]["total"], 3)
        self.assertEqual(payload["devices"]["top"], "pc")
        self.assertEqual(payload["devices"]["top_label"], "PC")

        self.assertEqual(payload["summary"]["click_count"], 3)
        self.assertEqual(payload["summary"]["unique_visitors"], 3)
        self.assertTrue(payload["summary"]["created_at"])

    def test_top_is_none_without_clicks(self):
        payload = analytics.build_stats_payload(self.link, today=self.today)
        self.assertIsNone(payload["devices"]["top"])
        self.assertEqual(payload["devices"]["total"], 0)
        self.assertEqual(payload["trend"]["total"], 0)

