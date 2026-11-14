"""Servicios con puertos en memoria: atomicidad, idempotencia, aislamiento de fallos y horario hábil."""
import unittest

from inventario.domain import (BusinessCalendar, CategoryCycle, InsufficientStock, InvalidInput, InvalidQuantity, NotFound,
                               OrderStatus, RequestStatus, Source)
from inventario.services import InsightService, InventoryService, PurchaseOrderService, RestockService
from tests.fakes import SATURDAY_21, TUESDAY_10, FakeNotifier, FakeQueue, FakeUnitOfWork, seeded_state


def restock(state, queue=None, now=TUESDAY_10):
    return RestockService(FakeUnitOfWork(state), queue or FakeQueue(), BusinessCalendar(), lambda: now)


class RestockServiceTest(unittest.TestCase):
    def test_scan_encola_solo_lo_necesario(self):
        s, q = seeded_state(), FakeQueue()
        result = restock(s, q).scan_low_stock()
        self.assertEqual((result.low, result.queued), ([2], [2]))  # SKU-004 ya tenía pendiente
        self.assertEqual((s.products[2].pending_orders, q.sent[0].qty), (1, 200))
        self.assertEqual(restock(s, q).scan_low_stock().queued, [])  # segunda pasada: no duplica

    def test_fallo_de_sqs_revierte_la_reserva_y_no_detiene_a_otros(self):
        s = seeded_state()
        s.products[4].pending_orders = 0
        result = restock(s, FakeQueue(fail=True)).scan_low_stock()
        self.assertEqual(result.failed, [2, 4])
        self.assertEqual([s.products[i].pending_orders for i in (2, 4)], [0, 0])
        self.assertEqual((s.requests, s.rollbacks), ({}, 2))

    def test_manual_fuera_de_horario_queda_en_cola_con_proxima_apertura(self):
        s = seeded_state()
        ticket = restock(s, now=SATURDAY_21).request_restock(2, Source.MANUAL, 50)
        self.assertFalse(ticket.processing_now)
        self.assertEqual(ticket.next_processing.strftime("%a %H:%M"), "Mon 08:00")
        self.assertEqual(s.requests[ticket.request.id].status, RequestStatus.QUEUED)

    def test_manual_permite_aunque_haya_pendiente_y_valida_cantidad(self):
        s = seeded_state()
        self.assertTrue(restock(s).request_restock(4, Source.MANUAL, 10).processing_now)
        self.assertEqual(s.products[4].pending_orders, 2)
        with self.assertRaises(InvalidQuantity):
            restock(s).request_restock(4, Source.MANUAL, 0)
        with self.assertRaises(NotFound):
            restock(s).request_restock(99, Source.MANUAL, 10)


class PurchaseOrderServiceTest(unittest.TestCase):
    def setUp(self):
        self.s, self.n = seeded_state(), FakeNotifier()
        self.req = restock(self.s).request_restock(2, Source.MANUAL, 200).request
        self.svc = PurchaseOrderService(FakeUnitOfWork(self.s), self.n)

    def test_crea_orden_libera_reserva_y_notifica(self):
        order = self.svc.create_from_request(self.req.id)
        self.assertEqual((order.id, order.qty, self.s.products[2].pending_orders), (1000, 200, 0))
        self.assertEqual(self.s.requests[self.req.id].status, RequestStatus.ORDERED)
        self.assertEqual(self.n.sent[0][1], "SKU-002")

    def test_fallo_a_mitad_deja_los_datos_como_antes(self):
        with self.assertRaises(RuntimeError):
            self.svc.create_from_request(self.req.id, fail_midway=True)
        self.assertEqual(self.s.products[2].pending_orders, 1)
        self.assertEqual(self.s.requests[self.req.id].status, RequestStatus.QUEUED)
        self.assertEqual((self.s.orders, self.n.sent), ({}, []))
        self.assertIsNotNone(self.svc.create_from_request(self.req.id))  # el reintento sí funciona

    def test_mensaje_duplicado_es_idempotente(self):
        self.svc.create_from_request(self.req.id)
        self.assertIsNone(self.svc.create_from_request(self.req.id))
        self.assertEqual((len(self.s.orders), len(self.n.sent)), (1, 1))

    def test_recibir_mercancia_suma_stock_una_sola_vez(self):
        order = self.svc.create_from_request(self.req.id)
        self.svc.receive(order.id)
        self.assertEqual((self.s.products[2].stock, self.s.orders[order.id].status), (235, OrderStatus.RECEIVED))
        with self.assertRaises(Exception):
            self.svc.receive(order.id)
        self.assertEqual(self.s.products[2].stock, 235)


class InventoryServiceTest(unittest.TestCase):
    def test_lecturas_al_buffer_circular_y_salida(self):
        s = seeded_state()
        svc = InventoryService(FakeUnitOfWork(s), lambda: TUESDAY_10)
        self.assertEqual(svc.record_readings(), 4)
        self.assertEqual(s.products[2].history.latest().stock, 35)
        self.assertEqual(svc.dispatch(2, 5).stock, 30)
        with self.assertRaises(InsufficientStock):
            svc.dispatch(2, 31)
        self.assertEqual(s.products[2].stock, 30)

    def test_arbol_de_categorias(self):
        s = seeded_state()
        tree = InventoryService(FakeUnitOfWork(s), lambda: TUESDAY_10).category_tree()
        self.assertEqual((tree.name, tree.summary()["low"], tree.find(2).summary()["stock"]), ("Bodega", 2, 665))
        s.categories.append(s.categories[0].__class__(9, "Huérfana", 42))
        with self.assertRaises(CategoryCycle):
            InventoryService(FakeUnitOfWork(s), lambda: TUESDAY_10).category_tree()

    def test_insights_validan_entrada(self):
        s = seeded_state()
        svc = InsightService(FakeUnitOfWork(s))
        svc.record("ALTA", " SKU-002 en riesgo ")
        self.assertEqual(svc.list_recent(5), [{"severity": "ALTA", "summary": "SKU-002 en riesgo"}])
        for sev, text in (("CRITICA", "x"), ("INFO", ""), ("INFO", "x" * 501)):
            with self.assertRaises(InvalidInput):
                svc.record(sev, text)


if __name__ == "__main__":
    unittest.main()
