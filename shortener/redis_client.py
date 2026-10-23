"""Redis 客户端与短链缓存层。

设计要点
--------
1. **懒加载单例**：进程启动时不连接 Redis，第一次真正访问时才创建连接池，
   避免 Redis 未启动导致 Django 无法启动（例如只跑 migrate 的时候）。
2. **缓存可选**：Redis 只是加速层，真值永远在数据库里。任何 ``RedisError``
   都会被捕获并降级为"缓存未命中"，保证短链跳转不中断。
3. **连接池**：``redis.Redis`` 自带 ConnectionPool 且线程安全，可以直接跨线程
   （例如后台写日志线程）复用，不需要自己维护池。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import redis
from django.conf import settings
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# 缓存的 key 前缀，方便在 Redis 里按命名空间排查 / 批量清理
KEY_PREFIX = "shortlink:code:"

_client: Optional[redis.Redis] = None
_client_lock = threading.Lock()


def get_redis_client() -> redis.Redis:
    """返回进程内共享的 redis-py 客户端（懒加载 + 线程安全）。"""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                password=settings.REDIS_PASSWORD,
                socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                health_check_interval=30,
                decode_responses=True,
                client_name="shortlink-django",
            )
            logger.info(
                "初始化 Redis 客户端 %s:%s/%s",
                settings.REDIS_HOST,
                settings.REDIS_PORT,
                settings.REDIS_DB,
            )
    return _client


def set_redis_client(client: Optional[redis.Redis]) -> None:
    """注入 / 重置客户端，方便单元测试替换为 FakeRedis。"""
    global _client
    with _client_lock:
        _client = client


def close_redis_client() -> None:
    """关闭连接池（测试或进程退出时调用）。"""
    global _client
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # pragma: no cover - 关闭失败不影响主流程
                logger.debug("关闭 Redis 客户端时出错", exc_info=True)
            _client = None


def ping_redis() -> bool:
    """探测 Redis 是否可用，供健康检查 / 管理命令使用。"""
    try:
        return bool(get_redis_client().ping())
    except RedisError as exc:
        logger.warning("Redis 不可用：%s", exc)
        return False


@dataclass(frozen=True)
class CachedLink:
    """缓存里保存的短链快照。

    同时缓存 link_id 是有意为之：跳转时即使命中缓存也能拿到主键，
    这样异步写点击日志时不需要再回查数据库。
    """

    link_id: int
    original_url: str


class ShortLinkCache:
    """短码 -> 原始链接 的读写封装。

    :param client_factory: 返回 redis 客户端的可调用对象，测试时可替换
    :param ttl: 过期时间（秒），默认取 settings.SHORTLINK_CACHE_TTL
    """

    # Redis 故障时日志的抑制间隔，避免高并发下刷爆日志
    _LOG_INTERVAL_SECONDS = 30.0

    def __init__(self, client_factory=get_redis_client, ttl: Optional[int] = None):
        self._client_factory = client_factory
        self._ttl = ttl
        self._last_error_log_at = 0.0

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    @staticmethod
    def build_key(short_code: str) -> str:
        return f"{KEY_PREFIX}{short_code}"

    @property
    def ttl(self) -> int:
        return settings.SHORTLINK_CACHE_TTL if self._ttl is None else self._ttl

    def _client(self) -> redis.Redis:
        return self._client_factory()

    def _log_failure(self, action: str, short_code: str, exc: Exception) -> None:
        """节流日志：Redis 挂了的时候不要每个请求都打一条 traceback。"""
        now = time.monotonic()
        if now - self._last_error_log_at >= self._LOG_INTERVAL_SECONDS:
            self._last_error_log_at = now
            logger.warning(
                "Redis %s 失败（short_code=%s），本次降级为回源数据库：%s",
                action,
                short_code,
                exc,
            )

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def get(self, short_code: str) -> Optional[CachedLink]:
        """读取缓存，未命中 / Redis 异常 / 数据损坏都返回 None。"""
        if not short_code:
            return None
        try:
            raw = self._client().get(self.build_key(short_code))
        except RedisError as exc:
            self._log_failure("GET", short_code, exc)
            return None
        if raw is None:
            return None
        return self._decode(raw, short_code)

    def set(self, short_code: str, link_id: int, original_url: str) -> bool:
        """写入缓存（写穿模式），返回是否成功。"""
        if not short_code:
            return False
        payload = json.dumps(
            {"id": link_id, "url": original_url},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        try:
            self._client().setex(self.build_key(short_code), self.ttl, payload)
            return True
        except RedisError as exc:
            self._log_failure("SETEX", short_code, exc)
            return False

    def delete(self, short_code: str) -> bool:
        try:
            self._client().delete(self.build_key(short_code))
            return True
        except RedisError as exc:
            self._log_failure("DEL", short_code, exc)
            return False

    def get_many(self, short_codes) -> dict:
        """批量读取（阶段二的预热 / 离线任务会用到），返回 {short_code: CachedLink}。"""
        codes = [code for code in short_codes if code]
        if not codes:
            return {}
        try:
            keys = [self.build_key(code) for code in codes]
            values = self._client().mget(keys)
        except RedisError as exc:
            self._log_failure("MGET", ",".join(codes[:3]), exc)
            return {}

        result = {}
        for code, raw in zip(codes, values):
            if raw is None:
                continue
            cached = self._decode(raw, code)
            if cached is not None:
                result[code] = cached
        return result

    # ------------------------------------------------------------------
    # 内部：反序列化
    # ------------------------------------------------------------------
    @staticmethod
    def _decode(raw, short_code: str) -> Optional[CachedLink]:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("缓存值不是合法 UTF-8（short_code=%s），忽略", short_code)
                return None
        if not isinstance(raw, str) or not raw:
            return None

        if raw.startswith("{"):
            try:
                data = json.loads(raw)
                return CachedLink(link_id=int(data["id"]), original_url=str(data["url"]))
            except (ValueError, KeyError, TypeError):
                logger.warning("缓存 JSON 解析失败（short_code=%s），忽略", short_code)
                return None

        # 兼容早期只存 URL 字符串的格式：没有 link_id，跳转仍可完成
        return CachedLink(link_id=0, original_url=raw)


# 全局缓存实例：视图里直接 import 使用
shortlink_cache = ShortLinkCache()

