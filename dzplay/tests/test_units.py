from __future__ import annotations

import os

import pytest

from app import clock
from app.config import Settings
from app.errors import AppError
from app.services.content import clean_message
from app.services.rate_limit import Limit, MemoryRateLimiter, RedisRateLimiter


def _limiters():
    yield MemoryRateLimiter()
    url = os.environ.get("DZ_TEST_REDIS_URL")
    if url:
        r = RedisRateLimiter(url, prefix="dz:test:")
        r.reset()
        yield r


@pytest.mark.parametrize("limiter", list(_limiters()), ids=lambda l: type(l).__name__)
def test_rate_limiter_sliding_window(limiter):
    clock.reset()
    limits = [Limit("k1", 2, 60), Limit("k2", 3, 3600)]
    assert limiter.check_and_hit(limits).allowed
    assert limiter.check_and_hit(limits).allowed
    d = limiter.check_and_hit(limits)
    assert not d.allowed and d.key == "k1" and 0 < d.retry_after <= 60
    clock.advance(61)
    assert limiter.check_and_hit(limits).allowed  # k2 now at 3
    d = limiter.check_and_hit(limits)
    assert not d.allowed and d.key == "k2"
    clock.reset()


def test_clean_message_allows_normal_arabic_and_emoji():
    text = "أشعر بالوحدة اليوم 😔‍ وأريد أن أتكلم. هل أنت بخير؟"
    assert clean_message(text, 1000, "reject") == text


def test_clean_message_rejects_non_string():
    with pytest.raises(AppError):
        clean_message(123, 1000, "reject")


def test_links_allowed_when_configured():
    assert clean_message("https://example.com", 1000, "allow_plain") == "https://example.com"


def test_settings_validation():
    with pytest.raises(ValueError):
        Settings(ENV="production", SECRET_KEY="")
    with pytest.raises(ValueError):
        Settings(LOGIN_BLOCK_SCOPE="everything")
    s = Settings(ENV="production", SECRET_KEY="x" * 40)
    assert s.COOKIE_SECURE is True
