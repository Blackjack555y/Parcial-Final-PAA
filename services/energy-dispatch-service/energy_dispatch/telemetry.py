"""Simulación de la telemetría del cargador (lógica pura, probada de forma unitaria).

Curva típica de carga rápida: rampa de subida (30 s) hasta la potencia nominal, meseta, y
reducción progresiva al pasar el 80 % del estado de carga (SoC) para proteger la batería.
La batería del vehículo de reparto se modela en 75 kWh, iniciando al 20 %.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

BATTERY_KWH = 75.0
START_SOC = 20.0
RAMP_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class Sample:
    elapsed_s: float
    power_kw: float
    energy_kwh: float
    soc_percent: float
    voltage_v: float
    current_a: float

    def as_dict(self) -> dict:
        return asdict(self)


def sample(elapsed_s: float, rated_kw: float, target_kwh: float, step_s: float = 1.0) -> Sample:
    """Integra la curva de potencia hasta elapsed_s (paso de 1 s) y devuelve la muestra."""
    elapsed_s = max(0.0, elapsed_s)
    energy = 0.0
    power = 0.0
    t = 0.0
    while t < elapsed_s and energy < target_kwh:
        dt = min(step_s, elapsed_s - t)
        soc = START_SOC + energy / BATTERY_KWH * 100
        power = _power(t, rated_kw, soc)
        energy = min(target_kwh, energy + power * dt / 3600)
        t += dt
    if energy >= target_kwh:
        power = 0.0  # objetivo alcanzado: el cargador corta
    soc = min(100.0, START_SOC + energy / BATTERY_KWH * 100)
    voltage = 400.0 if rated_kw < 200 else 800.0
    return Sample(
        elapsed_s=round(elapsed_s, 1),
        power_kw=round(power, 1),
        energy_kwh=round(energy, 3),
        soc_percent=round(soc, 1),
        voltage_v=voltage,
        current_a=round(power * 1000 / voltage, 1),
    )


def _power(t: float, rated_kw: float, soc: float) -> float:
    ramp = min(1.0, t / RAMP_SECONDS) if RAMP_SECONDS else 1.0
    taper = 1.0 if soc < 80 else max(0.15, (100 - soc) / 20)
    return rated_kw * ramp * taper
