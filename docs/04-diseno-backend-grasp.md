# Diseño del backend: capas, árbol de categorías, buffer circular y GRASP

**Resumen.** El backend (`backend/inventario/`) separa presentación, negocio, puertos, datos y dominio, y todas las dependencias apuntan hacia el dominio. Las reglas están en las entidades (Experto); cada caso de uso es un método de servicio con su propia transacción (Controlador GRASP). MySQL, SQS y SNS se esconden detrás de puertos (Variaciones protegidas).

Hay dos estructuras de datos nuevas:
- **Árbol de categorías:** agrega stock, alertas y consumo de abajo hacia arriba.
- **Buffer circular por producto:** guarda las últimas 24 lecturas de stock para estimar el consumo y la cantidad de reorden.

Diagramas (PlantUML, en `docs/diagrams/`):

| Archivo | Muestra |
|---|---|
| `clases-capas.puml` | Modelo por capas |
| `secuencia-revisar-stock.puml` | Revisión horaria del scheduler |
| `secuencia-crear-orden.puml` | Consumidor SQS con transacción atómica |
| `secuencia-reabastecimiento-manual.puml` | Pedido manual desde la API |
| `secuencia-consultar-inventario.puml` | Consulta del árbol de categorías |

---

## 1. Relaciones y multiplicidades

| Origen → Destino | Tipo | Multiplicidad | Navegabilidad | Lectura |
|---|---|---|---|---|
| CategoryNode → CategoryNode | Composición | 0..1 padre — 0..* hijos | padre → hijos (y `parent` para `path()`) | Una categoría se compone de 0..N subcategorías; cada una tiene como máximo 1 padre (la raíz no tiene). |
| CategoryNode → Product | Agregación | 1 — 0..* | nodo → producto | Una categoría agrupa 0..N productos; el producto existe fuera del árbol. Si su categoría no existe, cuelga de la raíz. |
| Product → StockHistory | Composición | 1 — 1 | producto → historial | Cada producto tiene exactamente un historial, que nace y muere con él. |
| StockHistory → CircularBuffer | Generalización | — | — | Un historial **es un** buffer circular de `StockReading`. |
| RestockRequest → Product | Asociación | 0..* — 1 | solicitud → producto | Un producto tiene 0..N solicitudes; cada solicitud repone exactamente 1 producto (FK `product_id`). |
| PurchaseOrder → RestockRequest | Asociación | 0..1 — 1 | orden → solicitud | Cada solicitud genera como máximo 1 orden (`UNIQUE request_id`); cada orden atiende exactamente 1 solicitud. |
| Servicios → UnitOfWork / RestockQueue / OrderNotifier | Asociación (inyectada) | 1 — 1 | servicio → puerto | Cada servicio conoce un único puerto de cada tipo, recibido en el constructor. |
| MySqlUnitOfWork, SqsRestockQueue, SnsOrderNotifier → puertos | Realización | — | — | Cada clase implementa el contrato del puerto correspondiente. |

### Responsabilidades

| Clase | Existe para… | Conoce | Hace |
|---|---|---|---|
| `Product` | Representar un artículo y sus reglas de stock | stock, umbral, reorden, pendientes, tiempo de entrega, historial | `needs_auto_restock`, `suggested_qty`, `dispatch`, `receive`, `reserve/release_restock`, `record_reading` |
| `RestockRequest` | Registrar la necesidad de reponer | producto, cantidad, origen, estado | `create` (valida y reserva), `mark_ordered` |
| `PurchaseOrder` | Representar el pedido al proveedor | solicitud, cantidad, estado | `from_request` (transición atómica en memoria), `receive` |
| `CircularBuffer` / `StockHistory` | Guardar las últimas N lecturas con memoria fija | ranuras, cabeza | `append` O(1), iterar en orden, `consumption_per_hour`, `hours_to_stockout` |
| `CategoryNode` | Organizar productos en jerarquía | nombre, padre, hijos, productos | `walk`, `find`, `path`, `summary` (agrega el subárbol) |
| `BusinessCalendar` | Definir el horario hábil | horas de apertura y cierre | `is_open`, `next_opening` |

Señales revisadas:
- No queda un modelo anémico: las reglas están en las entidades.
- No hay asociaciones bidireccionales, salvo `parent` en el árbol, que `path()` necesita.

---

## 2. Clasificación por capas

