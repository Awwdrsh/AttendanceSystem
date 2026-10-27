"""The seeder's helpers (kit.ts): generated PDFs, dates and the error shape."""
import re
from datetime import datetime, timezone

from demo.kit import ApiError, add_days_iso, base36, iso_z, js_day, make_pdf, npt_instant, pick, week_start


def test_make_pdf_is_a_valid_one_page_pdf():
    pdf = make_pdf('Receipt: Taxi (Kamaladi)', ['Amount: NPR 850.00', 'Paid in cash – thanks'])
    text = pdf.decode('latin1')
    assert text.startswith('%PDF-1.4\n') and text.endswith('%%EOF\n')
    # Parentheses are escaped and non-ASCII characters replaced, as the TS code did.
    assert '(Receipt: Taxi \\(Kamaladi\\)) Tj' in text
    assert '(Paid in cash ? thanks) Tj' in text
    # Every xref offset points at its object, and startxref at the table.
    offsets = [int(m) for m in re.findall(r'^(\d{10}) 00000 n $', text, re.M)]
    assert len(offsets) == 5
    for i, off in enumerate(offsets):
        assert text[off:].startswith(f'{i + 1} 0 obj\n')
    start = int(re.search(r'startxref\n(\d+)\n', text).group(1))
    assert text[start:].startswith('xref\n0 6\n')
    length = int(re.search(r'/Length (\d+)', text).group(1))
    stream = text.split('stream\n', 1)[1].split('\nendstream', 1)[0]
    assert len(stream.encode('latin1')) == length


def test_dates_follow_the_ts_helpers():
    assert add_days_iso('2026-10-26', 7) == '2026-11-02'
    assert add_days_iso('2026-03-01', -1) == '2026-02-28'
    assert js_day('2026-10-25') == 0 and js_day('2026-10-31') == 6
    assert week_start('2026-10-26') == '2026-10-25'      # Monday -> the Sunday before
    assert week_start('2026-10-25') == '2026-10-25'      # Sunday stays
    assert week_start('2026-10-31') == '2026-10-25'      # Saturday
    assert iso_z(npt_instant('2026-10-26', '15:00')) == '2026-10-26T09:15:00.000Z'
    assert iso_z(datetime(2026, 1, 2, 3, 4, 5, 678900, tzinfo=timezone.utc)) == '2026-01-02T03:04:05.678Z'


def test_pick_and_base36_match_javascript():
    assert pick(['a', 'b', 'c'], 4) == 'b' and pick(['a', 'b', 'c'], -1) == 'c'
    assert base36(734211) == 'fqir' and base36(0) == '0'
    assert base36(734211 + 5 * 97).upper() == 'FQW8'


def test_api_error_names_the_route():
    e = ApiError(409, 'you already have an open request for a salary certificate', 'POST /documents/letter-requests')
    assert str(e) == 'POST /documents/letter-requests → 409 you already have an open request for a salary certificate'
