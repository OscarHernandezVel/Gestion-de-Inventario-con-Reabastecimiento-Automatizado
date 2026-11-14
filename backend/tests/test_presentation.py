"""Controladores: rutas HTTP, traducción de errores a códigos y fallo parcial de lotes SQS."""
import json
import os
import unittest
from unittest import mock

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("QUEUE_URL", "https://sqs.local/q")

from inventario import api, handlers  # noqa: E402
from inventario.domain import BusinessCalendar, Source  # noqa: E402
from inventario.services import InventoryService, PurchaseOrderService, RestockService  # noqa: E402
from tests.fakes import SATURDAY_21, TUESDAY_10, FakeNotifier, FakeQueue, FakeUnitOfWork, seeded_state  # noqa: E402


class FakeContainer:
    """Sustituye la raíz de composición: mismos servicios reales, puertos en memoria."""

    def __init__(self, now=TUESDAY_10):
        self.state, self.queue, self.notifier = seeded_state(), FakeQueue(), FakeNotifier()
        uow = FakeUnitOfWork(self.state)
        self._inv = InventoryService(uow, lambda: now)
        self._rs = RestockService(uow, self.queue, BusinessCalendar(), lambda: now)
        self._po = PurchaseOrderService(uow, self.notifier)

    def inventory_service(self, per_request=False):
        return self._inv

    def restock_service(self, per_request=False):
        return self._rs

    def purchase_order_service(self, per_request=False):
        return self._po


class ApiRoutesTest(unittest.TestCase):
    def call(self, method, path, body=None, query=None, now=TUESDAY_10):
        self.c = FakeContainer(now)
        with mock.patch.object(api, "container", self.c):
            return api.route(method, path, query or {}, body or {})

    def test_productos_incluyen_prediccion(self):
        status, body = self.call("GET", "/api/products")
        self.assertEqual(status, 200)
        self.assertEqual({"low", "suggested_qty", "hours_to_stockout", "history"} <= body[0].keys(), True)

    def test_arbol_de_categorias(self):
        status, body = self.call("GET", "/api/categories/tree", query={"products": ["0"]})
        self.assertEqual((status, body["name"], body["low"]), (200, "Bodega", 2))
        self.assertNotIn("items", body)
        self.assertEqual(body["children"][0]["path"], "Bodega > Fijaciones")

    def test_restock_horario_y_validacion(self):
        self.assertEqual(self.call("POST", "/api/products/2/restock", {"qty": 50})[0], 202)
        status, body = self.call("POST", "/api/products/2/restock", {"qty": 50}, now=SATURDAY_21)
        self.assertEqual((status, body["message"]), (202, "Solicitud encolada; se procesará el 23/11 a las 08:00"))
        self.assertEqual(self.call("POST", "/api/products/2/restock", {"qty": 0}), (400, {"error": "Cantidad entre 1 y 10000"}))
        self.assertEqual(self.call("POST", "/api/products/99/restock", {"qty": 5})[0], 404)

    def test_salida_sin_stock_es_conflicto(self):
        self.assertEqual(self.call("POST", "/api/products/2/dispatch", {"qty": 5})[1]["stock"], 30)
        self.assertEqual(self.call("POST", "/api/products/2/dispatch", {"qty": 500})[0], 409)

    def test_ruta_desconocida_y_error_inesperado(self):
        self.assertEqual(self.call("GET", "/api/nada")[0], 404)
        with mock.patch.object(InventoryService, "list_products", side_effect=OSError("BD caída")):
            self.assertEqual(self.call("GET", "/api/products"), (503, {"error": "Servicio no disponible, reintente"}))


class ConsumerBatchTest(unittest.TestCase):
    def run_batch(self, allow):
        """3 solicitudes reales (la 2.ª con simulate_failure) + 1 mensaje con JSON roto."""
        c = FakeContainer()
        ids = [c.restock_service().request_restock(i, Source.MANUAL, 10).request.id for i in (1, 2, 3)]
        recs = [{"messageId": f"m{i}", "body": json.dumps({"request_id": rid, "simulate_failure": i == 1})}
                for i, rid in enumerate(ids)] + [{"messageId": "bad", "body": "no-json"}]
        with mock.patch.object(handlers, "container", c), mock.patch.object(handlers, "ALLOW_FAULT_INJECTION", allow):
            return handlers.consumer_handler({"Records": recs}, None), c

    def test_solo_los_fallidos_vuelven_a_la_cola(self):
        result, c = self.run_batch(allow=True)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}, {"itemIdentifier": "bad"}]})
        self.assertEqual(len(c.state.orders), 2)
        self.assertEqual(c.state.products[2].pending_orders, 1)  # el fallido quedó como antes

    def test_sin_flag_de_entorno_no_se_inyecta_fallo(self):
        result, c = self.run_batch(allow=False)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "bad"}]})
        self.assertEqual(len(c.state.orders), 3)


if __name__ == "__main__":
    unittest.main()
