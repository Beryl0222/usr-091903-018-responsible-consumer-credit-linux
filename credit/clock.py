"""统一时钟，测试时可冻结/快进。"""

from datetime import datetime, timezone


class Clock:
    def __init__(self, fixed=None):
        self._fixed = fixed

    def now(self):
        if self._fixed is not None:
            return self._fixed
        return datetime.now(timezone.utc)

    def set(self, value):
        self._fixed = value

    def advance(self, **delta):
        if self._fixed is None:
            self._fixed = datetime.now(timezone.utc)
        self._fixed += _timedelta(**delta)


def _timedelta(**kwargs):
    from datetime import timedelta

    return timedelta(**kwargs)
