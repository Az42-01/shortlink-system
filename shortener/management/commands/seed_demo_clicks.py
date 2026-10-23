"""``python manage.py seed_demo_clicks``：写入演示点击日志，用来看数据看板效果。

默认插入 50 条日志，分布在最近 7 天，覆盖 PC / 移动端 / 其他 三种设备，
并把短链的累计点击同步为日志总数。库里没有短链时会先创建一条演示短链。

示例::

    python manage.py seed_demo_clicks
    python manage.py seed_demo_clicks --clicks 200 --days 14
    python manage.py seed_demo_clicks --code 0001 --clear

这是一个纯演示用的临时脚本，正式环境请勿执行。
"""

import random
from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from shortener.analytics import daily_click_counts, device_distribution
from shortener.models import ClickLog, ShortLink

# 每个设备类型准备几条真实感较强的 UA
_PC_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

_MOBILE_USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.47",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S9180) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
]

_OTHER_USER_AGENTS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "curl/8.5.0",
    "PostmanRuntime/7.37.0",
]

# 设备占比：移动端为主，符合短链场景
_DEVICE_WEIGHTS = [35, 55, 10]

_IP_PREFIXES = ("203.0.113.", "198.51.100.", "192.0.2.")


class Command(BaseCommand):
    help = "写入演示用点击日志（默认 50 条，分布在最近 7 天，覆盖 PC / 移动端 / 其他）"

    def add_arguments(self, parser):
        parser.add_argument("--clicks", type=int, default=50, help="写入的日志条数（默认 50）")
        parser.add_argument("--days", type=int, default=7, help="时间跨度天数（默认 7）")
        parser.add_argument("--code", default="", help="目标短码，留空则取最近一条短链")
        parser.add_argument("--clear", action="store_true", help="写入前清空该短链已有的点击日志")
        parser.add_argument("--seed", type=int, default=20261023, help="随机种子，保证演示数据可复现")

    def handle(self, *args, **options):
        clicks = options["clicks"]
        days = options["days"]
        if clicks < 1:
            raise CommandError("--clicks 必须 >= 1")
        if days < 1:
            raise CommandError("--days 必须 >= 1")

        link = self._resolve_link(options["code"])
        if options["clear"]:
            deleted, _ = link.clicks.all().delete()
            self.stdout.write(f"已清空 {link.short_code} 的历史日志 {deleted} 条")

        rng = random.Random(options["seed"])
        now = timezone.localtime()
        timestamps = sorted(self._build_timestamps(rng, now, clicks, days))
        user_agents = [self._pick_user_agent(rng) for _ in range(clicks)]
        ip_addresses = [self._pick_ip(rng) for _ in range(clicks)]

        self._write_logs(link, timestamps, user_agents, ip_addresses)
        self._print_summary(link, clicks, days)

    # ------------------------------------------------------------------
    # 数据生成
    # ------------------------------------------------------------------
    def _build_timestamps(self, rng, now, clicks, days):
        """生成 ``clicks`` 个时间点，越靠近今天权重越高（趋势线更好看）。"""
        weights = [index + 1 for index in range(days)]
        timestamps = []
        for _ in range(clicks):
            day_index = rng.choices(range(days), weights=weights, k=1)[0]
            day = now.date() - timedelta(days=days - 1 - day_index)
            moment = datetime.combine(
                day,
                time(rng.randint(8, 23), rng.randint(0, 59), rng.randint(0, 59)),
                tzinfo=timezone.get_current_timezone(),
            )
            # 今天的随机时刻可能落在「未来」，往前推一点，避免出现未来时间
            if moment > now:
                moment = now - timedelta(minutes=rng.randint(1, 180))
            timestamps.append(moment)
        return timestamps

    def _pick_user_agent(self, rng):
        bucket = rng.choices(
            (_PC_USER_AGENTS, _MOBILE_USER_AGENTS, _OTHER_USER_AGENTS),
            weights=_DEVICE_WEIGHTS,
            k=1,
        )[0]
        return rng.choice(bucket)

    def _pick_ip(self, rng):
        return f"{rng.choice(_IP_PREFIXES)}{rng.randint(1, 254)}"

    def _resolve_link(self, code):
        if code:
            link = ShortLink.objects.filter(short_code=code).first()
            if link is None:
                raise CommandError(f"短码 {code} 不存在")
            return link

        link = ShortLink.objects.first()  # Meta.ordering = -created_at，取最新一条
        if link is None:
            link = ShortLink.create_with_short_code("https://www.djangoproject.com/")
            self.stdout.write(
                self.style.WARNING(
                    f"库中还没有短链，已创建演示短链 {link.short_code} -> {link.original_url}"
                )
            )
        return link

    # ------------------------------------------------------------------
    # 落库与输出
    # ------------------------------------------------------------------
    def _write_logs(self, link, timestamps, user_agents, ip_addresses):
        with transaction.atomic():
            for moment, user_agent, ip_address in zip(timestamps, user_agents, ip_addresses):
                log = ClickLog.objects.create(
                    link=link, ip_address=ip_address, user_agent=user_agent
                )
                # created_at 是 auto_now_add，只能建完再回写历史时间
                ClickLog.objects.filter(pk=log.pk).update(created_at=moment)

            # 让累计点击和日志总数保持一致，看板 KPI 才不会互相打架
            link.click_count = link.clicks.count()
            link.save(update_fields=["click_count"])

    def _print_summary(self, link, clicks, days):
        self.stdout.write(
            self.style.SUCCESS(
                f"[OK] 已写入 {clicks} 条点击日志 -> {link.short_code}（累计 {link.click_count} 次）"
            )
        )

        series = daily_click_counts(link, days=days)
        peak = max((count for _, count in series), default=0)
        self.stdout.write("每日点击：")
        for day, total in series:
            bar = "#" * (round(total * 30 / peak) if peak else 0)
            self.stdout.write(f"  {day.isoformat()}  {bar:<30} {total}")

        self.stdout.write("设备分布：")
        for item in device_distribution(link):
            self.stdout.write(f"  {item['label']:<4} {item['value']:>4}")

        stats_path = f"/stats/{link.short_code}/"
        self.stdout.write(self.style.SUCCESS(f"看板地址：{stats_path}"))
        self.stdout.write(f"数据接口：{stats_path}api/")

