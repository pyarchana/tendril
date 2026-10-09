from app.ratelimit import RateLimiter


def test_allows_up_to_the_limit_then_says_how_long_to_wait():
    limiter = RateLimiter(window_seconds=3600)
    assert limiter.check("garden", 2, now=0) is None
    assert limiter.check("garden", 2, now=10) is None
    assert limiter.check("garden", 2, now=20) == 3580


def test_hits_expire_after_the_window():
    limiter = RateLimiter(window_seconds=3600)
    limiter.check("garden", 1, now=0)
    assert limiter.check("garden", 1, now=3599) is not None
    assert limiter.check("garden", 1, now=3600) is None


def test_each_key_has_its_own_budget():
    limiter = RateLimiter()
    assert limiter.check(1, 1, now=0) is None
    assert limiter.check(2, 1, now=0) is None
    assert limiter.check(1, 1, now=1) is not None
