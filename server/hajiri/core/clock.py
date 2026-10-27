"""Nepal time. Business dates are Nepal (UTC+5:45) calendar dates; stored timestamps are UTC."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

NPT = ZoneInfo('Asia/Kathmandu')


def now() -> datetime:
    return datetime.now(timezone.utc)


def npt_today(at: datetime | None = None) -> date:
    return (at or now()).astimezone(NPT).date()
