"""Capa de dominio: entidades, objetos de valor y estructuras de datos del inventario.

No conoce HTTP, SQL, SQS ni boto3 (regla de dependencias: todas las capas conocen el dominio,
el dominio no conoce a nadie). Cada regla vive en la clase que tiene los datos (GRASP Experto).

Estructuras de datos:
- `CircularBuffer`: buffer circular (ring buffer) de capacidad fija; guarda las últimas N lecturas
  de stock de cada producto en O(1) por inserción y memoria acotada.
- `CategoryNode`: árbol de categorías (Bodega > Categoría > Subcategoría > Producto). Cada hoja
  (producto) carga su buffer circular y el árbol agrega stock, alertas y consumo hacia arriba.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Generic, Iterable, Iterator, TypeVar

MAX_QTY = 10_000
HISTORY_SLOTS = 24  # 24 lecturas horarias ≈ 2,4 días hábiles


# ---------------------------------------------------------------- errores de dominio
class DomainError(Exception):
    """Base de las reglas de negocio violadas. La presentación las traduce a códigos HTTP."""


class NotFound(DomainError):
    pass


class InvalidInput(DomainError):
    pass


class InvalidQuantity(InvalidInput):
    pass


class InsufficientStock(DomainError):
    pass


class InvalidTransition(DomainError):
    pass


class CategoryCycle(DomainError):
    """Los datos de categorías forman un ciclo: el árbol no se puede construir."""


# ---------------------------------------------------------------- buffer circular
T = TypeVar("T")


class CircularBuffer(Generic[T]):
    """Buffer circular de capacidad fija. Al llenarse, cada `append` sobrescribe el más antiguo.

    `head` es el índice absoluto de la próxima escritura; la ranura física es `head % capacity`.
    Así se persiste igual en MySQL (tabla `stock_readings` con PK (product_id, slot)).
    """

    __slots__ = ("capacity", "_slots", "head")

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity debe ser >= 1")
        self.capacity = capacity
        self._slots: list[T | None] = [None] * capacity
        self.head = 0

    @classmethod
    def from_slots(cls, capacity: int, head: int, slots: dict[int, T]) -> CircularBuffer[T]:
        buf = cls(capacity)
        for slot, value in slots.items():
            buf._slots[slot % capacity] = value
        buf.head = head
        return buf

    def append(self, value: T) -> int:
        """Escribe y devuelve la ranura física usada."""
        slot = self.head % self.capacity
        self._slots[slot] = value
        self.head += 1
        return slot

    def __len__(self) -> int:
        return min(self.head, self.capacity)

    def __iter__(self) -> Iterator[T]:
        """Del más antiguo al más reciente."""
        start = self.head - len(self)
        for i in range(start, self.head):
            yield self._slots[i % self.capacity]  # type: ignore[misc]

    def latest(self) -> T | None:
        return self._slots[(self.head - 1) % self.capacity] if self.head else None

    def oldest(self) -> T | None:
        return next(iter(self), None)


@dataclass(frozen=True)
class StockReading:
    at: datetime
    stock: int


class StockHistory(CircularBuffer[StockReading]):
    """Últimas lecturas horarias de stock de un producto y el consumo que se deduce de ellas."""

    def __init__(self, capacity: int = HISTORY_SLOTS):
        super().__init__(capacity)

    def consumption_per_hour(self) -> float:
        """Unidades que salen por hora. Ignora subidas (llegada de mercancía): solo suma bajadas."""
        readings = list(self)
        if len(readings) < 2:
            return 0.0
        hours = (readings[-1].at - readings[0].at).total_seconds() / 3600
        if hours <= 0:
            return 0.0
        used = sum(max(0, a.stock - b.stock) for a, b in zip(readings, readings[1:]))
        return used / hours

    def hours_to_stockout(self, stock: int) -> float | None:
        rate = self.consumption_per_hour()
        return round(stock / rate, 1) if rate > 0 else None


# ---------------------------------------------------------------- entidades
class Source(str, Enum):
    SCHEDULER = "SCHEDULER"
    MANUAL = "MANUAL"


class RequestStatus(str, Enum):
    QUEUED = "QUEUED"
    ORDERED = "ORDERED"


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    RECEIVED = "RECEIVED"


def validate_qty(qty: object) -> int:
    if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
        raise InvalidQuantity(f"Cantidad entre 1 y {MAX_QTY}")
    return qty


@dataclass
class Product:
    id: int
    sku: str
    name: str
    stock: int
    threshold: int
    reorder_qty: int
    pending_orders: int = 0
    category_id: int | None = None
    lead_time_hours: int = 30  # horas hábiles que tarda el proveedor (3 días de 10 h)
    history: StockHistory = field(default_factory=StockHistory, repr=False, compare=False)

    def is_low(self) -> bool:
        return self.stock < self.threshold

    def needs_auto_restock(self) -> bool:
        """El scheduler solo pide si está bajo el umbral y no hay otra solicitud en curso (no duplica)."""
        return self.is_low() and self.pending_orders == 0

    def suggested_qty(self) -> int:
        """Cubre el consumo esperado durante el tiempo de entrega y vuelve a dejar el stock sobre el umbral."""
        needed = self.history.consumption_per_hour() * self.lead_time_hours + self.threshold - self.stock
        return min(MAX_QTY, max(self.reorder_qty, math.ceil(needed)))

    def reserve_restock(self) -> None:
        self.pending_orders += 1

    def release_restock(self) -> None:
        if self.pending_orders == 0:
            raise InvalidTransition(f"{self.sku} no tiene solicitudes pendientes")
        self.pending_orders -= 1

    def dispatch(self, qty: int) -> None:
        """Salida de bodega (venta o consumo interno)."""
        validate_qty(qty)
        if qty > self.stock:
            raise InsufficientStock(f"{self.sku}: stock {self.stock}, se pidieron {qty}")
        self.stock -= qty

    def receive(self, qty: int) -> None:
        self.stock += validate_qty(qty)

    def record_reading(self, at: datetime) -> int:
        return self.history.append(StockReading(at, self.stock))


@dataclass
class RestockRequest:
    id: str
    product_id: int
    qty: int
    source: Source
    status: RequestStatus = RequestStatus.QUEUED

    @classmethod
    def create(cls, product: Product, source: Source, qty: int | None = None) -> RestockRequest:
        """Creador: el producto tiene los datos de inicialización (cantidad sugerida) y queda reservado."""
        req = cls(str(uuid.uuid4()), product.id, validate_qty(qty if qty is not None else product.suggested_qty()), source)
        product.reserve_restock()
        return req

    def mark_ordered(self) -> None:
        if self.status is not RequestStatus.QUEUED:
            raise InvalidTransition(f"Solicitud {self.id} ya está {self.status.value}")
        self.status = RequestStatus.ORDERED


@dataclass
class PurchaseOrder:
    id: int | None
    request_id: str
    product_id: int
    qty: int
    status: OrderStatus = OrderStatus.CREATED

    @classmethod
    def from_request(cls, request: RestockRequest, product: Product) -> PurchaseOrder:
        """Transición atómica en memoria: la solicitud pasa a ORDENADA y libera la reserva del producto."""
        request.mark_ordered()
        product.release_restock()
        return cls(None, request.id, request.product_id, request.qty)

    def receive(self, product: Product) -> None:
        if self.status is OrderStatus.RECEIVED:
            raise InvalidTransition(f"La orden {self.id} ya fue recibida")
        product.receive(self.qty)
        self.status = OrderStatus.RECEIVED


# ---------------------------------------------------------------- árbol de categorías
@dataclass(frozen=True)
class Category:
    id: int
    name: str
    parent_id: int | None


@dataclass
class CategoryNode:
    """Nodo del árbol. Composición con sus hijos (viven y mueren con el árbol); agregación con los
    productos, que existen fuera del árbol."""

    id: int
    name: str
    parent: CategoryNode | None = field(default=None, repr=False)
    children: list[CategoryNode] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)

    def add_child(self, child: CategoryNode) -> None:
        child.parent = self
        self.children.append(child)

    def walk(self) -> Iterator[CategoryNode]:
        """Recorrido en preorden (iterativo: no depende del límite de recursión)."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def all_products(self) -> Iterator[Product]:
        for node in self.walk():
            yield from node.products

    def path(self) -> list[str]:
        node, names = self, []
        while node:
            names.append(node.name)
            node = node.parent
        return names[::-1]

    def find(self, category_id: int) -> CategoryNode | None:
        return next((n for n in self.walk() if n.id == category_id), None)

    def summary(self) -> dict:
        """Agregados del subárbol: stock total, productos bajo umbral y consumo por hora."""
        products = list(self.all_products())
        return {
            "products": len(products),
            "stock": sum(p.stock for p in products),
            "low": sum(p.is_low() for p in products),
            "pending_orders": sum(p.pending_orders for p in products),
            "consumption_per_hour": round(sum(p.history.consumption_per_hour() for p in products), 2),
        }


