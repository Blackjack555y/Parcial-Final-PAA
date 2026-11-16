"""Flujos de Prefect de AegisVolt.

  aegisvolt-salud-ambientes   cada 5 min   salud de dev/test/prod (gateway, servicios, tablas,
                                           errores 5xx recientes, flag de falla en prod)
  aegisvolt-auditoria-saga    cada 10 min  consistencia de las SAGA entre las 4 tablas:
                                           huérfanas, atascadas, compensadas, cobros sin
                                           reembolso, conectores retenidos

Resultados visibles en el dashboard de Prefect:
  - artifacts de tabla y markdown por ejecución (pestaña Artifacts),
  - estado del flow run: Failed si algún ambiente está DOWN o hay hallazgos CRITICAL,
  - métricas en CloudWatch: EnvironmentHealthy y SagaInconsistencies (AegisVolt/<env>).
"""

from __future__ import annotations

from typing import Any

from prefect import flow, get_run_logger, task
from prefect.artifacts import create_markdown_artifact, create_table_artifact
from prefect.states import Completed, Failed

from aegis_flows import sources
from aegis_flows.rules import audit, health_verdict, summarize

ENVS = ["dev", "test", "prod"]
ICON = {"OK": "🟢", "DEGRADED": "🟡", "DOWN": "🔴"}


# --- Salud de los ambientes ---------------------------------------------------------------
@task(name="chequear-ambiente", retries=2, retry_delay_seconds=3, log_prints=True)
def check_environment(env: str) -> dict[str, Any]:
    checks: dict[str, Any] = {"env": env}
    checks.update(sources.gateway(env))
    checks["tables"] = sources.table_status(env)
    checks["errors_5xx_15m"] = sources.recent_5xx(env)
    checks["fault_flag"] = sources.flags(env).get("SIMULATE_CHARGER_HARDWARE_FAULT", False)
    verdict, problems = health_verdict(checks)
    checks["verdict"] = verdict
    checks["problems"] = problems
    sources.put_metric(env, "EnvironmentHealthy", 1 if verdict == "OK" else 0)
    get_run_logger().info("%s → %s %s", env, verdict, "; ".join(problems))
    return checks


@flow(name="aegisvolt-salud-ambientes", log_prints=True)
def environment_health(envs: list[str] = ENVS):  # noqa: B006 — Prefect serializa el default
    results = [f.result() for f in check_environment.map(envs)]
    rows = [
        {
            "ambiente": r["env"],
            "estado": f"{ICON[r['verdict']]} {r['verdict']}",
            "versión": r.get("version") or "—",
            "latencia_ms": r.get("latency_ms", "—"),
            "servicios_ok": f"{sum(r['services'].values())}/{len(r['services'])}",
            "tablas_ok": f"{sum(v == 'ACTIVE' for v in r['tables'].values())}/{len(r['tables'])}",
            "errores_5xx_15m": r["errors_5xx_15m"],
            "flag_falla": "ON" if r["fault_flag"] else "off",
            "problemas": "; ".join(r["problems"]) or "—",
        }
        for r in results
    ]
    create_table_artifact(
        key="salud-ambientes",
        table=rows,
        description="Salud de dev, test y prod: gateway, microservicios, tablas y errores 5xx",
    )
    down = [r["env"] for r in results if r["verdict"] == "DOWN"]
    degraded = [r["env"] for r in results if r["verdict"] == "DEGRADED"]
    if down:
        return Failed(message=f"Ambientes caídos: {', '.join(down)}")
    message = f"Degradados: {', '.join(degraded)}" if degraded else "Los 3 ambientes OK"
    return Completed(message=message, data=rows)


# --- Auditoría de la SAGA -----------------------------------------------------------------
@task(name="leer-tablas", retries=2, retry_delay_seconds=3)
def load_snapshot(env: str):
    return sources.snapshot(env)


@task(name="auditar-saga")
def audit_environment(env: str, snap, stuck_after_seconds: int) -> dict[str, Any]:
    findings = audit(snap, stuck_after_s=stuck_after_seconds)
    summary = summarize(snap, findings)
    problems = [f for f in findings if f.severity != "INFO"]
    sources.put_metric(env, "SagaInconsistencies", len(problems))
    create_table_artifact(
        key=f"auditoria-saga-{env}",
        table=[f.as_dict() for f in findings] or [{"resultado": "sin hallazgos"}],
        description=f"Hallazgos de la auditoría de SAGA en {env}",
    )
    logger = get_run_logger()
    for f in problems:
        logger.warning("[%s] %s %s: %s", env, f.severity, f.rule, f.detail)
    return {"env": env, "summary": summary, "findings": [f.as_dict() for f in findings]}


@task(name="remediar-compensacion", retries=1, retry_delay_seconds=5)
def remediate(env: str, saga_id: str) -> str:
    """Reintenta las compensaciones: seguro porque son idempotentes (pregunta 6.3)."""
    result = sources.retry_compensation(env, saga_id)
    get_run_logger().info("[%s] retryCompensation(%s) → %s", env, saga_id, result)
    return result


@flow(name="aegisvolt-auditoria-saga", log_prints=True)
def saga_audit(
    envs: list[str] = ENVS,  # noqa: B006
    stuck_after_seconds: int = 120,
    remediate_failed: bool = False,
):
    reports = []
    remediations = []
    for env in envs:
        report = audit_environment(env, load_snapshot(env), stuck_after_seconds)
        reports.append(report)
        if remediate_failed:
            for f in report["findings"]:
                if f["rule"] in ("compensacion_fallida", "cobro_sin_reembolso"):
                    remediations.append((env, f["saga_id"], remediate(env, f["saga_id"])))

    lines = [
        "# Auditoría de consistencia de la SAGA",
        "",
        "| Ambiente | Sagas | Por estado | Compensadas | CRITICAL | WARNING | Consistente |",
        "| --- | ---: | --- | ---: | ---: | ---: | :---: |",
    ]
    for r in reports:
        s = r["summary"]
        estados = ", ".join(f"{k}={v}" for k, v in s["por_estado"].items()) or "—"
        lines.append(
            f"| {r['env']} | {s['sagas']} | {estados} | {s['compensadas']} | "
            f"{s['critical']} | {s['warning']} | {'✅' if s['consistente'] else '❌'} |"
        )
    if remediations:
        lines += [
            "",
            "## Remediación",
            "",
            "| Ambiente | Saga | Resultado |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {e} | `{sid}` | {res} |" for e, sid, res in remediations]
    lines += [
        "",
        "Reglas: `cobro_sin_reembolso`, `compensacion_fallida`, `carga_sin_sesion`, "
        "`sesion_activa_huerfana`, `cobro_huerfano` (CRITICAL) · `saga_atascada`, "
        "`conector_retenido`, `reserva_huerfana`, `conector_no_marcado` (WARNING) · "
        "`saga_compensada` (INFO).",
    ]
    create_markdown_artifact(
        key="auditoria-saga-resumen", markdown="\n".join(lines), description="Resumen"
    )

    critical = sum(r["summary"]["critical"] for r in reports)
    if critical and not remediations:
        return Failed(message=f"{critical} hallazgos CRITICAL en la consistencia de la SAGA")
    return Completed(message=f"Auditoría OK ({critical} críticos)", data=reports)
