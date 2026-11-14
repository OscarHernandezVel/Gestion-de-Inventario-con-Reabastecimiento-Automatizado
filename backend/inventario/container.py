"""Raíz de composición: único lugar que conoce las clases concretas y las conecta (inyección por constructor).

Los clientes de boto3 y los servicios se crean una sola vez por contenedor Lambda / proceso.
"""
import os
from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo

import boto3

from . import db
from .aws_adapters import SnsOrderNotifier, SqsRestockQueue
from .domain import BusinessCalendar
from .mysql_repositories import MySqlUnitOfWork
from .services import InsightService, InventoryService, PurchaseOrderService, RestockService

TZ = ZoneInfo(os.environ.get("BUSINESS_TZ", "America/Bogota"))
CALENDAR = BusinessCalendar()


def clock() -> datetime:
    """Hora local de la bodega sin zona (MySQL TIMESTAMP la guarda tal cual)."""
    return datetime.now(TZ).replace(tzinfo=None)


def lambda_uow():
    return MySqlUnitOfWork(db.get_conn)


def request_uow():
    return MySqlUnitOfWork(db.connect, close_connection=True)


@cache
def inventory_service(per_request: bool = False) -> InventoryService:
    return InventoryService(request_uow if per_request else lambda_uow, clock)


@cache
def restock_service(per_request: bool = False) -> RestockService:
    queue = SqsRestockQueue(boto3.client("sqs", region_name=db.REGION), os.environ["QUEUE_URL"])
    return RestockService(request_uow if per_request else lambda_uow, queue, CALENDAR, clock)


@cache
def purchase_order_service(per_request: bool = False) -> PurchaseOrderService:
    topic = os.environ.get("TOPIC_ARN", "")
    notifier = SnsOrderNotifier(boto3.client("sns", region_name=db.REGION), topic)
    return PurchaseOrderService(request_uow if per_request else lambda_uow, notifier)


@cache
def insight_service(per_request: bool = False) -> InsightService:
    return InsightService(request_uow if per_request else lambda_uow)
