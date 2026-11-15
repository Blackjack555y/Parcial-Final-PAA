"""Carga datos de demostración en las tablas de un ambiente.

Uso:
    python scripts/seed.py dev            # crea lo que falte, sin pisar estados actuales
    python scripts/seed.py test --reset   # vuelve conectores y saldos a su valor inicial

Las tablas las crea Terraform; este script solo escribe datos usando los repositorios de los
propios servicios (así respeta el formato que cada servicio espera).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for svc in ("station-reservation-service", "billing-wallet-service"):
    sys.path.insert(0, str(ROOT / "services" / svc))

from aegis_common import Environment, Settings  # noqa: E402
from aegis_common.aws import client  # noqa: E402
from billing_wallet.repository import BillingRepository  # noqa: E402
from station_reservation.repository import ReservationRepository  # noqa: E402

STATIONS = [
    {
        "station_id": "bog-norte",
        "name": "Electrolinera Bogotá Norte",
        "city": "Bogotá",
        "address": "Autopista Norte # 180-50",
        "connectors": [
            {"connector_id": "c1", "type": "CCS2", "power_kw": 150},
            {"connector_id": "c2", "type": "CCS2", "power_kw": 150},
            {"connector_id": "c3", "type": "ULTRA", "power_kw": 350},
        ],
    },
    {
        "station_id": "med-poblado",
        "name": "Hub Medellín El Poblado",
        "city": "Medellín",
        "address": "Carrera 43A # 1-50",
        "connectors": [
            {"connector_id": "c1", "type": "CCS2", "power_kw": 150},
            {"connector_id": "c2", "type": "ULTRA", "power_kw": 350},
        ],
    },
    {
        "station_id": "cali-sur",
        "name": "Centro Logístico Cali Sur",
        "city": "Cali",
        "address": "Calle 70 # 1-20",
        "connectors": [
            {"connector_id": "c1", "type": "CCS2", "power_kw": 150},
            {"connector_id": "c2", "type": "CCS2", "power_kw": 150},
        ],
    },
]

WALLETS = [
    ("fleet-rapidoexpress", "RápidoExpress Logística", 50_000),  # USD 500,00
    ("fleet-ultimamilla", "Última Milla SAS", 120_000),  # USD 1.200,00
    ("fleet-sinfondos", "Flota Demo Sin Fondos", 500),  # USD 5,00 → fondos insuficientes
]


def seed(settings: Settings, *, reset: bool = False) -> None:
    ddb = client("dynamodb", settings)
    reservations = ReservationRepository(ddb, settings.table("reservations"))
    billing = BillingRepository(ddb, settings.table("billing"))
    for station in STATIONS:
        reservations.put_station(station, reset=reset)
    for fleet_id, name, balance in WALLETS:
        billing.put_wallet(fleet_id, name, balance, reset=reset)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("env", choices=[e.value for e in Environment])
    parser.add_argument("--reset", action="store_true", help="restablecer conectores y saldos")
    args = parser.parse_args()
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
    settings = Settings.from_env(
        {**os.environ, "AEGIS_ENV": args.env, "AEGIS_SERVICE": "seed"},
        aws_endpoint_url=os.environ.get("AWS_ENDPOINT_URL", "http://localhost:4566"),
    )
    seed(settings, reset=args.reset)
    print(
        f"Datos cargados en {settings.table('reservations')} y {settings.table('billing')}"
        f"{' (reset)' if args.reset else ''}: {len(STATIONS)} estaciones, {len(WALLETS)} flotas."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
