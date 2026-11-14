"""Carga concurrente sobre la Lambda bench y reporte de métricas CloudWatch con y sin Proxy.

Uso:
  python scripts/load_test.py run --mode proxy  --concurrency 100 --rounds 3
  python scripts/load_test.py run --mode direct --concurrency 100 --rounds 3   # requiere enable_direct_benchmark=true
  python scripts/load_test.py report --minutes 30
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

PROJECT = "inventario"
lam = boto3.client("lambda", config=Config(max_pool_connections=200, read_timeout=90))
cw = boto3.client("cloudwatch")


def invoke(mode):
    resp = lam.invoke(FunctionName=f"{PROJECT}-bench", Payload=json.dumps({"mode": mode, "queries": 10}))
    return json.loads(resp["Payload"].read())


def run(mode, concurrency, rounds):
    for r in range(rounds):
        with ThreadPoolExecutor(concurrency) as pool:
            results = list(pool.map(invoke, [mode] * concurrency))
        errors = [x for x in results if "error" in x or "errorMessage" in x]
        ok = [x for x in results if "query_ms" in x]
        avg = sum(x["query_ms"] for x in ok) / len(ok) if ok else 0
        print(f"[{mode}] ronda {r + 1}: ok={len(ok)} errores={len(errors)} query_ms_prom={avg:.1f}")
        if errors:
            print("  ejemplo de error:", (errors[0].get("error") or errors[0].get("errorMessage"))[:120])


def stat(namespace, metric, dims, statistic, minutes):
    end = datetime.now(timezone.utc)
    points = cw.get_metric_statistics(
        Namespace=namespace, MetricName=metric, Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=end - timedelta(minutes=minutes), EndTime=end, Period=60, Statistics=[statistic],
    )["Datapoints"]
    return max((p[statistic] for p in points), default=None)


def report(minutes):
    rows = [
        ("RDS DatabaseConnections (instancia, máx.)", "AWS/RDS", "DatabaseConnections", {"DBInstanceIdentifier": PROJECT}, "Maximum"),
        ("Proxy ClientConnections (máx.)", "AWS/RDS", "ClientConnections", {"ProxyName": PROJECT}, "Maximum"),
        ("Proxy DatabaseConnections (máx.)", "AWS/RDS", "DatabaseConnections", {"ProxyName": PROJECT}, "Maximum"),
        ("Proxy QueryDatabaseResponseLatency (µs, prom.)", "AWS/RDS", "QueryDatabaseResponseLatency", {"ProxyName": PROJECT}, "Average"),
        ("QueryDuration proxy (ms, máx. de promedios)", "Inventario/Bench", "QueryDuration", {"Mode": "proxy"}, "Average"),
        ("QueryDuration direct (ms, máx. de promedios)", "Inventario/Bench", "QueryDuration", {"Mode": "direct"}, "Average"),
        ("ConnectionErrors proxy (suma/min máx.)", "Inventario/Bench", "ConnectionErrors", {"Mode": "proxy"}, "Sum"),
        ("ConnectionErrors direct (suma/min máx.)", "Inventario/Bench", "ConnectionErrors", {"Mode": "direct"}, "Sum"),
    ]
    print(f"Métricas de los últimos {minutes} min")
    for label, ns, metric, dims, st in rows:
        v = stat(ns, metric, dims, st, minutes)
        print(f"  {label:<48} {'—' if v is None else round(v, 1)}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--mode", choices=["proxy", "direct"], required=True)
    r.add_argument("--concurrency", type=int, default=100)
    r.add_argument("--rounds", type=int, default=3)
    rep = sub.add_parser("report")
    rep.add_argument("--minutes", type=int, default=30)
    a = p.parse_args()
    run(a.mode, a.concurrency, a.rounds) if a.cmd == "run" else report(a.minutes)
