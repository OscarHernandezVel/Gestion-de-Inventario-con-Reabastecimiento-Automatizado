"""Puertos (interfaces) que necesita la capa de servicios. Expresados en lenguaje del dominio.

GRASP Variaciones protegidas + Indirección: los servicios dependen de estos contratos, no de MySQL,
SQS ni SNS. Cada puerto tiene dos implementaciones: la real (`mysql_repositories`, `aws_adapters`)
y la de memoria de las pruebas (`tests/fakes.py`).
"""
from __future__ import annotations

from datetime import datetime
from typing import ContextManager, Protocol

from .domain import Category, Product, PurchaseOrder, RestockRequest


class ProductRepository(Protocol):
    def get(self, product_id: int, *, for_update: bool = False) -> Product: ...
    def list_all(self, *, with_history: bool = True) -> list[Product]: ...
    def find_needing_restock(self) -> list[int]: ...
    def save(self, product: Product) -> None: ...
    def append_reading(self, product: Product, slot: int, at: datetime) -> None: ...


class CategoryRepository(Protocol):
    def list_all(self) -> list[Category]: ...


class RestockRequestRepository(Protocol):
    def add(self, request: RestockRequest) -> None: ...
    def get_queued(self, request_id: str, *, for_update: bool = False) -> RestockRequest | None: ...
    def save(self, request: RestockRequest) -> None: ...


class PurchaseOrderRepository(Protocol):
    def add(self, order: PurchaseOrder) -> int: ...
    def get(self, order_id: int, *, for_update: bool = False) -> PurchaseOrder: ...
    def save(self, order: PurchaseOrder) -> None: ...
    def list_recent(self, limit: int) -> list[dict]: ...


class InsightRepository(Protocol):
    def list_recent(self, limit: int) -> list[dict]: ...
    def add(self, severity: str, summary: str) -> None: ...


class UnitOfWork(Protocol):
    """Frontera transaccional: todos los repositorios comparten una transacción.

    `with uow:` hace START TRANSACTION; sale con COMMIT, o con ROLLBACK si hubo una excepción.
    """

    products: ProductRepository
    categories: CategoryRepository
    requests: RestockRequestRepository
    orders: PurchaseOrderRepository
    insights: InsightRepository

    def __enter__(self) -> UnitOfWork: ...
    def __exit__(self, *exc) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> ContextManager[UnitOfWork]: ...


class RestockQueue(Protocol):
    def send(self, request: RestockRequest) -> None: ...


class OrderNotifier(Protocol):
    def notify(self, order: PurchaseOrder, product: Product) -> None: ...


class Clock(Protocol):
    def __call__(self) -> datetime: ...
