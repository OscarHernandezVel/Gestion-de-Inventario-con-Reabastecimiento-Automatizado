"""API HTTP del frontend (corre en EC2 detrás de NGINX, 127.0.0.1:8080). Controlador delgado.

Cada ruta: valida formato (DTO) → llama a UN método de servicio → elige el código HTTP.
Los errores de dominio se traducen en un solo lugar (`dto.status_for`).

ponytail: stdlib ThreadingHTTPServer; basta para un frontend interno. Pasar a FastAPI/gunicorn
si se necesita autenticación, OpenAPI o más throughput.
"""
import json
import logging
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3

from . import container, dto
from .domain import Source

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("api")
MAX_BODY = 10_000
_lambda = None


def _agent(body):
    global _lambda
    _lambda = _lambda or boto3.client("lambda", region_name=container.db.REGION)
    resp = _lambda.invoke(FunctionName=os.environ["AGENT_FUNCTION"],
                          Payload=json.dumps({"question": str(body.get("question", ""))[:2000]}))
    result = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        log.error(json.dumps({"agent_error": result}))
        return 503, {"error": "El agente no está disponible"}
    return (400 if "error" in result else 200), result


def _restock(body, product_id):
    ticket = container.restock_service(True).request_restock(int(product_id), Source.MANUAL, dto.qty_from(body))
    msg = "Solicitud encolada" if ticket.processing_now else \
        f"Solicitud encolada; se procesará el {ticket.next_processing:%d/%m a las %H:%M}"
    return 202, {"request_id": ticket.request.id, "qty": ticket.request.qty, "message": msg}


def _dispatch(body, product_id):
    return 200, dto.product_dto(container.inventory_service(True).dispatch(int(product_id), dto.qty_from(body)))


def _receive(body, order_id):
    return 200, dto.order_dto(container.purchase_order_service(True).receive(int(order_id)))


def _limit(query, default):
    try:
        return int(query.get("limit", [default])[0])
    except ValueError:
        raise dto.BadRequest("limit debe ser un número") from None


ROUTES = [
    ("GET", r"/api/health", lambda q: (200, {"ok": True, "products": len(container.inventory_service(True).list_products())})),
    ("GET", r"/api/products", lambda q: (200, [dto.product_dto(p) for p in container.inventory_service(True).list_products()])),
    ("GET", r"/api/categories/tree", lambda q: (200, dto.tree_dto(container.inventory_service(True).category_tree(),
                                                                  include_products=q.get("products", ["1"])[0] != "0"))),
    ("GET", r"/api/orders", lambda q: (200, container.inventory_service(True).list_orders(_limit(q, 20)))),
    ("GET", r"/api/insights", lambda q: (200, container.insight_service(True).list_recent(_limit(q, 10)))),
    ("POST", r"/api/products/(\d+)/restock", _restock),
    ("POST", r"/api/products/(\d+)/dispatch", _dispatch),
    ("POST", r"/api/orders/(\d+)/receive", _receive),
    ("POST", r"/api/agent", _agent),
]
ROUTES = [(m, re.compile(f"^{p}$"), fn) for m, p, fn in ROUTES]


def route(method, path, query, body):
    """Devuelve (status, cuerpo). Separado del servidor para probarlo sin sockets."""
    for m, pattern, fn in ROUTES:
        if m == method and (match := pattern.match(path)):
            try:
                return fn(query) if method == "GET" else fn(body, *match.groups())
            except Exception as e:  # noqa: BLE001 — manejador global
                if (code := dto.status_for(e)) is not None:
                    return code, {"error": str(e)}
                log.exception("%s %s", method, path)
                return 503, {"error": "Servicio no disponible, reintente"}
    return 404, {"error": "No encontrado"}


class Handler(BaseHTTPRequestHandler):
    server_version = "inventario"

    def _handle(self, method):
        rid, t0 = self.headers.get("X-Request-Id") or str(uuid.uuid4()), time.perf_counter()
        url = urlparse(self.path)
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "Cuerpo demasiado grande"}, rid, t0, url.path)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "JSON inválido"}, rid, t0, url.path)
        status, payload = route(method, url.path, parse_qs(url.query), body)
        self._send(status, payload, rid, t0, url.path)

    def _send(self, status, body, rid, t0, path):
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", rid)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        log.info(json.dumps({"request_id": rid, "method": self.command, "path": path, "status": status,
                             "ms": round((time.perf_counter() - t0) * 1000, 1)}))

    def log_message(self, *args):  # el log estructurado de _send lo reemplaza
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
