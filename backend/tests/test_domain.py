"""Dominio: buffer circular, árbol de categorías, reglas de producto/solicitud/orden y calendario."""
import unittest
from datetime import datetime, timedelta

from inventario.domain import (BusinessCalendar, Category, CategoryCycle, CircularBuffer, InsufficientStock,
                               InvalidQuantity, InvalidTransition, OrderStatus, Product, PurchaseOrder, RequestStatus,
                               RestockRequest, Source, StockHistory, StockReading, build_category_tree)
from tests.fakes import SATURDAY_21, TUESDAY_10


class CircularBufferTest(unittest.TestCase):
    def test_sobrescribe_el_mas_antiguo_al_llenarse(self):
        buf = CircularBuffer(3)
        slots = [buf.append(v) for v in range(5)]
        self.assertEqual(slots, [0, 1, 2, 0, 1])
        self.assertEqual(list(buf), [2, 3, 4])
        self.assertEqual((len(buf), buf.oldest(), buf.latest()), (3, 2, 4))

    def test_reconstruye_desde_ranuras_persistidas(self):
        buf = CircularBuffer.from_slots(3, head=5, slots={0: "d", 1: "e", 2: "c"})
        self.assertEqual(list(buf), ["c", "d", "e"])
        self.assertEqual(buf.append("f"), 2)
        self.assertEqual(list(buf), ["d", "e", "f"])

    def test_vacio_y_capacidad_invalida(self):
        self.assertEqual((list(CircularBuffer(2)), CircularBuffer(2).latest()), ([], None))
        with self.assertRaises(ValueError):
            CircularBuffer(0)


def history(*stocks, start=TUESDAY_10):
    h = StockHistory()
    for i, s in enumerate(stocks):
        h.append(StockReading(start + timedelta(hours=i), s))
    return h


class StockHistoryTest(unittest.TestCase):
    def test_consumo_ignora_llegadas_de_mercancia(self):
        h = history(100, 90, 80, 280, 270)  # +200 en la 4.ª hora es una recepción
        self.assertEqual(h.consumption_per_hour(), 30 / 4)
        self.assertEqual(h.hours_to_stockout(75), 10.0)

    def test_sin_datos_suficientes_no_estima(self):
        self.assertEqual(history(50).consumption_per_hour(), 0.0)
        self.assertIsNone(history(50).hours_to_stockout(50))


class ProductTest(unittest.TestCase):
    def product(self, **kw):
        base = dict(id=2, sku="SKU-002", name="Tuerca", stock=35, threshold=80, reorder_qty=200)
        return Product(**{**base, **kw})

    def test_auto_restock_solo_bajo_umbral_y_sin_pendientes(self):
        self.assertTrue(self.product().needs_auto_restock())
        self.assertFalse(self.product(pending_orders=1).needs_auto_restock())
        self.assertFalse(self.product(stock=80).needs_auto_restock())

    def test_cantidad_sugerida_cubre_el_tiempo_de_entrega(self):
        p = self.product(history=history(95, 85, 75, 65, 55, 45, 35), lead_time_hours=30)  # 10 u/h
        self.assertEqual(p.suggested_qty(), 10 * 30 + 80 - 35)  # 345 > reorder_qty 200
        self.assertEqual(self.product().suggested_qty(), 200)  # sin historial: reorder_qty

    def test_salida_y_recepcion(self):
        p = self.product()
        p.dispatch(5)
        p.receive(100)
        self.assertEqual(p.stock, 130)
        with self.assertRaises(InsufficientStock):
            p.dispatch(131)
        for bad in (0, 10_001, "5", True):
            with self.assertRaises(InvalidQuantity):
                p.dispatch(bad)

    def test_no_libera_reserva_inexistente(self):
        with self.assertRaises(InvalidTransition):
            self.product().release_restock()


class RequestAndOrderTest(unittest.TestCase):
    def test_ciclo_de_vida_solicitud_orden_recepcion(self):
        p = Product(2, "SKU-002", "Tuerca", 35, 80, 200)
        req = RestockRequest.create(p, Source.MANUAL, 50)
        self.assertEqual((p.pending_orders, req.qty, req.status), (1, 50, RequestStatus.QUEUED))
        order = PurchaseOrder.from_request(req, p)
        self.assertEqual((p.pending_orders, req.status), (0, RequestStatus.ORDERED))
        with self.assertRaises(InvalidTransition):
            req.mark_ordered()
        order.receive(p)
        self.assertEqual((p.stock, order.status), (85, OrderStatus.RECEIVED))
        with self.assertRaises(InvalidTransition):
            order.receive(p)

    def test_cantidad_invalida_no_reserva(self):
        p = Product(2, "SKU-002", "Tuerca", 35, 80, 200)
        with self.assertRaises(InvalidQuantity):
            RestockRequest.create(p, Source.MANUAL, 0)
        self.assertEqual(p.pending_orders, 0)


class CategoryTreeTest(unittest.TestCase):
    CATS = [Category(1, "Bodega", None), Category(2, "Fijaciones", 1), Category(3, "Tornillería", 2),
            Category(4, "Tuercas", 2), Category(5, "Herramientas", 1)]

    def test_agrega_stock_y_alertas_hacia_arriba(self):
        products = [Product(1, "A", "a", 420, 100, 1, category_id=3), Product(2, "B", "b", 35, 80, 1, 1, category_id=4),
                    Product(3, "C", "c", 12, 20, 1, category_id=5), Product(4, "D", "d", 5, 1, 1, category_id=99)]
        root = build_category_tree(self.CATS, products)
        self.assertEqual(root.summary()["stock"], 472)
        self.assertEqual(root.summary()["low"], 2)
        fij = root.find(2)
        self.assertEqual((fij.summary()["stock"], fij.summary()["pending_orders"]), (455, 1))
        self.assertEqual(root.find(4).path(), ["Bodega", "Fijaciones", "Tuercas"])
        self.assertEqual([n.id for n in root.walk()], [1, 2, 3, 4, 5])
        self.assertEqual([p.sku for p in root.products], ["D"])  # sin categoría válida: cuelga de la raíz

    def test_rechaza_ciclos_raices_multiples_y_padres_inexistentes(self):
        cycle = self.CATS + [Category(6, "X", 7), Category(7, "Y", 6)]
        for cats in (cycle, self.CATS + [Category(8, "Otra raíz", None)], [Category(1, "R", None), Category(2, "H", 9)]):
            with self.assertRaises(CategoryCycle):
                build_category_tree(cats, [])


class CalendarTest(unittest.TestCase):
    def test_ventana_lunes_a_viernes_8_a_18(self):
        cal = BusinessCalendar()
        self.assertTrue(cal.is_open(TUESDAY_10))
        self.assertTrue(cal.is_open(TUESDAY_10.replace(hour=17, minute=59)))
        self.assertFalse(cal.is_open(TUESDAY_10.replace(hour=18)))
        self.assertFalse(cal.is_open(TUESDAY_10.replace(hour=7, minute=59)))
        self.assertFalse(cal.is_open(SATURDAY_21))

    def test_proxima_apertura(self):
        cal = BusinessCalendar()
        self.assertEqual(cal.next_opening(SATURDAY_21), datetime(2026, 11, 23, 8, 0))  # lunes
        self.assertEqual(cal.next_opening(TUESDAY_10.replace(hour=6)), datetime(2026, 11, 17, 8, 0))
        self.assertEqual(cal.next_opening(datetime(2026, 11, 20, 19, 0)), datetime(2026, 11, 23, 8, 0))  # viernes noche


if __name__ == "__main__":
    unittest.main()
