"""Base class used to provide ratelimiting to github3.py."""
import enum
import typing as t

import rush
import rush.limit_data
import rush.quota
import rush.result
import rush.throttle
import rush.limiters.periodic
import rush.stores.dictionary

if t.TYPE_CHECKING:
    from .. import users


class RateLimits(enum.IntEnum):
    """Object documenting the ratelimits in GitHub."""

    #: UNAUTHENTICATED is the number, per hour, of unauthenticated requests
    #: that are allowed
    UNAUTHENTICATED = 60
    #: AUTHENTICATED is the number, per hour, of authenticated requests that
    #: are allowed
    AUTHENTICATED = 5000
    #: ACTIONS_TOKEN is the number, per hour, of requests that the default
    #: ``GITHUB_TOKEN`` is allowd per repository per hour
    ACTIONS_TOKEN = 1000
    #: ENTERPRISE_CLOUD is the number, per hour, of authenticated requests that
    #: are allowd per repository per hour
    ENTERPRISE_CLOUD = 15000


class RateLimitEstimate:
    """Wrapper around rush's RateLimitResult."""

    def __init__(self, result: rush.result.RateLimitResult) -> None:
        """Not intended to be created by users."""
        self.result = result

    @property
    def limit(self) -> int:
        """Return the original rate-limit."""
        return self.result.limit

    original_limit = limit

    @property
    def limited(self) -> bool:
        """Return whether the current user is rate-limited."""
        return self.result.limited

    @property
    def remaining(self) -> int:
        """Return the number of remaining requests a user has."""
        return self.result.remaining

    @property
    def reset_after(self) -> datetime.timedelta:
        """Return the timedelta for when the limit resets."""
        return self.result.reset_after

    @property
    def retry_after(self) -> datetime.timedelta:
        """Return the timedelta for when to next retry."""
        return self.result.retry_after

    @property
    def resets_at(self) -> datetime.datetime:
        """Return the datetime of the next reset."""
        return self.result.resets_at()

    @property
    def retry_at(self) -> datetime.datetime:
        """Return the datetime of when to retry."""
        return self.result.retry_at()


Lr = t.TypeVar("Lr", bound="Limiter")


class Limiter:
    """Basic rate-limiting for the GitHub API."""

    @classmethod
    def authenticated(
        cls: t.Type[Lr],
        *,
        username: users.UserLike,
        throttle: t.Optional[rush.throttle.Throttle] = None,
    ) -> Lr:
        """Create an authenticated limiter.

        :param username:
            The name of the user to be used to track the rate limit.
        :param throttle:
            (Optional) The rush throttle to use as the backing limitation
        :returns:
            Limiter (or subclass) instance
        """
        return cls(
            requests_per_hour=RateLimits.AUTHENTICATED,
            throttle=throttle,
        )

    @classmethod
    def unauthenticated(
        cls: t.Type[Lr],
        *,
        throttle: t.Optional[rush.throttle.Throttle] = None,
    ) -> Lr:
        """Create an unauthenticated limiter.

        :param throttle:
            (Optional) The rush throttle to use as the backing limitation
        :returns:
            Limiter (or subclass) instance
        """
        return cls(
            requests_per_hour=RateLimits.UNAUTHENTICATED, throttle=throttle
        )

    @classmethod
    def github_actions(
        cls: t.Type[Lr],
        *,
        throttle: t.Optional[rush.throttle.Throttle] = None,
    ) -> Lr:
        """Create an unauthenticated limiter.

        :param throttle:
            (Optional) The rush throttle to use as the backing limitation
        :returns:
            Limiter (or subclass) instance
        """
        return cls(
            requests_per_hour=RateLimits.ACTIONS_TOKEN, throttle=throttle
        )

    @classmethod
    def enterprise_cloud(
        cls: t.Type[Lr],
        *,
        throttle: t.Optional[rush.throttle.Throttle] = None,
    ) -> Lr:
        """Create an unauthenticated limiter.

        :param throttle:
            (Optional) The rush throttle to use as the backing limitation
        :returns:
            Limiter (or subclass) instance
        """
        return cls(
            requests_per_hour=RateLimits.ENTERPRISE_CLOUD, throttle=throttle
        )

    def __init__(
        self,
        *,
        requests_per_hour: RateLimits,
        username: t.Optional[users.UserLike] = None,
        repository: t.Optional[str] = None,
        throttle: t.Optional[rush.throttle.Throttle] = None,
    ) -> None:
        """Initialize the limiter.

        :param requests_per_hour:
        :param username:
        :param repository:
        :param throttle:
        """
        self.requests_per_hour = requests_per_hour
        self.username = username
        self.repository = repository
        self.throttle = throttle
        self.authenticated = self.username is not None
        if not self.throttle:
            self.throttle = in_memory(self.requests_per_hour)

    @property
    def _key(self) -> str:
        if not self.authenticated:
            return "anonymous"
        if self.repository:
            return f"{self.username}/{self.repository}"
        return self.username

    def copy_with(self: Lr, *, repository: str) -> Lr:
        """Duplicate current limiter for a new repository."""
        return self.__class__(
            requests_per_hour=self.requests_per_hour,
            username=self.username,
            repository=repository,
            throttle=self.throttle,
        )

    def estimate(self) -> RateLimitEstimate:
        """Retrieve an estimate of the current usage."""
        return RateLimitEstimate(self.throttle.peek(self._key))

    def is_ratelimited(self) -> bool:
        """Check if we're currently rate-limited."""
        return self.estimate().limited

    def update(self, count: int = 1) -> RateLimitEstimate:
        """Update the rate-limit usage with a new request."""
        return RateLimitEstimate(self.throttle.check(self._key, count))


