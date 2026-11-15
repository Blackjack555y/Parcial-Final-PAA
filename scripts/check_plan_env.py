"""Guardia de aislamiento: falla si un plan de Terraform toca recursos de OTRO ambiente.

Uso:  terraform show -json tfplan > plan.json
      python scripts/check_plan_env.py <dev|test|prod> plan.json

Recorre todos los valores (before/after) de los recursos que el plan crea, cambia o destruye
y busca nombres con ambiente: aegisvolt-<env>, aegisvolt-<tabla>-<env>, /aegisvolt/<env>/,
env:/<env>/. Si aparece un ambiente distinto al del workspace, el check falla y el PR no se
puede fusionar.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

ENVS = ("dev", "test", "prod")
ENV_PATTERNS = [
    re.compile(r"\baegisvolt-(?:[a-z0-9]+-)*?(dev|test|prod)\b"),
    re.compile(r"/aegisvolt/(dev|test|prod)/"),
    re.compile(r"\benv:/(dev|test|prod)/"),
    re.compile(r"\bAegisVolt/(dev|test|prod)\b"),
]


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def find_violations(plan: dict, target_env: str) -> list[str]:
    if target_env not in ENVS:
        raise ValueError(f"Ambiente inválido: {target_env!r}")
    violations: list[str] = []
    for rc in plan.get("resource_changes", []):
        change = rc.get("change", {})
        actions = change.get("actions", [])
        if actions in (["no-op"], ["read"]):
            continue
        for side in ("before", "after"):
            for text in _strings(change.get(side)):
                for pattern in ENV_PATTERNS:
                    for match in pattern.finditer(text):
                        if match.group(1) != target_env:
                            violations.append(
                                f"{rc['address']} ({'/'.join(actions)}): "
                                f"'{match.group(0)}' pertenece a '{match.group(1)}'"
                            )
    return sorted(set(violations))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    target_env, plan_path = argv[1], Path(argv[2])
    plan = json.loads(plan_path.read_text())
    violations = find_violations(plan, target_env)
    changes = [
        rc
        for rc in plan.get("resource_changes", [])
        if rc.get("change", {}).get("actions") not in (["no-op"], ["read"])
    ]
    if violations:
        print(f"❌ El plan del workspace '{target_env}' toca recursos de otro ambiente:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"✅ Aislamiento OK: {len(changes)} cambios, todos del ambiente '{target_env}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
