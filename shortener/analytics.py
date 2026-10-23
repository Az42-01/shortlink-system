"""数据看板的聚合层。

看板需要两类数据，都在这里一次性算好，视图只负责序列化：

    1. 趋势：最近 N 天（默认 7）每天的点击量，缺失日期补 0，按本地时区分组
    2. 设备：按 User-Agent 解析出的 PC / 移动端 / 其他 占比

把聚合逻辑从视图里拆出来，好处是可以单独写测试，也方便后续换缓存 / 预计算。
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from .utils import (
    DEVICE_OTHER,
    DEVICE_TYPES,
    DEVICE_TYPE_LABELS,
    device_label,
    parse_device_type,
)

# 看板默认展示最近 7 天
DEFAULT_TREND_DAYS = 7


def _iso_datetime(value: datetime | None) -> str | None:
    """统一输出带时区的 ISO 8601 字符串（本地时区）。"""
    if value is None:
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat(timespec="seconds")


def daily_click_counts(link, days: int = DEFAULT_TREND_DAYS, today: date | None = None):
    """统计某条短链最近 ``days`` 天的每日点击量。

    :returns: ``[(date, count), ...]``，长度固定为 ``days``，无点击的日期补 0
    :raises ValueError: ``days`` 小于 1

    分组用 ``TruncDate``，Django 会按当前时区（settings.TIME_ZONE）把 UTC 时间
    转换成日期，因此跨时区的点击会落在正确的「本地自然日」上。
    """
    if days < 1:
        raise ValueError("days 必须 >= 1")

    today = today or timezone.localdate()
    start_date = today - timedelta(days=days - 1)
    tz = timezone.get_current_timezone()
    start = datetime.combine(start_date, time.min, tzinfo=tz)

    rows = (
        link.clicks.filter(created_at__gte=start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Count("id"))
        .order_by("day")
    )
    counts = {row["day"]: row["total"] for row in rows}

    return [
        (start_date + timedelta(days=offset), counts.get(start_date + timedelta(days=offset), 0))
        for offset in range(days)
    ]


def device_distribution(link):
    """按设备类型汇总点击量。

    先按 ``user_agent`` 分组（去重后通常只有几十种），再在 Python 里归类，
    避免数据库层做正则匹配，SQLite / MySQL 都通用。

    :returns: ``[{"key": "pc", "label": "PC", "value": 12}, ...]``，三种类型始终都在
    """
    counted = Counter()
    rows = link.clicks.values("user_agent").annotate(total=Count("id"))
    for row in rows:
        counted[parse_device_type(row["user_agent"])] += row["total"]

    return [
        {
            "key": device_type,
            "label": DEVICE_TYPE_LABELS[device_type],
            "value": counted.get(device_type, 0),
        }
        for device_type in DEVICE_TYPES
    ]


def unique_visitor_count(link) -> int:
    """独立 IP 数（IP 为空的记录不计入）。"""
    return (
        link.clicks.exclude(ip_address__isnull=True)
        .values("ip_address")
        .distinct()
        .count()
    )


def build_stats_payload(link, days: int = DEFAULT_TREND_DAYS, today: date | None = None) -> dict:
    """组装看板 / API 使用的完整 JSON 结构。"""
    today = today or timezone.localdate()
    series = daily_click_counts(link, days=days, today=today)
    devices = device_distribution(link)

    trend_total = sum(count for _, count in series)
    device_total = sum(item["value"] for item in devices)
    top_device = None
    if device_total:
        top = max(devices, key=lambda item: item["value"])
        if top["value"] > 0:
            top_device = top["key"]

    return {
        "short_code": link.short_code,
        "original_url": link.original_url,
        "generated_at": _iso_datetime(timezone.now()),
        "summary": {
            "click_count": link.click_count,
            "unique_visitors": unique_visitor_count(link),
            "created_at": _iso_datetime(link.created_at),
        },
        "trend": {
            "days": days,
            # 日期字符串 "YYYY-MM-DD"，前端负责截成 "MM/DD"
            "labels": [day.isoformat() for day, _ in series],
            "values": [count for _, count in series],
            "total": trend_total,
        },
        "devices": {
            "keys": [item["key"] for item in devices],
            "labels": [item["label"] for item in devices],
            "values": [item["value"] for item in devices],
            "total": device_total,
            "top": top_device,
            "top_label": device_label(top_device) if top_device else DEVICE_TYPE_LABELS[DEVICE_OTHER],
        },
    }
