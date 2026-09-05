"""Canonical units (§2 of the architecture).

All quantities are integers at fixed precision, converted once at ingestion:
weight in grams, volume in liters, durations in minutes, money in currency cents.
Latitude/longitude stay float64 WGS84 degrees — they never enter a solver model.
"""

GRAMS_PER_KG = 1_000
LITERS_PER_M3 = 1_000
MINUTES_PER_DAY = 24 * 60
CENTS_PER_UNIT = 100

# Fixed integer scale applied to normalized objective contributions when they
# enter a CP-SAT model (§2, §7.4). Defined once, here. 10^8 rather than 10^4 so
# per-unit coefficients (e.g. balance deviation per inventory unit against a large
# target) survive integer rounding with real resolution.
OBJECTIVE_SCALE = 100_000_000


def kg(value: float) -> int:
    """Kilograms → canonical grams."""
    return round(value * GRAMS_PER_KG)


def m3(value: float) -> int:
    """Cubic meters → canonical liters."""
    return round(value * LITERS_PER_M3)


def currency(value: float) -> int:
    """Major currency units → canonical cents."""
    return round(value * CENTS_PER_UNIT)
