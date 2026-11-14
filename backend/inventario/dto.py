"""DTOs: contrato JSON de la API. Validan formato y traducen entidades ↔ JSON (GRASP Indirección).

Las reglas de negocio (p. ej. stock suficiente) no van aquí: las valida el dominio.
"""
from __future__ import annotations

from .domain import CategoryNode, DomainError, InsufficientStock, InvalidQuantity, InvalidTransition, NotFound, \
    Product, PurchaseOrder, validate_qty


class BadRequest(Exception):
    pass


def qty_from(body: dict) -> int:
    try:
        return validate_qty(body.get("qty"))
    except InvalidQuantity as e:
        raise BadRequest(str(e)) from e


def product_dto(p: Product) -> dict:
    return {
        "id": p.id, "sku": p.sku, "name": p.name, "stock": p.stock, "threshold": p.threshold,
        "reorder_qty": p.reorder_qty, "pending_orders": p.pending_orders, "category_id": p.category_id,
        "low": p.is_low(), "suggested_qty": p.suggested_qty(),
        "consumption_per_hour": round(p.history.consumption_per_hour(), 2),
        "hours_to_stockout": p.history.hours_to_stockout(p.stock),
        "history": [{"at": r.at.isoformat(), "stock": r.stock} for r in p.history],
    }


def tree_dto(node: CategoryNode, *, include_products: bool = True) -> dict:
    return {
        "id": node.id, "name": node.name, "path": " > ".join(node.path()), **node.summary(),
        "children": [tree_dto(c, include_products=include_products) for c in node.children],
        **({"items": [{"id": p.id, "sku": p.sku, "name": p.name, "stock": p.stock, "low": p.is_low()}
                      for p in node.products]} if include_products else {}),
    }


def order_dto(o: PurchaseOrder) -> dict:
    return {"po_id": o.id, "request_id": o.request_id, "product_id": o.product_id, "qty": o.qty, "status": o.status.value}


# Traducción única de errores de dominio a HTTP (manejador global, no en cada ruta).
ERROR_STATUS: list[tuple[type[Exception], int]] = [
    (BadRequest, 400), (NotFound, 404), (InvalidTransition, 409), (InsufficientStock, 409),
    (InvalidQuantity, 422), (DomainError, 422),
]


def status_for(error: Exception) -> int | None:
    return next((code for cls, code in ERROR_STATUS if isinstance(error, cls)), None)
