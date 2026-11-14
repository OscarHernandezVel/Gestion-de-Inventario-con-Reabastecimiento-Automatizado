"""Controladores Lambda (presentación): traducen el evento AWS a una llamada de servicio y nada más.

checker  ← EventBridge Scheduler (L-V 8-17 h): lecturas al buffer circular + revisión de stock.
consumer ← SQS (batch 10, ReportBatchItemFailures): crea órdenes de compra.
bench    ← prueba de carga con / sin RDS Proxy.
migrate  ← despliegue: aplica schema.sql y crea los usuarios de aplicación.
"""
import json
import logging
import os
import time
from pathlib import Path

import boto3
import pymysql

from . import container, db

log = logging.getLogger()
log.setLevel(logging.INFO)

ALLOW_FAULT_INJECTION = os.environ.get("ALLOW_FAULT_INJECTION") == "true"


def emf(namespace, dimensions, **metrics):
    """Embedded Metric Format: CloudWatch convierte esta línea de log en métricas, sin llamadas a la API."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": namespace, "Dimensions": [list(dimensions)],
            "Metrics": [{"Name": k, "Unit": "Count" if not k.endswith("Duration") else "Milliseconds"} for k in metrics],
        }]},
        **dimensions, **metrics,
    }))


def checker_handler(event, context):
    readings = container.inventory_service().record_readings()
    result = container.restock_service().scan_low_stock()
    log.info(json.dumps({"readings": readings, **result.__dict__}))
    emf("Inventario/Restock", {"Function": "checker"}, LowStock=len(result.low),
        Enqueued=len(result.queued), EnqueueFailed=len(result.failed))
    if result.failed:
        raise RuntimeError(f"Fallaron {len(result.failed)} productos")  # visible en la métrica Errors y su alarma
    return {"readings": readings, "low_stock": len(result.low), "queued": len(result.queued)}


def consumer_handler(event, context):
    """Solo los mensajes fallidos vuelven a la cola. Si falla la conexión, se reintenta todo el lote."""
    service = container.purchase_order_service()
    failures = []
    for record in event["Records"]:
        try:
            msg = json.loads(record["body"])
            fail = ALLOW_FAULT_INJECTION and bool(msg.get("simulate_failure"))
            order = service.create_from_request(msg["request_id"], fail_midway=fail)
            log.info(json.dumps({"message_id": record["messageId"], "request_id": msg["request_id"],
                                 "po_id": order and order.id, "duplicate": order is None}))
        except Exception:
            log.exception("Mensaje %s falló", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    emf("Inventario/Restock", {"Function": "consumer"}, Processed=len(event["Records"]) - len(failures),
        BatchItemFailures=len(failures))
    return {"batchItemFailures": failures}


# ---------------------------------------------------------------- benchmark Proxy vs directo
_direct = None
_secrets = None


def _direct_conn():
    global _direct, _secrets
    if _direct is None:
        _secrets = _secrets or boto3.client("secretsmanager", region_name=db.REGION)
        pwd = json.loads(_secrets.get_secret_value(SecretId=os.environ["APP_USER_SECRET_ARN"])["SecretString"])["password"]
        _direct = db.connect(host=os.environ["DB_INSTANCE_ENDPOINT"], password=pwd, verify_tls=False)
    return _direct


def bench_handler(event, context):
    """Simula una Lambda de negocio bajo carga: conecta (proxy|direct) y ejecuta N consultas."""
    mode = event.get("mode", "proxy")
    t0 = time.perf_counter()
    try:
        conn = _direct_conn() if mode == "direct" else db.get_conn()
    except pymysql.Error as e:  # ej. 1040 Too many connections en modo direct
        emf("Inventario/Bench", {"Mode": mode}, ConnectionErrors=1)
        return {"mode": mode, "error": str(e)}
    connect_ms = (time.perf_counter() - t0) * 1000
    durations = []
    with conn.cursor() as cur:
        for _ in range(int(event.get("queries", 10))):
            t = time.perf_counter()
            cur.execute("SELECT SLEEP(%s) AS s, COUNT(*) AS n FROM purchase_orders", (float(event.get("sleep", 0.05)),))
            cur.fetchall()
            durations.append((time.perf_counter() - t) * 1000)
    query_ms = sum(durations) / len(durations)
    emf("Inventario/Bench", {"Mode": mode}, ConnectDuration=connect_ms, QueryDuration=query_ms, ConnectionErrors=0)
    return {"mode": mode, "connect_ms": round(connect_ms, 1), "query_ms": round(query_ms, 1)}


# ---------------------------------------------------------------- migración
USERS = {
    "app_user": ["GRANT SELECT, INSERT, UPDATE ON inventory.* TO 'app_user'@'%'"],
    "agent_ro": ["GRANT SELECT ON inventory.* TO 'agent_ro'@'%'",
                 "GRANT INSERT ON inventory.agent_insights TO 'agent_ro'@'%'"],
}


def migrate_handler(event, context):
    """Aplica schema.sql y crea los usuarios de aplicación. Se conecta como admin vía Proxy (IAM)."""
    for attempt in range(20):  # el target del Proxy tarda unos minutos en quedar disponible
        try:
            conn = db.connect(user=os.environ["DB_ADMIN_USER"])
            break
        except pymysql.Error as e:
            log.info("Proxy aún no disponible (%s), intento %d", e, attempt)
            time.sleep(15)
    else:
        raise RuntimeError("No se pudo conectar al Proxy")
    secrets = boto3.client("secretsmanager", region_name=db.REGION)
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in filter(None, (s.strip() for s in schema.split(";"))):
            cur.execute(stmt)
        for user, grants in USERS.items():
            arn = os.environ[f"{user.upper()}_SECRET_ARN"]
            pwd = json.loads(secrets.get_secret_value(SecretId=arn)["SecretString"])["password"]
            # mysql_native_password: el plugin que RDS Proxy usa hacia MySQL 8.0
            cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED WITH mysql_native_password BY %s", (user, pwd))
            cur.execute("ALTER USER %s@'%%' IDENTIFIED WITH mysql_native_password BY %s", (user, pwd))
            for g in grants:
                cur.execute(g)
    conn.close()
    return {"ok": True}
