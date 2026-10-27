"""Fill the bs_calendar table from the engine's month table (port of apps/api/src/db/seed-bs-calendar.ts). The
application reads BS dates from the table at run time, so this runs on install and after a calendar update."""
from engines.bs_calendar import all_supported_days, format_bs

from .db import execute


def seed_bs_calendar() -> int:
    """Upsert every supported day in one statement; returns the rows written."""
    y, m, d, label, ad = [], [], [], [], []
    for bs, ad_iso in all_supported_days():
        y.append(bs.year); m.append(bs.month); d.append(bs.day); label.append(format_bs(bs)); ad.append(ad_iso)  # noqa: E702
    return execute("""
        INSERT INTO bs_calendar (bs_year, bs_month, bs_day, bs_date, ad_date)
        SELECT * FROM unnest(%s::int[], %s::int[], %s::int[], %s::text[], %s::date[])
        ON CONFLICT (ad_date) DO UPDATE SET bs_year = EXCLUDED.bs_year, bs_month = EXCLUDED.bs_month,
          bs_day = EXCLUDED.bs_day, bs_date = EXCLUDED.bs_date""", [y, m, d, label, ad])