| Clase / módulo | Capa | Justificación |
|---|---|---|
| `domain.py` (Product, RestockRequest, PurchaseOrder, CategoryNode, CircularBuffer, StockHistory, BusinessCalendar, errores) | Dominio | Existiría sin software. No importa HTTP, SQL ni boto3. |
| `services.py` (InventoryService, RestockService, PurchaseOrderService, InsightService) | Negocio | Cambia si cambia el proceso. Orquesta y define la transacción. |
| `ports.py` | Negocio (contratos) | Son interfaces en lenguaje del dominio que los servicios necesitan. |
| `mysql_repositories.py`, `db.py` | Datos | Cambian si cambia el almacenamiento. Solo SQL parametrizado. |
| `aws_adapters.py` | Infraestructura | Cambia si se reemplaza SQS o SNS. |
| `api.py`, `handlers.py`, `agent.py` | Presentación | Cambian si cambia el canal (HTTP, evento Lambda, herramienta del agente). |
| `dto.py` | Presentación | Contrato JSON y traducción de errores a códigos HTTP. |
| `container.py` | Composición | Es el único módulo que conoce las clases concretas. |

Ninguna clase quedó en dos capas. Antes, `core.py` mezclaba conexión, SQL y reglas; se dividió en `db`, `mysql_repositories`, `domain` y `services`.

---

## 3. Modelo Controller / Service / Repository

```
api.route / handlers / agent  →  *Service  →  UnitOfWork (puerto) → MySqlUnitOfWork → RDS Proxy
          ↘ dto                  ↘ RestockQueue / OrderNotifier (puertos) → SQS / SNS
todas las capas → domain.py   (el dominio no depende de nadie)
```

| Clase | Métodos principales | Colaboradores |
|---|---|---|
| `api.route` | `GET /api/products`, `GET /api/categories/tree`, `GET /api/orders`, `POST /api/products/{id}/restock`, `POST /api/products/{id}/dispatch`, `POST /api/orders/{id}/receive`, `POST /api/agent` | servicios, `dto` |
| `handlers` | `checker_handler`, `consumer_handler`, `bench_handler`, `migrate_handler` | servicios, `db` |
| `InventoryService` | `list_products()`, `category_tree()`, `dispatch(id, qty)`, `record_readings()`, `list_orders(limit)` | `UnitOfWork`, reloj |
| `RestockService` | `scan_low_stock()`, `request_restock(id, source, qty)` | `UnitOfWork`, `RestockQueue`, `BusinessCalendar`, reloj |
| `PurchaseOrderService` | `create_from_request(request_id, fail_midway)`, `receive(order_id)` | `UnitOfWork`, `OrderNotifier` |
| `InsightService` | `list_recent(limit)`, `record(severity, summary)` | `UnitOfWork` |
| `MySqlUnitOfWork` | `__enter__` → START TRANSACTION; `__exit__` → COMMIT o ROLLBACK | repositorios MySQL |

Inyección por constructor en `container.py`:
- Las Lambdas reutilizan la conexión del contenedor.
- La API de EC2 abre una conexión por petición, y RDS Proxy hace el pooling.

### Árbol de categorías

- **Persistencia:** lista de adyacencia en la tabla `categories (id, name, parent_id)`, con `CHECK parent_id <> id`.
- **Construcción en O(n):** `build_category_tree` crea los nodos y engancha cada uno a su padre.
- **Validaciones** (cualquier falla lanza `CategoryCycle`, que se traduce a 422):
  - Debe haber una sola raíz.
  - Cada padre debe existir.
  - Todos los nodos deben ser alcanzables desde la raíz; si no lo son, forman un ciclo.
- **Recorrido iterativo** (`walk`), sin depender del límite de recursión.
- **Agregados por nivel** (`summary`): productos, stock, bajo umbral, pendientes y consumo por hora. Responden "¿qué rama de la bodega está en riesgo?" y el agente los usa con la herramienta `get_category_summary`.

### Buffer circular de stock

- **Persistencia:** la tabla `stock_readings (product_id, slot)` tiene 24 ranuras fijas por producto, y `products.history_head` es la cabeza.
- **Escritura:** la ranura es `head % 24`, se sobrescribe con `INSERT … ON DUPLICATE KEY UPDATE` y luego se hace `head += 1`. La tabla nunca crece y la escritura es O(1).
- **Lectura:** `CircularBuffer.from_slots` reconstruye el orden cronológico a partir de la cabeza.
- **Uso:**
  - `consumption_per_hour` suma solo las bajadas, para que la llegada de mercancía no cuente como consumo negativo.
  - `suggested_qty = max(reorder_qty, consumo × tiempo_de_entrega + umbral − stock)`.
  - `hours_to_stockout` se expone en la API.

---

## 4. Trazas de casos de uso

