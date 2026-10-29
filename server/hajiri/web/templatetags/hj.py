"""Display helpers shared by services (the page template library it once held went with the server-rendered UI)."""
from __future__ import annotations

from django import template

register = template.Library()


def live_status(status, *, on_date, today, last_out, first_in):
    """Today's "Incomplete" just means the person has not clocked out yet."""
    if on_date == today and status == 'Incomplete' and first_in and not last_out:
        return 'Working'
    return status
