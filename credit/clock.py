"""统一时钟：生产环境使用真实时间，测试可注入固定时间。"""

from datetime import datetime, timezone


def now() -> datetime:
    """当前 UTC 时间（带时区），全系统唯一时间来源。"""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat()


def parse(value: str) -> datetime:
    """解析 ISO-8601 字符串为带时区的 UTC 时间。"""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
