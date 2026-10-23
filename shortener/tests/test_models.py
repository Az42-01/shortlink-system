"""数据模型测试：短码生成、唯一性、级联删除。"""

from django.test import TestCase

from shortener.models import ClickLog, ShortLink
from shortener.utils import generate_short_code


class ShortLinkModelTests(TestCase):
    def test_short_code_is_derived_from_autoincrement_id(self):
        link = ShortLink.create_with_short_code("https://example.com/very/long/path")
        self.assertEqual(link.short_code, generate_short_code(link.pk))
        self.assertTrue(4 <= len(link.short_code) <= 6)
        self.assertEqual(link.click_count, 0)
        self.assertIsNotNone(link.created_at)
        self.assertTrue(link.is_active)

    def test_short_codes_are_unique_across_many_links(self):
        codes = set()
        for index in range(200):
            link = ShortLink.create_with_short_code(f"https://example.com/{index}")
            codes.add(link.short_code)
        self.assertEqual(len(codes), 200)

    def test_short_path_matches_redirect_route(self):
        link = ShortLink.create_with_short_code("https://example.com/a")
        self.assertEqual(link.short_path, f"/{link.short_code}/")

    def test_pending_short_code_is_allowed(self):
        """插入时允许 short_code 为空（先拿主键再回填），唯一索引不冲突。"""
        first = ShortLink.objects.create(original_url="https://example.com/1")
        second = ShortLink.objects.create(original_url="https://example.com/2")
        self.assertIsNone(first.short_code)
        self.assertIsNone(second.short_code)

    def test_active_queryset_filters_inactive_links(self):
        active = ShortLink.create_with_short_code("https://example.com/active")
        inactive = ShortLink.create_with_short_code("https://example.com/inactive")
        ShortLink.objects.filter(pk=inactive.pk).update(is_active=False)
        self.assertEqual(list(ShortLink.objects.active()), [active])

    def test_duplicate_short_code_is_rejected(self):
        link = ShortLink.create_with_short_code("https://example.com/a")
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            ShortLink.objects.create(
                original_url="https://example.com/b", short_code=link.short_code
            )


class ClickLogModelTests(TestCase):
    def setUp(self):
        self.link = ShortLink.create_with_short_code("https://example.com/a")

    def test_click_log_defaults(self):
        log = ClickLog.objects.create(link=self.link, ip_address="203.0.113.9", user_agent="pytest")
        self.assertEqual(log.link_id, self.link.pk)
        self.assertIsNotNone(log.created_at)
        self.assertEqual(str(log).split(" @ ")[0], str(self.link.pk))

    def test_click_log_accepts_empty_ip(self):
        log = ClickLog.objects.create(link=self.link, user_agent="")
        self.assertIsNone(log.ip_address)

    def test_deleting_link_cascades_to_click_logs(self):
        ClickLog.objects.create(link=self.link, ip_address="203.0.113.9")
        self.link.delete()
        self.assertEqual(ClickLog.objects.count(), 0)
