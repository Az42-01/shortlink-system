"""短链接系统的视图。

两个核心视图：
    create_link   : POST -> 入库 + Base62 短码 -> 写穿 Redis -> 结果页
    redirect_link : 短码 -> Redis 命中 / 回源 DB 并回填 -> 301 + 异步点击日志
"""

import logging

from django.conf import settings
from django.db import DatabaseError, connection
from django.http import HttpResponsePermanentRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from .analytics import build_stats_payload
from .forms import ShortLinkForm
from .models import ShortLink
from .redis_client import ping_redis, shortlink_cache
from .tasks import record_click
from .utils import (
    ShortCodeOverflow,
    device_label,
    get_client_ip,
    is_valid_short_code,
    parse_device_type,
)

logger = logging.getLogger(__name__)


def _short_url(request, short_code: str) -> str:
    """拼出对外的完整短链接。

    配置了 ``SHORTLINK_BASE_URL`` 时优先使用（线上域名 / CDN 场景），
    否则退化为当前请求的 Host，方便本地调试。
    """
    path = reverse("shortener:redirect", args=[short_code])
    if settings.SHORTLINK_BASE_URL:
        return f"{settings.SHORTLINK_BASE_URL}{path}"
    return request.build_absolute_uri(path)


@require_http_methods(["GET", "POST"])
def create_link(request):
    """创建短链接：GET 展示表单，POST 保存并展示生成结果。"""
    if request.method == "POST":
        form = ShortLinkForm(request.POST)
        if form.is_valid():
            try:
                link = ShortLink.create_with_short_code(form.cleaned_data["original_url"])
            except ShortCodeOverflow:
                logger.exception("短码空间已耗尽，无法创建新链接")
                form.add_error("original_url", "系统短码空间已用尽，请联系管理员扩容")
            else:
                # 写穿缓存：创建完立刻进 Redis，第一次点击就能命中
                shortlink_cache.set(link.short_code, link.pk, link.original_url)
                logger.info("创建短链 %s -> %s", link.short_code, link.original_url)
                return render(
                    request,
                    "shortener/result.html",
                    {
                        "link": link,
                        "short_url": _short_url(request, link.short_code),
                        "stats_url": request.build_absolute_uri(
                            reverse("shortener:link_stats", args=[link.short_code])
                        ),
                    },
                )
    else:
        form = ShortLinkForm()

    return render(
        request,
        "shortener/index.html",
        {
            "form": form,
            "recent_links": ShortLink.objects.all()[:5],
        },
    )


@require_GET
def redirect_link(request, short_code: str):
    """短码跳转：缓存优先，回源后写回缓存，最后 301 跳转。

    点击日志通过后台队列异步落库（见 ``shortener.tasks``），
    跳转路径上只有一次 Redis GET（命中）或 一次 DB 查询 + 一次 Redis SETEX（未命中）。
    """
    if not is_valid_short_code(short_code):
        return _render_not_found(request, short_code)

    cached = shortlink_cache.get(short_code)
    cache_hit = cached is not None

    if cache_hit:
        target_url, link_id = cached.original_url, cached.link_id
    else:
        link = (
            ShortLink.objects.filter(short_code=short_code, is_active=True)
            .only("id", "original_url")
            .first()
        )
        if link is None:
            logger.info("短码 %s 在数据库中也未找到", short_code)
            return _render_not_found(request, short_code)
        target_url, link_id = link.original_url, link.pk
        # 回填缓存（cache-aside / read-through 的写回一步）
        shortlink_cache.set(short_code, link_id, target_url)

    if not link_id:
        # 兼容"缓存里只有 URL 字符串"的旧格式，补一次主键
        link_id = (
            ShortLink.objects.filter(short_code=short_code)
            .values_list("id", flat=True)
            .first()
        )
        if link_id:
            shortlink_cache.set(short_code, link_id, target_url)

    # 关键：入队即返回，不等待数据库写入
    record_click(
        link_id=link_id,
        ip_address=get_client_ip(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
    )

    response = HttpResponsePermanentRedirect(target_url)  # 301 Moved Permanently
    # 301 会被浏览器长期缓存，导致后续点击不再回源、统计失真；
    # 显式 no-store，保证每次访问都经过服务端（生产可换成短 max-age）。
    response["Cache-Control"] = "no-store"
    # 便于压测 / 排障时确认缓存是否生效
    response["X-Shortlink-Cache"] = "HIT" if cache_hit else "MISS"
    return response


@require_GET
def link_stats(request, short_code: str):
    """数据看板：KPI 卡片 + 7 天点击趋势 + 设备分布 + 最近 20 条访问明细。

    页面本身只渲染骨架和 KPI，图表数据由前端请求 ``link_stats_api`` 拿到后
    交给 ECharts 绘制，这样看板可以独立刷新而不重载整页。
    """
    link = get_object_or_404(ShortLink, short_code=short_code)
    payload = build_stats_payload(link)
    devices = payload["devices"]
    device_total = devices["total"]

    recent_clicks = list(link.clicks.all()[:20])
    recent_rows = []
    for click in recent_clicks:
        device_key = parse_device_type(click.user_agent)
        recent_rows.append(
            {"click": click, "device_key": device_key, "device_label": device_label(device_key)}
        )

    return render(
        request,
        "shortener/stats.html",
        {
            "link": link,
            "short_url": _short_url(request, link.short_code),
            "recent_clicks": recent_clicks,
            "recent_rows": recent_rows,
            "unique_visitors": payload["summary"]["unique_visitors"],
            "trend_days": payload["trend"]["days"],
            "trend_total": payload["trend"]["total"],
            "top_device_label": (
                devices["top_label"] if devices["top"] else "暂无数据"
            ),
            "top_device_share": (
                round(max(devices["values"]) * 100 / device_total) if device_total else 0
            ),
            "device_total": device_total,
            # 图表数据接口，交给前端 fetch
            "api_url": reverse("shortener:link_stats_api", args=[link.short_code]),
            "updated_at": timezone.localtime(timezone.now()),
        },
    )


@require_GET
def link_stats_api(request, short_code: str):
    """看板数据接口（JSON）。

    返回结构见 ``shortener.analytics.build_stats_payload``：
    ``trend`` 是最近 7 天每日点击量（折线图），``devices`` 是设备分布（饼图）。
    """
    link = ShortLink.objects.filter(short_code=short_code).first()
    if link is None:
        return JsonResponse(
            {"detail": f"短链接 {short_code} 不存在"},
            status=404,
            content_type="application/json; charset=utf-8",
            json_dumps_params={"ensure_ascii": False},
        )

    response = JsonResponse(
        build_stats_payload(link),
        content_type="application/json; charset=utf-8",
        json_dumps_params={"ensure_ascii": False},
    )
    # 看板数据实时变化，禁止中间层缓存
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def health_check(request):
    """健康检查：数据库必须可用，Redis 只是加速层（不可用时降级但不致命）。"""
    db_ok = True
    db_error = ""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError as exc:
        db_ok = False
        db_error = str(exc)

    redis_ok = ping_redis()
    payload = {
        "status": "ok" if db_ok else "unhealthy",
        "database": db_ok,
        "redis": redis_ok,
        "cache_degraded": not redis_ok,
    }
    if db_error:
        payload["database_error"] = db_error
    return JsonResponse(payload, status=200 if db_ok else 503)


def _render_not_found(request, short_code: str):
    return render(
        request,
        "shortener/link_not_found.html",
        {"short_code": short_code},
        status=404,
    )