def in_memory(requests_per_hour: int) -> rush.throttle.Throttle:
    """Produce an in-memory way of tracking rate limit.

    .. warning::

        This is not thread safe and will not survive program termination

    :param int requests_per_hour:
        The number of requests per hour to expect to be allowed
    """
    store = rush.stores.dictionary.DictionaryStore()
    return with_store(requests_per_hour, store)


def with_store(
    requests_per_hour: int, store: rush.stores.base.BaseStore
) -> rush.throttle.Throttle:
    """Create an appropriate throttle with the desired backend store.

    :param int requests_per_hour:
        The number of requests per hour to expect to be allowed
    :param store:
        The storage mechanism to use for the throttle. Must implement a rush
        storage API. See also:
        https://rush.readthedocs.io/en/latest/storage.html
    :type store:
        :class:`~rush.stores.base.BaseStore`
    """
    limiter = _GitHubPeriodicLimiter(store)
    return rush.throttle.Throttle(
        rush.quota.Quota.per_hour(requests_per_hour), limiter
    )


class _GitHubPeriodicLimiter(rush.limiters.periodic.PeriodicLimiter):
    """Periodic ratelimiter that matches GitHub ratelimit behaviour.

    GitHub resets the ratelimit at the top of each hour. The default periodic
    limiter in Rush uses whatever "now" is to start the period.
    """

    def rate_limit(
        self, key: str, quantity: int, rate: rush.quota.Quota
    ) -> rush.result.RateLimitResult:
        """Apply the rate-limit to a quantity of requests."""

        def _fresh_limitdata(rate, now, used=0) -> rush.limit_data.LimitData:
            return rush.limit_data.LimitData(
                used=used,
                remaining=(rate.limit - used),
                created_at=now.replace(minute=0, second=0, microsecond=0),
            )

        now = self.store.current_time()
        olddata = self.store.get(key)
        last_created_at = (
            olddata.created_at
            if olddata
            else now.replace(minute=0, second=0, microsecond=0)
            # GitHub resets the ratelimit on the hour
        )

        elapsed_time = now - last_created_at

        if (
            rate.period > elapsed_time
            and olddata is not None
            and (olddata.remaining == 0 or olddata.remaining < quantity)
        ):
            return self.result_from_quota(
                rate=rate,
                limited=True,
                limitdata=olddata or _fresh_limitdata(rate, now),
                elapsed_since_period_start=elapsed_time,
            )

        if rate.period < elapsed_time:
            # New period to start
            limitdata = _fresh_limitdata(rate, now, used=quantity)
            limitdata = self.store.set(key=key, data=limitdata)
        else:
            copy_from = olddata or _fresh_limitdata(rate, now)
            limitdata = copy_from.copy_with(
                remaining=(copy_from.remaining - quantity),
                used=(copy_from.used + quantity),
            )
            self.store.compare_and_swap(key=key, old=olddata, new=limitdata)

        return self.result_from_quota(
            rate=rate,
            limited=False,
            limitdata=limitdata,
            elapsed_since_period_start=elapsed_time,
        )
