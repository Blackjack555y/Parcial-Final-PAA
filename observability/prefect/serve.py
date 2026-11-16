"""Registra los flujos como deployments con horario y los ejecuta (proceso de larga vida).

    python serve.py        (el contenedor prefect-flows lo ejecuta como CMD)

Desde la UI (http://127.0.0.1:4200) o la CLI se pueden lanzar a demanda:
    prefect deployment run 'aegisvolt-salud-ambientes/cada-5-min'
    prefect deployment run 'aegisvolt-auditoria-saga/con-remediacion'
"""

from prefect import serve

from aegis_flows.flows import environment_health, saga_audit

if __name__ == "__main__":
    serve(
        environment_health.to_deployment(
            name="cada-5-min", cron="*/5 * * * *", tags=["aegisvolt", "salud"]
        ),
        saga_audit.to_deployment(
            name="cada-10-min", cron="*/10 * * * *", tags=["aegisvolt", "saga"]
        ),
        saga_audit.to_deployment(  # solo a demanda: reintenta compensaciones fallidas
            name="con-remediacion",
            parameters={"remediate_failed": True},
            tags=["aegisvolt", "saga", "remediacion"],
        ),
    )