### 4.1 Crear orden de compra (consumidor SQS)

| # | Capa | Clase.método | Responsabilidad | Falla → resultado |
|---|---|---|---|---|
| 1 | Presentación | `handlers.consumer_handler` | Iterar el lote y aislar cada mensaje | JSON inválido → solo ese mensaje va a `batchItemFailures` |
| 2 | Negocio | `PurchaseOrderService.create_from_request` | Abrir la transacción y orquestar | — |
| 3 | Datos | `requests.get_queued(for_update)` | Bloquear la solicitud EN COLA | No existe o ya está ORDENADA → `None` (idempotente, sin escrituras) |
| 4 | Datos | `products.get(for_update)` | Bloquear el producto | No existe → `NotFound` → reintento y luego DLQ |
| 5 | Dominio | `PurchaseOrder.from_request` → `RestockRequest.mark_ordered`, `Product.release_restock` | Hacer la transición de estados en memoria | Estado inválido → `InvalidTransition` → ROLLBACK |
| 6 | Datos | `products.save` → `orders.add` → `requests.save` | Persistir en la misma transacción | Error técnico → ROLLBACK, el mensaje vuelve a la cola (máx. 3 intentos, luego DLQ) |
| 7 | Infra | `SnsOrderNotifier.notify` | Notificar antes del COMMIT (al menos una vez) | SNS falla → ROLLBACK, nada cambia |
| 8 | Datos | `MySqlUnitOfWork.__exit__` | COMMIT | — |

Demostración del fallo a mitad de la transacción:
- Con `fail_midway=True` se lanza el error después del `UPDATE pending_orders` y antes del `INSERT` de la orden.
- La prueba `test_fallo_a_mitad_hace_rollback` verifica `ROLLBACK`, que no se ejecuta el `INSERT` y que no se notifica.
- `test_fallo_a_mitad_deja_los_datos_como_antes` verifica el estado final.

### 4.2 Revisar stock (scheduler)

| # | Capa | Clase.método | Falla → resultado |
|---|---|---|---|
| 1 | Presentación | `checker_handler` | Si algún producto falló, la Lambda termina en error para que la alarma lo detecte |
| 2 | Negocio | `InventoryService.record_readings` | Una lectura por producto en su buffer circular (una transacción) |
| 3 | Negocio | `RestockService.scan_low_stock` | Una transacción por producto: un fallo no detiene a los demás |
| 4 | Dominio | `Product.needs_auto_restock`, `suggested_qty` | Ya tiene una solicitud pendiente → se omite (no duplica) |
| 5 | Dominio | `RestockRequest.create` | Valida 1..10000 y reserva (`pending_orders + 1`) |
| 6 | Infra | `SqsRestockQueue.send` antes del COMMIT | SQS falla → ROLLBACK de la reserva |

### 4.3 Solicitar reabastecimiento manual (API)

| Paso | Resultado HTTP |
|---|---|
| `dto.qty_from` (formato) | 400 si la cantidad no es un entero entre 1 y 10000 |
| `RestockService.request_restock(MANUAL)` | 404 si el producto no existe |
| `BusinessCalendar.is_open / next_opening` | 202 con "Solicitud encolada" o "se procesará el dd/mm a las 08:00" |
| Error técnico | 503 "Servicio no disponible, reintente" |

### 4.4 Consultar inventario (lista y árbol)

| Paso | Resultado HTTP |
|---|---|
| `InventoryService.list_products` / `category_tree` | 200 con `low`, `suggested_qty`, `hours_to_stockout` y agregados por nivel |
| `build_category_tree` detecta un ciclo | 422 |
| BD no disponible | 503 (el frontend muestra "No se pudo cargar el inventario, reintente") |

Los códigos se asignan en un solo lugar, `dto.ERROR_STATUS`:

| Excepción | Código |
|---|---|
| `BadRequest` | 400 |
| `NotFound` | 404 |
| `InvalidTransition`, `InsufficientStock` | 409 |
| `InvalidQuantity`, `InvalidInput`, `CategoryCycle` (`DomainError`) | 422 |
| Cualquier otra | 503 |

---

## 5. Justificación GRASP, cohesión y acoplamiento

