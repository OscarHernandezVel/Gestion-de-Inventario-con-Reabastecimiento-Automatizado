"""Capa de datos con un cursor falso: el SQL de la transacción sale en el orden correcto y el
fallo a mitad termina en ROLLBACK sin INSERT de la orden ni notificación."""
import os
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("AWS_REGION", "us-east-1")

from inventario.domain import NotFound  # noqa: E402
from inventario.mysql_repositories import MySqlUnitOfWork  # noqa: E402
from inventario.services import PurchaseOrderService  # noqa: E402

PRODUCT = {"id": 2, "sku": "SKU-002", "name": "Tuerca", "stock": 35, "threshold": 80, "reorder_qty": 200,
           "pending_orders": 1, "category_id": 4, "lead_time_hours": 30, "history_head": 2}
REQUEST = {"id": "req-1", "product_id": 2, "qty": 200, "source": "MANUAL", "status": "QUEUED"}
READINGS = [{"product_id": 2, "slot": s, "stock": 40 + s, "read_at": datetime(2026, 11, 17, 8 + s % 10)} for s in (0, 1)]


class FakeConn:
    def __init__(self, request=REQUEST):
        self.log, self.request = [], request

    def cursor(self):
        return FakeCursor(self)


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.lastrowid, self._rows = conn, None, []

    def execute(self, sql, params=None):
        self.conn.log.append(sql)
        if sql.startswith("SELECT id, product_id, qty, source, status FROM restock_requests"):
            self._rows = [self.conn.request] if self.conn.request else []
        elif sql.startswith("SELECT id, sku"):
            self._rows = [PRODUCT] if params == (2,) or "ORDER BY" in sql else []
        elif sql.startswith("SELECT product_id, slot"):
            self._rows = READINGS
        elif sql.startswith("INSERT INTO purchase_orders"):
            self.lastrowid = 42

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class MySqlUnitOfWorkTest(unittest.TestCase):
    def service(self, conn, notifier):
        return PurchaseOrderService(lambda: MySqlUnitOfWork(lambda: conn), notifier)

    def test_commit_con_notificacion_antes_del_commit(self):
        conn, notifier = FakeConn(), mock.Mock()
        notifier.notify.side_effect = lambda *a: conn.log.append("NOTIFY")
        order = self.service(conn, notifier).create_from_request("req-1")
        self.assertEqual(order.id, 42)
        self.assertEqual(conn.log[0], "START TRANSACTION")
        locks = [s for s in conn.log if s.endswith("FOR UPDATE")]
        self.assertEqual([s.split(" FROM ")[1].split()[0] for s in locks], ["restock_requests", "products"])
        self.assertEqual(conn.log[-2:], ["NOTIFY", "COMMIT"])
        writes = [" ".join(s.split()[:3]) for s in conn.log if s.startswith(("UPDATE", "INSERT"))]
        self.assertEqual(writes, ["UPDATE products SET", "INSERT INTO purchase_orders", "UPDATE restock_requests SET"])

    def test_fallo_a_mitad_hace_rollback(self):
        conn, notifier = FakeConn(), mock.Mock()
        with self.assertRaises(RuntimeError):
            self.service(conn, notifier).create_from_request("req-1", fail_midway=True)
        self.assertEqual(conn.log[-1], "ROLLBACK")
        self.assertNotIn("COMMIT", conn.log)
        self.assertFalse(any(s.startswith("INSERT INTO purchase_orders") for s in conn.log))
        notifier.notify.assert_not_called()

    def test_solicitud_ya_procesada_no_escribe(self):
        conn = FakeConn(request=None)
        self.assertIsNone(self.service(conn, mock.Mock()).create_from_request("req-1"))
        self.assertFalse(any(s.startswith(("UPDATE", "INSERT")) for s in conn.log))
        self.assertEqual(conn.log[-1], "COMMIT")

    def test_reconstruye_el_buffer_circular_desde_las_ranuras(self):
        conn = FakeConn()
        with MySqlUnitOfWork(lambda: conn) as uow:
            p = uow.products.get(2)
            self.assertEqual((p.history.head, [r.stock for r in p.history]), (2, [40, 41]))
            uow.products.append_reading(p, p.record_reading(datetime(2026, 11, 17, 10)), None)
        self.assertTrue(conn.log[-3].startswith("INSERT INTO stock_readings"))
        self.assertTrue(conn.log[-2].startswith("UPDATE products SET history_head"))

    def test_producto_inexistente(self):
        with self.assertRaises(NotFound), MySqlUnitOfWork(lambda: FakeConn()) as uow:
            uow.products.get(3)


if __name__ == "__main__":
    unittest.main()
