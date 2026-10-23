"""点击日志的异步采集与批量落库。

为什么需要它
-----------
短链跳转是"读多 + 写多"的场景：如果同步 INSERT 点击日志，跳转的尾延迟会被
数据库抖动放大，热点短链还会把单行锁打满。这里的做法：

    request --submit()--> queue.Queue --> 单个后台线程 --> bulk_create + 批量累加 click_count

- 队列有界（默认 10000）：高峰期宁可丢少量统计日志，也不能把内存打爆；
- 线程是 daemon，随进程退出；注册 atexit 在退出前把队列排空；
- 写库使用 ``bulk_create`` + 一条 ``UPDATE ... CASE WHEN``，
  200 条点击只产生 2 条 SQL。

生产环境可以把 ``ClickLogBuffer.submit`` 换成 Celery task / Kafka producer，
视图层代码不需要任何改动。
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from django.conf import settings
from django.db import close_old_connections, transaction
from django.db.models import Case, F, IntegerField, Value, When

from .models import ClickLog, ShortLink
from .utils import truncate_user_agent

logger = logging.getLogger(__name__)

# 队列中的停止哨兵
_STOP = object()


@dataclass(frozen=True)
class ClickEvent:
    """一条待落库的点击事件（尽量轻量，避免占用太多内存）。"""

    link_id: int
    ip_address: Optional[str] = None
    user_agent: str = ""


@dataclass
class BufferStats:
    accepted: int = 0
    dropped: int = 0
    flushed: int = 0
    failed_batches: int = 0

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "dropped": self.dropped,
            "flushed": self.flushed,
            "failed_batches": self.failed_batches,
        }


class ClickLogBuffer:
    """单消费者、批量提交的点击日志缓冲区。"""

    def __init__(
        self,
        *,
        maxsize: Optional[int] = None,
        batch_size: Optional[int] = None,
        flush_interval: Optional[float] = None,
        name: str = "shortlink-click-writer",
    ):
        self._maxsize = maxsize or settings.CLICK_LOG_QUEUE_MAXSIZE
        self._batch_size = max(1, batch_size or settings.CLICK_LOG_BATCH_SIZE)
        self._flush_interval = max(0.05, flush_interval or settings.CLICK_LOG_FLUSH_INTERVAL)
        self._queue: "queue.Queue" = queue.Queue(maxsize=self._maxsize)
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._stopping = threading.Event()
        self._name = name
        self.stats = BufferStats()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def submit(self, event: ClickEvent) -> bool:
        """非阻塞入队，队列满时丢弃并计数（绝不阻塞响应）。"""
        self._ensure_worker()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._bump("dropped")
            if self.stats.dropped % 100 == 1:
                logger.warning(
                    "点击日志队列已满（maxsize=%d），丢弃事件；累计丢弃 %d 条",
                    self._maxsize,
                    self.stats.dropped,
                )
            return False
        self._bump("accepted")
        return True

    def flush_now(self, events: Optional[Iterable[ClickEvent]] = None) -> int:
        """同步落库：``events`` 为空时把队列里积压的事件全部排空后写入。"""
        batch = list(events) if events is not None else self._drain_queue()
        if not batch:
            return 0
        return self._flush(batch)

    def stop(self, timeout: float = 5.0) -> None:
        """通知后台线程退出，并等待队列排空（进程退出 / 测试收尾时调用）。"""
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._flush(self._drain_queue())
            return
        self._stopping.set()
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:  # pragma: no cover - 极端拥塞
            logger.warning("停止信号入队失败，队列仍满")
        thread.join(timeout=timeout)
        if thread.is_alive():  # pragma: no cover - 极端拥塞
            logger.warning("点击日志后台线程未在 %.1f 秒内退出", timeout)

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._thread_lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopping.clear()
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True
                )
                self._thread.start()
                logger.info("点击日志后台线程已启动：%s", self._name)

    def _run(self) -> None:  # pragma: no cover - 由集成冒烟测试覆盖
        batch = []
        last_flush = time.monotonic()
        running = True
        try:
            while running:
                wait = max(0.02, self._flush_interval - (time.monotonic() - last_flush))
                try:
                    item = self._queue.get(timeout=wait)
                    self._queue.task_done()
                except queue.Empty:
                    item = None

                if item is _STOP:
                    running = False
                elif item is not None:
                    batch.append(item)

                # 一次唤醒尽量多取一些，凑够一个批次
                while running and len(batch) < self._batch_size:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    self._queue.task_done()
                    if item is _STOP:
                        running = False
                        break
                    batch.append(item)

                if batch and (
                    not running
                    or len(batch) >= self._batch_size
                    or time.monotonic() - last_flush >= self._flush_interval
                ):
                    close_old_connections()
                    self._flush(batch)
                    batch.clear()
                    last_flush = time.monotonic()

            # 退出前把队列里剩下的全部写完
            for event in self._drain_queue():
                batch.append(event)
                if len(batch) >= self._batch_size:
                    self._flush(batch)
                    batch.clear()
        finally:
            if batch:
                self._flush(batch)
            close_old_connections()

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------
    def _flush(self, events: Iterable[ClickEvent]) -> int:
        """把一批事件写入数据库：1 条 bulk_insert + 1 条批量 UPDATE。"""
        events = list(events)
        if not events:
            return 0
        counts = Counter(event.link_id for event in events)
        try:
            with transaction.atomic():
                ClickLog.objects.bulk_create(
                    [
                        ClickLog(
                            link_id=event.link_id,
                            ip_address=event.ip_address or None,
                            user_agent=event.user_agent,
                        )
                        for event in events
                    ],
                    batch_size=self._batch_size,
                    ignore_conflicts=True,
                )
                ShortLink.objects.filter(pk__in=counts.keys()).update(
                    click_count=F("click_count")
                    + Case(
                        *[
                            When(pk=link_id, then=Value(count))
                            for link_id, count in counts.items()
                        ],
                        output_field=IntegerField(),
                    )
                )
        except Exception:
            self._bump("failed_batches")
            logger.exception("点击日志批量写入失败，丢弃 %d 条事件", len(events))
            return 0
        self._bump("flushed", len(events))
        logger.debug("点击日志落库 %d 条，涉及 %d 个短链", len(events), len(counts))
        return len(events)

    def _drain_queue(self) -> list:
        drained = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
            if item is not _STOP:
                drained.append(item)
        return drained

    def _bump(self, field_name: str, amount: int = 1) -> None:
        with self._stats_lock:
            setattr(self.stats, field_name, getattr(self.stats, field_name) + amount)


# 全局缓冲区实例
click_log_buffer = ClickLogBuffer()


def record_click(link_id: int, ip_address: str = "", user_agent: str = "") -> None:
    """记录一次点击。

    默认走后台队列立即返回；``settings.CLICK_LOG_ASYNC = False`` 时同步写入，
    便于单元测试断言。任何异常都不应该影响跳转，因此这里兜底捕获。
    """
    if not link_id:
        return
    event = ClickEvent(
        link_id=int(link_id),
        ip_address=ip_address or None,
        user_agent=truncate_user_agent(user_agent),
    )
    try:
        if getattr(settings, "CLICK_LOG_ASYNC", True):
            click_log_buffer.submit(event)
        else:
            click_log_buffer.flush_now([event])
    except Exception:  # pragma: no cover - 统计失败不能影响跳转
        logger.exception("记录点击失败（link_id=%s）", link_id)


@atexit.register
def _shutdown_click_log_buffer() -> None:
    click_log_buffer.stop(timeout=3.0)