def build_category_tree(categories: Iterable[Category], products: Iterable[Product]) -> CategoryNode:
    """Construye el árbol en O(n). Falla si hay más de una raíz, padres inexistentes o ciclos."""
    cats = list(categories)
    nodes = {c.id: CategoryNode(c.id, c.name) for c in cats}
    roots = []
    for c in cats:
        if c.parent_id is None:
            roots.append(nodes[c.id])
        elif c.parent_id not in nodes:
            raise CategoryCycle(f"Categoría {c.id} apunta a un padre inexistente {c.parent_id}")
        else:
            nodes[c.parent_id].add_child(nodes[c.id])
    if len(roots) != 1:
        raise CategoryCycle(f"Se esperaba 1 raíz y hay {len(roots)}")
    reachable = sum(1 for _ in roots[0].walk())
    if reachable != len(nodes):  # los nodos de un ciclo nunca cuelgan de la raíz
        raise CategoryCycle(f"{len(nodes) - reachable} categorías forman un ciclo")
    for p in products:
        if p.category_id in nodes:
            nodes[p.category_id].products.append(p)
        else:
            roots[0].products.append(p)  # sin categoría: cuelga de la raíz para no perderlo
    return roots[0]


# ---------------------------------------------------------------- calendario hábil
@dataclass(frozen=True)
class BusinessCalendar:
    """Lunes a viernes, de `open_hour` a `close_hour` (hora local de la bodega)."""

    open_hour: int = 8
    close_hour: int = 18

    def is_open(self, now: datetime) -> bool:
        return now.weekday() < 5 and self.open_hour <= now.hour < self.close_hour

    def next_opening(self, now: datetime) -> datetime:
        day = now.replace(hour=self.open_hour, minute=0, second=0, microsecond=0)
        if now >= day:
            day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day
