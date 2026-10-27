"""Port of src/words.test.ts (plan §13.7)."""
from engines.payroll import amount_in_words_en, amount_in_words_ne, format_npr, to_devanagari_digits


def test_english_with_lakh_crore_grouping():
    assert amount_in_words_en('40000') == 'Forty Thousand Rupees Only'
    assert amount_in_words_en('2163.46') == 'Two Thousand One Hundred Sixty-Three Rupees and Forty-Six Paisa Only'
    assert amount_in_words_en('12345678.5') == (
        'One Crore Twenty-Three Lakh Forty-Five Thousand Six Hundred Seventy-Eight Rupees and Fifty Paisa Only')
    assert amount_in_words_en('0') == 'Zero Rupees Only'


def test_nepali():
    assert amount_in_words_ne('40000') == 'चालीस हजार रुपैयाँ मात्र'
    assert amount_in_words_ne('28387.10') == 'अट्ठाइस हजार तीन सय सतासी रुपैयाँ दश पैसा मात्र'
    assert amount_in_words_ne('1500000') == 'पन्ध्र लाख रुपैयाँ मात्र'


def test_npr_grouping_and_numerals():
    assert format_npr('123456.7') == '1,23,456.70'
    assert format_npr('999') == '999.00'
    assert format_npr('-1234.5') == '-1,234.50'
    assert format_npr('123456.7', 'devanagari') == '१,२३,४५६.७०'
    assert to_devanagari_digits('2083-05-12') == '२०८३-०५-१२'
