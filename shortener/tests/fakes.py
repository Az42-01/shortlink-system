"""测试用的 Redis 替身。

只实现 ``ShortLinkCache`` 实际用到的方法，避免测试依赖真实 Redis 进程。
"""

from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedisClient:
    """内存版 redis 客户端，行为与缓存层需要的方法保持一致。"""

    def __init__(self):
        self.store: dict = {}
        self.ttls: dict = {}
        self.setex_calls = []
        self.get_calls = []

    # --- 缓存层使用的方法 -------------------------------------------------
    def get(self, key):
        self.get_calls.append(key)
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value
        self.ttls[key] = ttl
        return True

    def delete(self, key):
        self.ttls.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    def mget(self, keys):
        return [self.store.get(key) for key in keys]

    def ping(self):
        return True

    # --- 管理命令使用的方法 -----------------------------------------------
    def ttl(self, key):
        return self.ttls.get(key, -1)

    def info(self, section=None):
        return {"redis_version": "fake-7.0"}

    def scan_iter(self, match=None, count=None):
        prefix = (match or "*").rstrip("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key


class BrokenRedisClient:
    """模拟 Redis 宕机：所有操作都抛连接错误。"""

    def get(self, key):
        raise RedisConnectionError("redis is down")

    def setex(self, key, ttl, value):
        raise RedisConnectionError("redis is down")

    def delete(self, key):
        raise RedisConnectionError("redis is down")

    def mget(self, keys):
        raise RedisConnectionError("redis is down")

    def ping(self):
        raise RedisConnectionError("redis is down")
