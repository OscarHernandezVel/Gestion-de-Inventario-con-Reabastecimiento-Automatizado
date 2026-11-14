"""Capa de acceso a datos: implementaciones MySQL de los puertos. Solo SQL parametrizado, sin reglas.

Cada repositorio recibe el cursor de la transacción abierta por `MySqlUnitOfWork`, así todas las
escrituras de un caso de uso son atómicas (START TRANSACTION / COMMIT / ROLLBACK explícitos).
"""
from __future__ import annotations

import logging
from typing import Callable

import pymysql

from .domain import (HISTORY_SLOTS, Category, NotFound, OrderStatus, Product, PurchaseOrder, RequestStatus,
                     RestockRequest, Source, StockHistory, StockReading)

log = logging.getLogger(__name__)
PRODUCT_COLS = "id, sku, name, stock, threshold, reorder_qty, pending_orders, category_id, lead_time_hours, history_head"


class MySqlProductRepository:
    def __init__(self, cur):
        self.cur = cur

    def _histories(self, heads: dict[int, int]) -> dict[int, StockHistory]:
        if not heads:
            return {}
        marks = ", ".join(["%s"] * len(heads))
        self.cur.execute(f"SELECT product_id, slot, stock, read_at FROM stock_readings WHERE product_id IN ({marks})",
                         tuple(heads))
        slots: dict[int, dict[int, StockReading]] = {pid: {} for pid in heads}
        for r in self.cur.fetchall():
            slots[r["product_id"]][r["slot"]] = StockReading(r["read_at"], r["stock"])
        return {pid: StockHistory.from_slots(HISTORY_SLOTS, heads[pid], s) for pid, s in slots.items()}

    @staticmethod
    def _to_product(row: dict, history: StockHistory | None) -> Product:
        return Product(row["id"], row["sku"], row["name"], row["stock"], row["threshold"], row["reorder_qty"],
                       row["pending_orders"], row["category_id"], row["lead_time_hours"], history or StockHistory())

    def get(self, product_id: int, *, for_update: bool = False) -> Product:
        self.cur.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE id = %s" + (" FOR UPDATE" if for_update else ""),
                         (product_id,))
        row = self.cur.fetchone()
        if row is None:
            raise NotFound(f"Producto {product_id} no existe")
        return self._to_product(row, self._histories({row["id"]: row["history_head"]})[row["id"]])

    def list_all(self, *, with_history: bool = True) -> list[Product]:
        self.cur.execute(f"SELECT {PRODUCT_COLS} FROM products ORDER BY sku")
        rows = self.cur.fetchall()
        hist = self._histories({r["id"]: r["history_head"] for r in rows}) if with_history else {}
        return [self._to_product(r, hist.get(r["id"])) for r in rows]

    def find_needing_restock(self) -> list[int]:
        self.cur.execute("SELECT id FROM products WHERE stock < threshold AND pending_orders = 0 ORDER BY id")
        return [r["id"] for r in self.cur.fetchall()]

    def save(self, product: Product) -> None:
        self.cur.execute("UPDATE products SET stock = %s, pending_orders = %s WHERE id = %s",
                         (product.stock, product.pending_orders, product.id))

    def append_reading(self, product: Product, slot: int, at) -> None:
        """Escritura del buffer circular: sobrescribe la ranura `slot` y avanza la cabeza."""
        self.cur.execute(
            "INSERT INTO stock_readings (product_id, slot, stock, read_at) VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE stock = VALUES(stock), read_at = VALUES(read_at)",
            (product.id, slot, product.stock, at))
        self.cur.execute("UPDATE products SET history_head = %s WHERE id = %s", (product.history.head, product.id))


class MySqlCategoryRepository:
    def __init__(self, cur):
        self.cur = cur

    def list_all(self) -> list[Category]:
        self.cur.execute("SELECT id, name, parent_id FROM categories ORDER BY id")
        return [Category(r["id"], r["name"], r["parent_id"]) for r in self.cur.fetchall()]


