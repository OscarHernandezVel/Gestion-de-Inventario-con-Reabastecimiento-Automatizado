"""Adaptadores AWS de los puertos `RestockQueue` (SQS) y `OrderNotifier` (SNS)."""
import json

from .domain import Product, PurchaseOrder, RestockRequest


class SqsRestockQueue:
    def __init__(self, sqs, queue_url: str):
        self._sqs, self._url = sqs, queue_url

    def send(self, request: RestockRequest) -> None:
        self._sqs.send_message(QueueUrl=self._url, MessageBody=json.dumps({
            "request_id": request.id, "product_id": request.product_id, "qty": request.qty, "source": request.source.value}))


class SnsOrderNotifier:
    def __init__(self, sns, topic_arn: str):
        self._sns, self._topic = sns, topic_arn

    def notify(self, order: PurchaseOrder, product: Product) -> None:
        self._sns.publish(
            TopicArn=self._topic,
            Subject=f"Orden de compra #{order.id} - {product.sku}"[:100],
            Message=json.dumps({"po_id": order.id, "request_id": order.request_id, "product_id": product.id,
                                "sku": product.sku, "name": product.name, "qty": order.qty, "stock": product.stock}),
        )
