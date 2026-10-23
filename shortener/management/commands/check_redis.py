"""``python manage.py check_redis``：检查 Redis 连接与短链缓存状态。"""

from django.conf import settings
from django.core.management.base import BaseCommand
from redis.exceptions import RedisError

from shortener.redis_client import KEY_PREFIX, get_redis_client


class Command(BaseCommand):
    help = "检查 Redis 连接是否可用，并统计 shortlink:code:* 缓存条目"

    def add_arguments(self, parser):
        parser.add_argument(
            "--sample",
            type=int,
            default=5,
            help="最多打印几个已缓存的 key（默认 5）",
        )

    def handle(self, *args, **options):
        target = f"{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}"
        try:
            client = get_redis_client()
            client.ping()
        except RedisError as exc:
            self.stdout.write(self.style.ERROR(f"[FAIL] 无法连接 Redis {target}：{exc}"))
            self.stdout.write(
                "       应用仍可启动并跳转（缓存自动降级为回源数据库），但性能会下降。"
            )
            return

        version = "unknown"
        try:
            version = client.info("server").get("redis_version", "unknown")
        except RedisError:
            pass

        self.stdout.write(
            self.style.SUCCESS(f"[OK] Redis 可用：{target}（redis {version}）")
        )

        cached_count = 0
        samples = []
        try:
            for key in client.scan_iter(match=f"{KEY_PREFIX}*", count=500):
                cached_count += 1
                if len(samples) < options["sample"]:
                    samples.append(key)
        except RedisError as exc:
            self.stdout.write(self.style.WARNING(f"[WARN] 扫描缓存 key 失败：{exc}"))
            return

        self.stdout.write(f"     缓存条目：{cached_count} 个（前缀 {KEY_PREFIX}）")
        for key in samples:
            try:
                ttl = client.ttl(key)
            except RedisError:  # pragma: no cover
                ttl = -1
            self.stdout.write(f"       - {key}  ttl={ttl}s")
