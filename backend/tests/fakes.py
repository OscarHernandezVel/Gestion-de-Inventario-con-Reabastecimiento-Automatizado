"""Implementaciones en memoria de los puertos. La UoW copia el estado al entrar y lo restaura
si hay excepción, igual que un ROLLBACK: así las pruebas de servicio verifican atomicidad real."""
import copy
from datetime import datetime

from inventario.domain import Category, NotFound, Product, PurchaseOrder, RequestStatus, RestockRequest


class State:
    def __init__(self):
        self.products: dict[int, Product] = {}
        self.categories: list[Category] = []
        self.requests: dict[str, RestockRequest] = {}
        self.orders: dict[int, PurchaseOrder] = {}
        self.insights: list[dict] = []
        self.commits = 0
        self.rollbacks = 0


class _Products:
    def __init__(self, s):
        self.s = s

    def get(self, product_id, *, for_update=False):
        if product_id not in self.s.products:
            raise NotFound(f"Producto {product_id} no existe")
        return self.s.products[product_id]

    def list_all(self, *, with_history=True):
        return sorted(self.s.products.values(), key=lambda p: p.sku)

    def find_needing_restock(self):
        return [p.id for p in self.s.products.values() if p.is_low() and p.pending_orders == 0]

    def save(self, product):
        self.s.products[product.id] = product

    def append_reading(self, product, slot, at):
        pass  # el buffer ya quedó en la entidad


class _Categories:
    def __init__(self, s):
        self.s = s

    def list_all(self):
        return list(self.s.categories)


class _Requests:
    def __init__(self, s):
        self.s = s

    def add(self, request):
        self.s.requests[request.id] = request

    def get_queued(self, request_id, *, for_update=False):
        r = self.s.requests.get(request_id)
        return r if r and r.status is RequestStatus.QUEUED else None

    def save(self, request):
        self.s.requests[request.id] = request


class _Orders:
    def __init__(self, s):
        self.s = s

    def add(self, order):
        order_id = len(self.s.orders) + 1000
        self.s.orders[order_id] = order
        return order_id

    def get(self, order_id, *, for_update=False):
        if order_id not in self.s.orders:
            raise NotFound(f"Orden {order_id} no existe")
        return self.s.orders[order_id]

    def save(self, order):
        self.s.orders[order.id] = order

    def list_recent(self, limit):
        return [{"po_id": k, "qty": o.qty, "status": o.status.value} for k, o in list(self.s.orders.items())[:limit]]


class _Insights:
    def __init__(self, s):
        self.s = s

    def list_recent(self, limit):
        return self.s.insights[-limit:][::-1]

    def add(self, severity, summary):
        self.s.insights.append({"severity": severity, "summary": summary})


class FakeUnitOfWork:
    def __init__(self, state: State):
        self.state = state

    def __call__(self):
        return self

    def __enter__(self):
        self._snapshot = copy.deepcopy(self.state.__dict__)
        s = self.state
        self.products, self.categories = _Products(s), _Categories(s)
        self.requests, self.orders, self.insights = _Requests(s), _Orders(s), _Insights(s)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.state.commits += 1
        else:
            commits, rollbacks = self.state.commits, self.state.rollbacks
            self.state.__dict__.update(self._snapshot)
            self.state.commits, self.state.rollbacks = commits, rollbacks + 1


class FakeQueue:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, request):
        if self.fail:
            raise RuntimeError("SQS caído")
        self.sent.append(request)


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, order, product):
        self.sent.append((order, product.sku))


def seeded_state() -> State:
    s = State()
    s.categories = [Category(1, "Bodega", None), Category(2, "Fijaciones", 1), Category(3, "Tornillería", 2),
                    Category(4, "Tuercas y arandelas", 2), Category(5, "Herramientas", 1), Category(6, "Brocas", 5)]
    for p in [Product(1, "SKU-001", "Tornillo 3/8", 420, 100, 300, 0, 3),
              Product(2, "SKU-002", "Tuerca hexagonal", 35, 80, 200, 0, 4),
              Product(3, "SKU-003", "Arandela plana", 210, 150, 250, 0, 4),
              Product(4, "SKU-004", "Broca 6mm", 12, 20, 50, 1, 6)]:
        s.products[p.id] = p
    return s


TUESDAY_10 = datetime(2026, 11, 17, 10, 0)
SATURDAY_21 = datetime(2026, 11, 21, 21, 13)