| Decisión | Principio | Razón | Costo / alternativa descartada |
|---|---|---|---|
| `Product.needs_auto_restock()`, `suggested_qty()`, `dispatch()` en la entidad | Experto en información | El producto tiene stock, umbral, historial y tiempo de entrega | Ponerlas en el servicio dejaba un modelo anémico (como el antiguo `core.py`) |
| `StockHistory.consumption_per_hour()` | Experto en información | Solo el historial conoce las lecturas | — |
| `RestockRequest.create(product, …)` y `PurchaseOrder.from_request(request, product)` | Creador | Tienen los datos de inicialización y hacen la transición de estado al crear | El servicio no hace `new` con lógica dispersa |
| `CategoryNode.summary()` agrega el subárbol | Experto + Alta cohesión | Cada nodo conoce a sus hijos | Calcularlo en SQL con CTE recursivas acoplaba la regla al motor |
| Un servicio por grupo de casos de uso, un método por caso | Controlador GRASP + Alta cohesión | `RestockService` solo pide reposición; `PurchaseOrderService` solo crea y recibe órdenes | No se hizo un "CRUD service" por tabla |
| `api.route` y `handlers` delgados | Controlador (fachada de entrada) + Alta cohesión | Solo traducen el canal: validar formato, llamar a un método, elegir el código | Cero reglas de negocio en el controller |
| `UnitOfWork`, repositorios, `RestockQueue`, `OrderNotifier` como puertos | Variaciones protegidas + Bajo acoplamiento | Los servicios no conocen MySQL, SQS ni SNS; las pruebas usan implementaciones en memoria | Un archivo más (`ports.py`). Se descartó pasar el cursor de pymysql a los servicios, como antes |
| `MySqlUnitOfWork` | Fabricación pura + Indirección | No existe en el dominio, pero concentra START TRANSACTION / COMMIT / ROLLBACK en un solo lugar | — |
| DTO entre API y dominio | Indirección | La API no expone entidades; el contrato JSON puede cambiar sin tocar el dominio | — |
| `container.py` | Bajo acoplamiento (DIP) | Es el único que nombra clases concretas | — |
| `CategoryCycle` y la validación del árbol | Variaciones protegidas | Datos corruptos dan un error claro (422), no un bucle infinito | — |

**Prueba de cohesión** (describir cada clase sin usar "y"):
- `RestockService` "pide reposición".
- `PurchaseOrderService` "gestiona el ciclo de vida de la orden".
- `MySqlProductRepository` "persiste productos".

**Acoplamiento:**
- Cambiar MySQL por otro motor solo toca `mysql_repositories.py` y `db.py`.
- Cambiar SQS solo toca `aws_adapters.py`.
- No hay ciclos de importación: `domain` no importa nada del paquete y `services` solo importa `domain` y `ports`.

**Tensiones honestas:**
- `RestockService.request_restock` llama a SQS dentro de la transacción. Así se gana atomicidad (si SQS falla no hay reserva), a cambio de mantener abierta la transacción unos milisegundos más.
- El `parent` en `CategoryNode` es la única navegación bidireccional; existe porque `path()` lo necesita.

---

## 6. Requerimientos no funcionales del backend

| RNF | Implementación |
|---|---|
| Consistencia | Transacciones explícitas en `MySqlUnitOfWork`; `SELECT … FOR UPDATE`; `CHECK` y `UNIQUE request_id` en la BD |
| Idempotencia | Solicitud ≠ QUEUED → `None`; el scheduler no duplica si `pending_orders > 0` |
| Resiliencia | Fallo por mensaje (`batchItemFailures`) y por producto (`ScanResult.failed`); DLQ tras 3 intentos |
| Seguridad | SQL parametrizado; límite de 10 KB por cuerpo; validación de tipos y rangos; IAM auth al Proxy; el agente usa un usuario de solo lectura |
| Observabilidad | Log JSON por petición (`request_id`, ruta, estado, ms) y cabecera `X-Request-Id`; métricas EMF `Inventario/Restock` (LowStock, Enqueued, EnqueueFailed, Processed, BatchItemFailures) |
| Rendimiento | Buffer de memoria fija (24 ranuras); árbol en O(n); una consulta para los historiales de todos los productos |
| Mantenibilidad | 38 pruebas unitarias sin AWS (dominio, servicios, datos, presentación); capas con dependencias en una sola dirección |

## 7. Riesgos y mejoras

1. **Escala del árbol y del historial:** se cargan completos en cada consulta. Con miles de productos conviene paginar o cachear `summary()` por categoría.
2. **Historial con huecos:** el buffer guarda lecturas horarias en días hábiles, así que las horas de fin de semana no cuentan como consumo. Si la bodega despacha los fines de semana, conviene registrar lecturas también esos días.
3. **Recepción de mercancía:** `POST /api/orders/{id}/receive` no valida cantidades parciales; agregar `received_qty` si el proveedor entrega por partes.
