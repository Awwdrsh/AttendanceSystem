"""The gateway environment is validated by a DRF serializer that keeps the TS gateway's (zod) messages."""
import pytest

from hajiri.gateway.config import load_config


def test_defaults():
    cfg = load_config({})
    assert (cfg.PORT, cfg.LOG_LEVEL, cfg.GATEWAY_MAX_PENDING_DEVICES, cfg.GATEWAY_MAX_CLOCK_SKEW_MIN, cfg.GATEWAY_RAW_DIR,
            cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS, cfg.DATA_ENCRYPTION_KEY) == (8080, 'info', 50, 10, 'data/raw', False, None)


def test_values_are_coerced_like_zod():
    cfg = load_config({'PORT': ' 9000 ', 'GATEWAY_ALLOW_DANGEROUS_COMMANDS': 'true', 'GATEWAY_SIGNING_KEY': '',
                       'LOG_LEVEL': 'debug'})
    assert (cfg.PORT, cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS, cfg.GATEWAY_SIGNING_KEY, cfg.LOG_LEVEL) == (9000, True, '', 'debug')


def test_every_problem_is_reported_in_field_order_with_the_ts_wording():
    with pytest.raises(ValueError) as e:
        load_config({'PORT': 'abc', 'LOG_LEVEL': 'x', 'DATABASE_URL': '', 'DATA_ENCRYPTION_KEY': 'short',
                     'GATEWAY_MAX_PENDING_DEVICES': '-1', 'GATEWAY_MAX_CLOCK_SKEW_MIN': '1.5',
                     'GATEWAY_ALLOW_DANGEROUS_COMMANDS': 'yes'})
    assert str(e.value) == (
        'Invalid environment: PORT: Invalid input: expected number, received NaN; '
        'LOG_LEVEL: Invalid option: expected one of "fatal"|"error"|"warn"|"info"|"debug"|"trace"; '
        'DATABASE_URL: Too small: expected string to have >=1 characters; '
        'DATA_ENCRYPTION_KEY: Too small: expected string to have >=40 characters; '
        'GATEWAY_MAX_PENDING_DEVICES: Too small: expected number to be >0; '
        'GATEWAY_MAX_CLOCK_SKEW_MIN: Invalid input: expected int, received number; '
        'GATEWAY_ALLOW_DANGEROUS_COMMANDS: Invalid option: expected one of "true"|"false"')
    with pytest.raises(ValueError, match='PORT: Too small: expected number to be >0'):
        load_config({'PORT': ''})


def test_utc_ms_rolls_over_past_year_9999_like_date_utc():
    from hajiri.gateway.jsutil import utc_ms
    # Date.UTC(9999, 12, 1) === 253402300800000 (month 13 of 9999 is January 10000).
    assert utc_ms(9999, 13, 1) == 253_402_300_800_000
    assert utc_ms(2026, 0, 1) == utc_ms(2025, 12, 1)
    assert utc_ms(99, 1, 1) == utc_ms(1999, 1, 1)


def test_drift_of_a_far_future_device_clock_is_computed_not_raised():
    from hajiri.gateway.adms import drift_seconds
    assert drift_seconds('9999-13-01 00:00:00', 345, 0) == 253_402_300_800 - 345 * 60
