"""Tarifas de carga. Lógica pura (sin AWS) para poder probarla de forma unitaria.

Montos SIEMPRE en centavos enteros (nunca float): evita errores de redondeo en dinero.

Tarifa dinámica (ENABLE_DYNAMIC_SURGE_PRICING): en horas pico se multiplica el precio por
SURGE_MULTIPLIER. Las horas pico se configuran por ambiente con PEAK_HOURS="17-22"
(hora local de PRICING_TZ). En dev/test se usa "0-24" para poder demostrarla a cualquier hora.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

BASE_PRICE_CENTS_PER_KWH = {"CCS2": 45, "ULTRA": 65}


@dataclass(frozen=True, slots=True)
class Quote:
    connector_type: str
    kwh: float
    unit_price_cents: int
    surge_active: bool
    surge_multiplier: float
    amount_cents: int

    def as_dict(self) -> dict:
        return asdict(self)


def parse_peak_hours(value: str) -> tuple[int, int]:
    """'17-22' → (17, 22). Intervalo [inicio, fin) en horas 0..24."""
    try:
        start_s, end_s = value.split("-", 1)
        start, end = int(start_s), int(end_s)
    except ValueError as exc:
        raise ValueError(f"PEAK_HOURS inválido: {value!r} (formato 'HH-HH')") from exc
    if not (0 <= start < end <= 24):
        raise ValueError(f"PEAK_HOURS fuera de rango: {value!r}")
    return start, end


def is_peak(now: datetime, peak_hours: tuple[int, int]) -> bool:
    start, end = peak_hours
    return start <= now.hour < end


def quote(
    connector_type: str,
    kwh: float,
    *,
    surge_flag: bool,
    now: datetime,
    peak_hours: tuple[int, int],
    surge_multiplier: float,
) -> Quote:
    if connector_type not in BASE_PRICE_CENTS_PER_KWH:
        raise ValueError(f"Tipo de conector desconocido: {connector_type}")
    if not (0 < kwh <= 500):
        raise ValueError("kwh debe estar entre 0 y 500")
    surge_active = surge_flag and is_peak(now, peak_hours)
    multiplier = surge_multiplier if surge_active else 1.0
    unit = Decimal(BASE_PRICE_CENTS_PER_KWH[connector_type]) * Decimal(str(multiplier))
    unit_cents = int(unit.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    amount = (Decimal(str(kwh)) * unit_cents).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return Quote(
        connector_type=connector_type,
        kwh=kwh,
        unit_price_cents=unit_cents,
        surge_active=surge_active,
        surge_multiplier=multiplier,
        amount_cents=int(amount),
    )


def local_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))
