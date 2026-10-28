"""The shared rounding helpers (hajiri.core.numbers) that pages, reports and charts use."""
from hajiri.core.numbers import round_half_up, to_fixed, whole


def test_round_half_up_sends_halves_up_and_returns_whole_numbers_as_int():
    assert round_half_up(2.5) == 3 and round_half_up(-2.5) == -2 and round_half_up(2.49) == 2
    assert round_half_up(12.345, 2) == 12.35 and round_half_up(-1.25, 1) == -1.2
    assert round_half_up(4.0, 1) == 4 and isinstance(round_half_up(4.0, 1), int)
    assert isinstance(round_half_up(4.25, 1), float)


def test_to_fixed_rounds_the_exact_binary_value():
    assert to_fixed(6.25, 1) == '6.3' and to_fixed(1.005, 2) == '1.00' and to_fixed(-0.125, 2) == '-0.13'
    assert to_fixed(3, 2) == '3.00'


def test_whole():
    assert whole(4.0) == 4 and isinstance(whole(4.0), int)
    assert whole(4.5) == 4.5 and whole(7) == 7
