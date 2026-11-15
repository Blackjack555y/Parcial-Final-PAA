"""Exporta el esquema GraphQL a services/graphql-gateway/schema.graphql.

python scripts/export_schema.py           escribe el archivo
python scripts/export_schema.py --check   falla si el archivo no coincide con el código (CI)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "graphql-gateway"))

from aegis_common import Environment  # noqa: E402
from graphql_gateway.main import build_schema  # noqa: E402

TARGET = ROOT / "services" / "graphql-gateway" / "schema.graphql"


def main() -> int:
    sdl = build_schema(Environment.DEV).as_str() + "\n"
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != sdl:
            print("schema.graphql desactualizado: ejecuta python scripts/export_schema.py")
            return 1
        print("schema.graphql al día")
        return 0
    TARGET.write_text(sdl)
    print(f"Esquema exportado a {TARGET.relative_to(ROOT)} ({len(sdl.splitlines())} líneas)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
