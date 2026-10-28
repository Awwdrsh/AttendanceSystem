"""Display labels and small formatters for the hiring pages and the public careers site (api/hiring.ts, hiring/shared.tsx)."""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from hajiri.core.clock import NPT
from hajiri.web.format import fmt_date_short, fmt_money, fmt_time

WORK_MODE = {'onsite': 'On site', 'remote': 'Remote', 'hybrid': 'Hybrid'}
EMPLOYMENT = {'full_time': 'Full-time', 'part_time': 'Part-time', 'contract': 'Contract', 'internship': 'Internship',
              'temporary': 'Temporary'}
SOURCE_LABEL = {'careers': 'Careers page', 'referral': 'Referral', 'manual': 'Added by HR', 'job_board': 'Job board', 'other': 'Other'}
RECOMMENDATION = {'strong_no': ('Strong no', 'bad'), 'no': ('No', 'warn'), 'yes': ('Yes', 'ok'), 'strong_yes': ('Strong yes', 'ok')}
MODE_LABEL = {'in_person': 'In person', 'phone': 'Phone', 'video': 'Video'}

JOB_STATUS = {'draft': ('Draft', 'idle'), 'open': ('Open', 'ok'), 'closed': ('Closed', 'idle')}
REQ_STATUS = {'pending': ('Waiting for HR', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Rejected', 'bad'),
              'cancelled': ('Cancelled', 'idle'), 'filled': ('Filled', 'accent')}
APP_STATUS = {'active': ('In progress', 'info'), 'rejected': ('Not selected', 'idle'), 'withdrawn': ('Withdrawn', 'idle'),
              'hired': ('Hired', 'ok')}
OFFER_STATUS = {'pending_approval': ('Waiting for approval', 'warn'), 'approved': ('Approved, not answered', 'info'),
                'rejected': ('Not approved', 'bad'), 'accepted': ('Accepted', 'ok'), 'declined': ('Declined', 'warn'),
                'withdrawn': ('Withdrawn', 'idle')}
EVENT_LABEL = {'applied': 'Applied', 'stage': 'Moved', 'note': 'Note', 'rating': 'Rating', 'email': 'E-mail sent', 'interview': 'Interview',
               'scorecard': 'Scorecard', 'offer': 'Offer', 'hired': 'Hired', 'rejected': 'Not selected', 'reopened': 'Reopened'}
DEFAULT_CRITERIA = ['Skills', 'Experience', 'Communication', 'Culture fit']


def label(table: dict, key) -> tuple[str, str]:
    return table.get(key, (key, 'idle'))


def _n(x) -> str:
    """3 not 3.0, 2.5 as 2.5."""
    return format(Decimal(str(x)).normalize(), 'f')


def experience_text(mn, mx) -> str | None:
    """'2–5 years', '3+ years' or None when the job sets no experience range."""
    if mn is None and mx is None:
        return None
    if mn is not None and mx is not None:
        return f'{_n(mn)}–{_n(mx)} years'
    return f'{_n(mn)}+ years' if mn is not None else f'Up to {_n(mx)} years'


def salary_text(mn, mx, suffix: str = ' a month') -> str | None:
    vals = [x for x in (mn, mx) if x]
    if not vals:
        return None
    return 'NPR ' + ' – '.join(fmt_money(str(x), currency=False, decimals=0) for x in vals) + suffix


def score_badge(score, knocked_out) -> dict:
    """ScoreBadge: the text, tone and tooltip of the screening score."""
    if knocked_out:
        return {'text': '—' if score is None else str(score), 'tone': 'bad', 'tip': 'Did not meet a must-have question', 'warn': True}
    if score is None:
        return {'text': 'No score', 'tone': 'idle', 'tip': None, 'warn': False}
    return {'text': str(score), 'tone': 'ok' if score >= 75 else 'warn' if score >= 50 else 'idle', 'tip': None, 'warn': False}


def npt_date_of(at: datetime) -> str:
    return at.astimezone(NPT).date().isoformat()


def fmt_when(at: datetime | None) -> str:
    """'27 Ashwin, 11:00' for an instant, in Nepal time and the BS calendar."""
    if not at:
        return ''
    return f'{fmt_date_short(npt_date_of(at))}, {fmt_time(at)}'


def prose_blocks(text: str | None) -> list[dict]:
    """Plain text with '- ' bullets as blocks: {'p': text} or {'ul': [items]} (no HTML from the job post)."""
    blocks: list[dict] = []
    bullets: list[str] = []
    for line in (text or '').split('\n'):
        t = line.strip()
        if re.match(r'^[-*•]\s+', t):
            bullets.append(re.sub(r'^[-*•]\s+', '', t))
            continue
        if bullets:
            blocks.append({'ul': bullets})
            bullets = []
        if t:
            blocks.append({'p': t})
    if bullets:
        blocks.append({'ul': bullets})
    return blocks
