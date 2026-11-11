"""Redis-backed document vectors and shared admission for live model runs."""

import json
import math
import os
from contextlib import contextmanager

REDIS_URL = "PROJECTPILOT_REDIS_URL"
VECTOR_TTL_SECONDS = 86400
LIVE_LIMIT_KEY = "projectpilot:live-admission:v1"

# The first admitted request opens the window. Rejections leave the counter and TTL alone.
TAKE_SLOT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
local remaining = redis.call('PTTL', KEYS[1])
if remaining < 0 then
    redis.call('SET', KEYS[1], 1, 'PX', ARGV[2])
elseif count >= tonumber(ARGV[1]) then
    return math.max(1, remaining)
else
    redis.call('INCR', KEYS[1])
end
return 0
"""


class RedisUnavailable(RuntimeError):
    pass


def _client(url):
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return Redis.from_url(
        url, socket_connect_timeout=0.5, socket_timeout=0.5, retry=Retry(NoBackoff(), 0),
    )


@contextmanager
def _cache_connection(url):
    from redis.exceptions import RedisError

    try:
        with _client(url) as client:
            yield client
    except (RedisError, ValueError):
        raise RedisUnavailable("Redis vector cache is unavailable") from None


class RedisVectorCache:
    def __init__(self, url):
        self.url = url

    def read(self, keys):
        with _cache_connection(self.url) as client:
            return client.mget(keys)

    def write(self, items):
        with _cache_connection(self.url) as client, client.pipeline(transaction=False) as pipeline:
            for key, vector in items:
                pipeline.set(key, json.dumps(vector, allow_nan=False), ex=VECTOR_TTL_SECONDS)
            pipeline.execute()


class LiveRateLimiter:
    def __init__(self, url):
        self.limit = int(os.getenv("PROJECTPILOT_LIVE_RATE_LIMIT", "5"))
        self.window = int(os.getenv("PROJECTPILOT_LIVE_RATE_WINDOW_SECONDS", "60"))
        if self.limit < 1 or self.window < 1:
            raise ValueError("The live rate limit and window must be positive integers.")
        self.client = _client(url)

    def retry_after(self):
        from redis.exceptions import RedisError

        try:
            remaining_ms = self.client.eval(TAKE_SLOT, 1, LIVE_LIMIT_KEY, self.limit, self.window * 1000)
        except RedisError:
            raise RedisUnavailable("Redis live rate limiter is unavailable") from None
        return math.ceil(remaining_ms / 1000)

    def close(self):
        self.client.close()