class MySqlRestockRequestRepository:
    def __init__(self, cur):
        self.cur = cur

    def add(self, request: RestockRequest) -> None:
        self.cur.execute("INSERT INTO restock_requests (id, product_id, qty, source, status) VALUES (%s, %s, %s, %s, %s)",
                         (request.id, request.product_id, request.qty, request.source.value, request.status.value))

    def get_queued(self, request_id: str, *, for_update: bool = False) -> RestockRequest | None:
        self.cur.execute("SELECT id, product_id, qty, source, status FROM restock_requests "
                         "WHERE id = %s AND status = 'QUEUED'" + (" FOR UPDATE" if for_update else ""), (request_id,))
        r = self.cur.fetchone()
        return r and RestockRequest(r["id"], r["product_id"], r["qty"], Source(r["source"]), RequestStatus(r["status"]))

    def save(self, request: RestockRequest) -> None:
        self.cur.execute("UPDATE restock_requests SET status = %s WHERE id = %s", (request.status.value, request.id))


class MySqlPurchaseOrderRepository:
    def __init__(self, cur):
        self.cur = cur

    def add(self, order: PurchaseOrder) -> int:
        self.cur.execute("INSERT INTO purchase_orders (request_id, product_id, qty, status) VALUES (%s, %s, %s, %s)",
                         (order.request_id, order.product_id, order.qty, order.status.value))
        return self.cur.lastrowid

    def get(self, order_id: int, *, for_update: bool = False) -> PurchaseOrder:
        self.cur.execute("SELECT id, request_id, product_id, qty, status FROM purchase_orders WHERE id = %s"
                         + (" FOR UPDATE" if for_update else ""), (order_id,))
        r = self.cur.fetchone()
        if r is None:
            raise NotFound(f"Orden de compra {order_id} no existe")
        return PurchaseOrder(r["id"], r["request_id"], r["product_id"], r["qty"], OrderStatus(r["status"]))

    def save(self, order: PurchaseOrder) -> None:
        self.cur.execute("UPDATE purchase_orders SET status = %s, received_at = IF(%s = 'RECEIVED', "
                         "COALESCE(received_at, CURRENT_TIMESTAMP), received_at) WHERE id = %s",
                         (order.status.value, order.status.value, order.id))

    def list_recent(self, limit: int) -> list[dict]:
        """Solicitudes recientes con su orden (si existe): incluye las EN COLA fuera de horario."""
        self.cur.execute(
            "SELECT o.id AS po_id, p.sku, r.qty, r.source, r.status, o.status AS po_status, r.created_at "
            "FROM restock_requests r JOIN products p ON p.id = r.product_id "
            "LEFT JOIN purchase_orders o ON o.request_id = r.id ORDER BY r.created_at DESC LIMIT %s", (limit,))
        return self.cur.fetchall()


class MySqlInsightRepository:
    def __init__(self, cur):
        self.cur = cur

    def list_recent(self, limit: int) -> list[dict]:
        self.cur.execute("SELECT severity, summary, created_at FROM agent_insights ORDER BY id DESC LIMIT %s", (limit,))
        return self.cur.fetchall()

    def add(self, severity: str, summary: str) -> None:
        self.cur.execute("INSERT INTO agent_insights (severity, summary) VALUES (%s, %s)", (severity, summary))


class MySqlUnitOfWork:
    """Abre la transacción y entrega los repositorios ligados a ella.

    `close_connection=True` para la API (una conexión por petición; el Proxy hace el pooling);
    `False` para las Lambdas, que reutilizan la conexión del contenedor.
    """

    def __init__(self, conn_factory: Callable, *, close_connection: bool = False):
        self._conn_factory, self._close = conn_factory, close_connection

    def __enter__(self) -> MySqlUnitOfWork:
        self._conn = self._conn_factory()
        self._cur = self._conn.cursor()
        self._cur.execute("START TRANSACTION")
        self.products = MySqlProductRepository(self._cur)
        self.categories = MySqlCategoryRepository(self._cur)
        self.requests = MySqlRestockRequestRepository(self._cur)
        self.orders = MySqlPurchaseOrderRepository(self._cur)
        self.insights = MySqlInsightRepository(self._cur)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self._cur.execute("COMMIT")
            else:
                try:
                    self._cur.execute("ROLLBACK")
                except pymysql.Error:
                    log.warning("ROLLBACK falló; MySQL revierte al cerrar la conexión")
        finally:
            self._cur.close()
            if self._close:
                self._conn.close()
