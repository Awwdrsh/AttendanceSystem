"""Amounts in words (English and Nepali, lakh/crore grouping) and NPR formatting (port of src/words.ts).

The arithmetic runs at the engine's 30-digit precision like the TS version, and out-of-range table lookups print
'undefined' as JS would; both only matter for amounts of 30+ digits, never for a real payslip.
"""
from __future__ import annotations

import math
from typing import Any

from ._js import js_mod
from .numeric import D, Decimal, engine_context, floor, is_neg, to_dp, to_fixed

__all__ = ['amount_in_words_en', 'amount_in_words_ne', 'to_devanagari_digits', 'format_npr']

ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen',
        'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen']
TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']

NE = [
    'शून्य', 'एक', 'दुई', 'तीन', 'चार', 'पाँच', 'छ', 'सात', 'आठ', 'नौ', 'दश', 'एघार', 'बाह्र', 'तेह्र', 'चौध', 'पन्ध्र', 'सोह्र', 'सत्र', 'अठार', 'उन्नाइस',
    'बीस', 'एक्काइस', 'बाइस', 'तेइस', 'चौबीस', 'पच्चीस', 'छब्बीस', 'सत्ताइस', 'अट्ठाइस', 'उनन्तीस', 'तीस', 'एकतीस', 'बत्तीस', 'तेत्तीस', 'चौंतीस', 'पैंतीस', 'छत्तीस', 'सैंतीस', 'अठतीस', 'उनन्चालीस',
    'चालीस', 'एकचालीस', 'बयालीस', 'त्रिचालीस', 'चवालीस', 'पैंतालीस', 'छयालीस', 'सच्चालीस', 'अठचालीस', 'उनन्चास', 'पचास', 'एकाउन्न', 'बाउन्न', 'त्रिपन्न', 'चवन्न', 'पचपन्न', 'छपन्न', 'सन्ताउन्न', 'अन्ठाउन्न', 'उनन्साठी',
    'साठी', 'एकसट्ठी', 'बयसट्ठी', 'त्रिसट्ठी', 'चौंसट्ठी', 'पैंसट्ठी', 'छयसट्ठी', 'सतसट्ठी', 'अठसट्ठी', 'उनन्सत्तरी', 'सत्तरी', 'एकहत्तर', 'बहत्तर', 'त्रिहत्तर', 'चौहत्तर', 'पचहत्तर', 'छयहत्तर', 'सतहत्तर', 'अठहत्तर', 'उनासी',
    'असी', 'एकासी', 'बयासी', 'त्रियासी', 'चौरासी', 'पचासी', 'छयासी', 'सतासी', 'अठासी', 'उनान्नब्बे', 'नब्बे', 'एकान्नब्बे', 'बयान्नब्बे', 'त्रियान्नब्बे', 'चौरान्नब्बे', 'पन्चान्नब्बे', 'छयान्नब्बे', 'सन्तान्नब्बे', 'अन्ठान्नब्बे', 'उनान्सय',
]


def _at(table: list[str], n: int) -> str:
    """table[n] as JS reads it: a negative or past-the-end index is undefined."""
    return table[n] if 0 <= n < len(table) else 'undefined'


def _num(d: Decimal) -> int:
    """toNumber() of a whole Decimal."""
    return int(d)


def _two_digits_en(n: int) -> str:
    if n < 20:
        return _at(ONES, n)
    return f"{_at(TENS, math.floor(n / 10))}{'-' + _at(ONES, js_mod(n, 10)) if js_mod(n, 10) else ''}"


def _three_digits_en(n: int) -> str:
    h = math.floor(n / 100)
    r = js_mod(n, 100)
    return ' '.join(p for p in [f'{_at(ONES, h)} Hundred' if h else '', _two_digits_en(r) if r else ''] if p)


def _group(n: Decimal, crore_word: str, lakh: Any, thousand: Any, rest_words: Any) -> str:
    """Shared lakh/crore split: crore (1e7), lakh (1e5), thousand (1e3), then the last three digits."""
    parts: list[str] = []
    rest = n
    crore = floor(rest / 10_000_000)
    rest = rest - crore * 10_000_000
    if crore > 0:
        parts.append(f'{_group(crore, crore_word, lakh, thousand, rest_words)} {crore_word}')
    lk = _num(floor(rest / 100_000))
    rest = rest - lk * 100_000
    if lk:
        parts.append(lakh(lk))
    th = _num(floor(rest / 1000))
    rest = rest - th * 1000
    if th:
        parts.append(thousand(th))
    parts.extend(rest_words(_num(rest)))
    return ' '.join(parts)


def _group_en(n: Decimal) -> str:
    return _group(n, 'Crore', lambda v: f'{_two_digits_en(v)} Lakh', lambda v: f'{_two_digits_en(v)} Thousand',
                  lambda h: [_three_digits_en(h)] if h else [])


def _rest_ne(h: int) -> list[str]:
    hundred = math.floor(h / 100)
    r = js_mod(h, 100)
    out = []
    if hundred:
        out.append(f'{_at(NE, hundred)} सय')
    if r:
        out.append(_at(NE, r))
    return out


def _group_ne(n: Decimal) -> str:
    return _group(n, 'करोड', lambda v: f'{_at(NE, v)} लाख', lambda v: f'{_at(NE, v)} हजार', _rest_ne)


def _split(v: Any) -> tuple[Decimal, Decimal, int]:
    signed = to_dp(D(v), 2)
    d = signed.copy_abs()
    rupees = floor(d)
    paisa = _num(to_dp((d - rupees) * 100, 0))
    return signed, rupees, paisa


@engine_context
def amount_in_words_en(v: Any) -> str:
    """Amount in words, English, Indian/Nepali grouping (lakh, crore)."""
    signed, rupees, paisa = _split(v)
    words = 'Zero' if rupees.is_zero() else _group_en(rupees)
    minus = 'Minus ' if is_neg(signed) and not signed.is_zero() else ''
    return f"{minus}{words} Rupees{f' and {_two_digits_en(paisa)} Paisa' if paisa else ''} Only"


@engine_context
def amount_in_words_ne(v: Any) -> str:
    """Amount in words, Nepali (Devanagari), lakh/crore grouping."""
    signed, rupees, paisa = _split(v)
    words = NE[0] if rupees.is_zero() else _group_ne(rupees)
    minus = 'ऋण ' if is_neg(signed) and not signed.is_zero() else ''
    return f"{minus}{words} रुपैयाँ{f' {_at(NE, paisa)} पैसा' if paisa else ''} मात्र"


_DEVANAGARI = str.maketrans('0123456789', '०१२३४५६७८९')


def to_devanagari_digits(s: str) -> str:
    return s.translate(_DEVANAGARI)


@engine_context
def format_npr(v: Any, numerals: str = 'latin') -> str:
    """'1,23,456.00' style grouping (Indian/Nepali)."""
    d = to_dp(D(v), 2)
    neg = is_neg(d) and not d.is_zero()
    parts = to_fixed(d.copy_abs(), 2).split('.')
    int_part = parts[0]
    frac = parts[1] if len(parts) > 1 else 'undefined'
    last3 = int_part[-3:]
    rest = int_part[:-3] if len(int_part) > 3 else ''
    groups: list[str] = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    s = f"{'-' if neg else ''}{','.join([*groups, last3])}.{frac}"
    return to_devanagari_digits(s) if numerals == 'devanagari' else s

