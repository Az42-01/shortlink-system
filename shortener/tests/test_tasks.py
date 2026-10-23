"""点击日志缓冲区测试：批量落库、点击计数、队列满降级、真实后台线程。"""

from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings

from shortener.models import ClickLog, ShortLink
from shortener.tasks import ClickEvent, ClickLogBuffer, record_click


class ClickLogBufferTests(TestCase):
    def setUp(self):
        self.buffer = ClickLogBuffer(
            maxsize=100, batch_size=200, flush_interval=0.05, name="test-writer"
        )
        self.first = ShortLink.create_with_short_code("https://example.com/1")
        self.second = ShortLink.create_with_short_code("https://example.com/2")

    def test_flush_writes_logs_and_updates_click_count(self):
        events = [
            ClickEvent(link_id=self.first.pk, ip_address="203.0.113.1", user_agent="UA-A")
            for _ in range(3)
        ] + [ClickEvent(link_id=self.second.pk, ip_address="203.0.113.2", user_agent="UA-B")]

        self.assertEqual(self.buffer.flush_now(events), 4)

        self.assertEqual(ClickLog.objects.count(), 4)
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.assertEqual(self.first.click_count, 3)
        self.assertEqual(self.second.click_count, 1)
        self.assertEqual(self.buffer.stats.flushed, 4)

    def test_flush_accumulates_on_existing_counts(self):
        ShortLink.objects.filter(pk=self.first.pk).update(click_count=10)
        self.buffer.flush_now([ClickEvent(link_id=self.first.pk)])
        self.first.refresh_from_db()
        self.assertEqual(self.first.click_count, 11)

    def test_empty_flush_is_noop(self):
        self.assertEqual(self.buffer.flush_now([]), 0)
        self.assertEqual(ClickLog.objects.count(), 0)

    def test_queue_full_drops_event_without_blocking(self):
        buffer = ClickLogBuffer(maxsize=1, batch_size=10, flush_interval=10)
        with mock.patch.object(buffer, "_ensure_worker"):  # 阻止后台线程消费，模拟积压
            self.assertTrue(buffer.submit(ClickEvent(link_id=self.first.pk)))
            self.assertFalse(buffer.submit(ClickEvent(link_id=self.first.pk)))
        self.assertEqual(buffer.stats.accepted, 1)
        self.assertEqual(buffer.stats.dropped, 1)

        buffer.flush_now()
        self.assertEqual(ClickLog.objects.count(), 1)

    def test_flush_failure_is_swallowed(self):
        with mock.patch.object(
            ClickLog.objects, "bulk_create", side_effect=RuntimeError("db down")
        ):
            self.assertEqual(self.buffer.flush_now([ClickEvent(link_id=self.first.pk)]), 0)
        self.assertEqual(self.buffer.stats.failed_batches, 1)
        self.assertEqual(ClickLog.objects.count(), 0)


@override_settings(CLICK_LOG_ASYNC=False)
class RecordClickSyncTests(TestCase):
    def setUp(self):
        self.link = ShortLink.create_with_short_code("https://example.com/a")

    def test_record_click_writes_synchronously(self):
        record_click(self.link.pk, ip_address="203.0.113.9", user_agent="Mozilla/5.0")
        log = ClickLog.objects.get()
        self.assertEqual(log.ip_address, "203.0.113.9")
        self.assertEqual(log.user_agent, "Mozilla/5.0")
        self.link.refresh_from_db()
        self.assertEqual(self.link.click_count, 1)

    def test_record_click_truncates_long_user_agent(self):
        record_click(self.link.pk, ip_address="", user_agent="x" * 900)
        log = ClickLog.objects.get()
        self.assertEqual(len(log.user_agent), 512)
        self.assertIsNone(log.ip_address)

    def test_record_click_without_link_id_is_noop(self):
        record_click(0)
        self.assertEqual(ClickLog.objects.count(), 0)


class ClickLogBufferThreadTests(TransactionTestCase):
    """真实后台线程的冒烟测试（需要能看到已提交的数据，故用 TransactionTestCase）。"""

    def test_background_thread_flushes_events(self):
        link = ShortLink.create_with_short_code("https://example.com/thread")
        buffer = ClickLogBuffer(maxsize=100, batch_size=10, flush_interval=0.05)

        for _ in range(5):
            self.assertTrue(
                buffer.submit(
                    ClickEvent(link_id=link.pk, ip_address="203.0.113.5", user_agent="UA")
                )
            )

        buffer.stop(timeout=5.0)  # 等待队列排空

        self.assertEqual(ClickLog.objects.filter(link_id=link.pk).count(), 5)
        link.refresh_from_db()
        self.assertEqual(link.click_count, 5)
