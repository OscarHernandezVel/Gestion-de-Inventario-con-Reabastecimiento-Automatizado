"""Capa de negocio: un método por caso de uso. Cada método es la frontera transaccional.

GRASP Controlador (de caso de uso) + Fabricación pura: los servicios no existen en el dominio;
orquestan repositorios y adaptadores y delegan las reglas a las entidades (Experto en información).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .domain import (BusinessCalendar, CategoryNode, InvalidInput, Product, PurchaseOrder, RestockRequest, Source,
                     build_category_tree)
from .ports import Clock, OrderNotifier, RestockQueue, UnitOfWorkFactory

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    low: list[int] = field(default_factory=list)
    queued: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class RestockTicket:
    request: RestockRequest | None
    processing_now: bool
    next_processing: datetime | None


class InventoryService:
    """Consultas del inventario, salidas de bodega y registro de lecturas para el buffer circular."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock):
        self._uow, self._clock = uow, clock

    def list_products(self) -> list[Product]:
        with self._uow() as uow:
            return uow.products.list_all()

    def category_tree(self) -> CategoryNode:
        with self._uow() as uow:
            return build_category_tree(uow.categories.list_all(), uow.products.list_all())

    def dispatch(self, product_id: int, qty: int) -> Product:
        with self._uow() as uow:
            product = uow.products.get(product_id, for_update=True)
            product.dispatch(qty)
            uow.products.save(product)
            return product

    def record_readings(self) -> int:
        """Una lectura por producto en su buffer circular (la llama la revisión horaria)."""
        now = self._clock()
        with self._uow() as uow:
            products = uow.products.list_all()
            for p in products:
                uow.products.append_reading(p, p.record_reading(now), now)
            return len(products)

    def list_orders(self, limit: int = 20) -> list[dict]:
        with self._uow() as uow:
            return uow.orders.list_recent(max(1, min(int(limit), 100)))


class RestockService:
    """Casos de uso «Revisar stock» (scheduler) y «Solicitar reabastecimiento manual»."""

    def __init__(self, uow: UnitOfWorkFactory, queue: RestockQueue, calendar: BusinessCalendar, clock: Clock):
        self._uow, self._queue, self._calendar, self._clock = uow, queue, calendar, clock

    def request_restock(self, product_id: int, source: Source, qty: int | None = None) -> RestockTicket:
        """Reserva + solicitud + SendMessage en una transacción.

        SendMessage va antes del COMMIT: si SQS falla, ROLLBACK y no queda el contador inflado;
        si el COMMIT falla después, el consumidor ignora el mensaje (la solicitud no existe).
        """
        now = self._clock()
        with self._uow() as uow:
            product = uow.products.get(product_id, for_update=True)
            if source is Source.SCHEDULER and not product.needs_auto_restock():
                return RestockTicket(None, False, None)
            request = RestockRequest.create(product, source, qty)
            uow.products.save(product)
            uow.requests.add(request)
            self._queue.send(request)
        open_now = self._calendar.is_open(now)
        return RestockTicket(request, open_now, None if open_now else self._calendar.next_opening(now))

    def scan_low_stock(self) -> ScanResult:
        """Un producto que falla no detiene a los demás: cada uno usa su propia transacción."""
        result = ScanResult()
        with self._uow() as uow:
            result.low = uow.products.find_needing_restock()
        for product_id in result.low:
            try:
                ticket = self.request_restock(product_id, Source.SCHEDULER)
                (result.queued if ticket.request else result.skipped).append(product_id)
            except Exception:
                log.exception("No se pudo encolar el producto %s", product_id)
                result.failed.append(product_id)
        return result


class PurchaseOrderService:
    """Casos de uso «Crear orden de compra» (consumidor SQS) y «Recibir mercancía»."""

    def __init__(self, uow: UnitOfWorkFactory, notifier: OrderNotifier):
        self._uow, self._notifier = uow, notifier

    def create_from_request(self, request_id: str, *, fail_midway: bool = False) -> PurchaseOrder | None:
        """Atómico: libera la reserva + crea la orden + marca ORDENADA + notifica, o nada.

        Idempotente: si la solicitud ya no está EN COLA devuelve None sin tocar nada.
        La notificación va antes del COMMIT (al menos una vez, nunca se pierde).
        """
        with self._uow() as uow:
            request = uow.requests.get_queued(request_id, for_update=True)
            if request is None:
                return None
            product = uow.products.get(request.product_id, for_update=True)
            order = PurchaseOrder.from_request(request, product)
            uow.products.save(product)  # UPDATE pending_orders - 1
            if fail_midway:
                raise RuntimeError("Fallo simulado a mitad de la transacción")
            order.id = uow.orders.add(order)
            uow.requests.save(request)
            self._notifier.notify(order, product)
            return order

    def receive(self, order_id: int) -> PurchaseOrder:
        with self._uow() as uow:
            order = uow.orders.get(order_id, for_update=True)
            product = uow.products.get(order.product_id, for_update=True)
            order.receive(product)
            uow.orders.save(order)
            uow.products.save(product)
            return order


class InsightService:
    """Alertas del agente: listar y registrar (con validación de severidad y longitud)."""

    SEVERITIES = ("INFO", "MEDIA", "ALTA")

    def __init__(self, uow: UnitOfWorkFactory):
        self._uow = uow

    def list_recent(self, limit: int = 10) -> list[dict]:
        with self._uow() as uow:
            return uow.insights.list_recent(max(1, min(int(limit), 50)))

    def record(self, severity: str, summary: str) -> None:
        if severity not in self.SEVERITIES or not 0 < len(summary.strip()) <= 500:
            raise InvalidInput("severity debe ser INFO|MEDIA|ALTA y summary 1-500 caracteres")
        with self._uow() as uow:
            uow.insights.add(severity, summary.strip())
