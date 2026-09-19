"""日历工具：还款计划按月推算，不依赖第三方库。"""

from datetime import date, timedelta


def add_months(d: date, months: int) -> date:
    """返回 d 之后第 months 个自然月的同日；起始日落在目标月之外时收敛到月末。"""
    month_index = d.year * 12 + (d.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))
