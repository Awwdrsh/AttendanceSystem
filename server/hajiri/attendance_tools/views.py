"""Attendance flags in words, shared by the Home summary (hajiri.home.services)."""
from __future__ import annotations


def describe_flag(f: dict) -> str:
    """HomePage describeFlag: a flag in words."""
    k = f.get('kind')
    return {
        'Late': f"Late by {f.get('minutes')} min",
        'EarlyOut': f"Left {f.get('minutes')} min early",
        'MissingOut': 'No clock-out yet',
        'UnpairedPunch': 'A clock-in or clock-out could not be paired',
        'OTPreapprovalMissing': f"{f.get('rawMinutes')} min overtime without pre-approval",
        'OTCapExceeded': f"Overtime cap exceeded by {f.get('minutes')} min",
        'HolidayWork': f"Worked on a holiday ({f.get('workedMin')} min)",
        'UnpaidLeave': 'Unpaid leave',
        'HalfDayLeave': 'Half-day leave',
        'AutoClockOut': 'Clocked out automatically; request a correction if this is wrong',
        'ClockSkew': 'Your device clock was off, so server time was used',
        'OutsideLocation': 'Clocked outside your work location (flagged for review)',
        'NoLocation': 'Location was not available',
        'Field': 'Recorded on a field visit',
        'Kiosk': 'Recorded at a kiosk',
    }.get(k, k)
