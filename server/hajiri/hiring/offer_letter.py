"""Printable A4 offer letter in the CoreValley style (port of apps/api/src/modules/hiring/offer-letter.ts), drawn from
the auto-escaped template hiring/print/offer_letter.html."""
from __future__ import annotations

import re

from django.template.loader import render_to_string

from engines.payroll.words import amount_in_words_en, format_npr
from hajiri.core.bsdate import bs_label

TYPE = {'full_time': 'full-time', 'part_time': 'part-time', 'contract': 'contract', 'internship': 'internship', 'temporary': 'temporary'}


def render_offer_letter(*, company: str, candidate: str, designation: str, department: str | None, location: str | None,
                        employment_type: str, salary, join_date, expires_on, issued_on: str, notes: str | None, reference: str,
                        draft: bool) -> str:
    """Marked DRAFT until the offer is approved. Notes become paragraphs at blank lines, single line breaks kept."""
    return render_to_string('hiring/print/offer_letter.html', {
        'company': company, 'candidate': candidate, 'designation': designation, 'department': department, 'location': location,
        'kind': TYPE.get(employment_type, 'full-time'), 'salary': format_npr(str(salary), 'latin'),
        'salary_words': amount_in_words_en(str(salary)), 'join_label': bs_label(join_date),
        'expires_label': bs_label(expires_on) if expires_on else None, 'issued_label': bs_label(issued_on),
        'reference': reference, 'draft': draft,
        'paragraphs': [p for p in (x.strip() for x in re.split(r'\n{2,}', notes or '')) if p],
    })
