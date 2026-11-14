# Inventario con Reabastecimiento Automático (AWS)

EC2 + ALB + RDS (MySQL 8) con **RDS Proxy** + Lambda + SQS + SNS + EventBridge Scheduler + agente Claude en **Amazon Bedrock**.
Lenguaje: **Python 3.12** (Lambdas y API). Infraestructura: **Terraform**.

| Documento | Contenido |
|---|---|
| [HU & Mockups/README.md](<HU & Mockups/README.md>) | 6 historias, cada una con su .md, mockup .html y .jpg |
| [docs/02-analisis-aws-rnf.md](docs/02-analisis-aws-rnf.md) | Arquitectura, trazabilidad de requerimientos, RNF técnicos |
| [docs/03-agente.md](docs/03-agente.md) | Rol, límites y características del agente Claude |
| [docs/04-diseno-backend-grasp.md](docs/04-diseno-backend-grasp.md) | Capas Controller/Service/Repository, árbol de categorías, buffer circular, trazas y GRASP |
| [docs/diagrams/](docs/diagrams) | Diagramas PlantUML (clases por capas y 4 secuencias) |

## Estructura (monorepo)
```
backend/inventario/
  domain.py              entidades, árbol de categorías, buffer circular, calendario hábil
  services.py            casos de uso (InventoryService, RestockService, PurchaseOrderService, InsightService)
  ports.py               interfaces: UnitOfWork, repositorios, cola, notificador
  mysql_repositories.py  repositorios MySQL + transacción (START TRANSACTION/COMMIT/ROLLBACK)
  aws_adapters.py        SQS y SNS
  db.py                  conexión vía RDS Proxy (IAM + TLS)
  handlers.py            Lambdas checker, consumer, bench, migrate
  api.py, dto.py         API HTTP del frontend (EC2) y contrato JSON
  agent.py               Lambda del agente Claude (Bedrock)
  container.py           composición e inyección de dependencias
  schema.sql             esquema + datos semilla
backend/tests/           38 pruebas unitarias sin AWS
frontend/                index.html (servido por NGINX)
infra/                   Terraform
scripts/                 build, demo de transacción, prueba de carga
HU & Mockups/            historias de usuario con mockups .html interactivos y .jpg
```

### API HTTP
| Método y ruta | Uso |
|---|---|
| `GET /api/products` | Inventario con `low`, consumo por hora, horas hasta agotarse y cantidad sugerida |
| `GET /api/categories/tree?products=0\|1` | Árbol de categorías con stock, alertas y consumo agregados por nivel |
| `GET /api/orders?limit=20`, `GET /api/insights` | Solicitudes/órdenes recientes y alertas del agente |
| `POST /api/products/{id}/restock` `{"qty": 50}` | Reabastecimiento manual (202, en cola fuera de horario) |
| `POST /api/products/{id}/dispatch` `{"qty": 5}` | Salida de bodega (409 si no hay stock) |
| `POST /api/orders/{id}/receive` | Recepción de mercancía: suma stock una sola vez |
| `POST /api/agent` `{"question": "..."}` | Pregunta al agente |

## Despliegue
```bash
bash scripts/build.sh
```
```bash
cd infra && terraform init && terraform apply -var alert_email=comprador@ejemplo.com
```
Habilita el acceso a Claude Haiku 5.5 en la consola de Bedrock (*Model access*); el agente usa el rol IAM de la Lambda, sin API key.
Confirma la suscripción en el correo de SNS. `terraform output app_url` da la URL del frontend.

## Demostraciones

### 1. Transacción atómica + fallo parcial de batch
```bash
bash scripts/demo_tx_failure.sh
```
Pausa el consumidor, encola dos solicitudes e invoca el consumidor con un batch donde la segunda trae `simulate_failure`.
Resultado esperado: respuesta `{"batchItemFailures":[{"itemIdentifier":"fail-2"}]}`; SKU-001 queda con su orden y `pending_orders=0`;
SKU-003 queda **igual que antes** (`pending_orders=1`, EN COLA, sin orden) porque el `UPDATE` se revirtió con `ROLLBACK`.

### 2. Con y sin RDS Proxy bajo carga concurrente
```bash
python scripts/load_test.py run --mode proxy --concurrency 100 --rounds 3
```
```bash
terraform -chdir=infra apply -var enable_direct_benchmark=true
```
```bash
python scripts/load_test.py run --mode direct --concurrency 100 --rounds 3
```
```bash
python scripts/load_test.py report --minutes 30
```
```bash
terraform -chdir=infra apply -var enable_direct_benchmark=false
```
Qué mirar (dashboard `inventario` en CloudWatch):
- **Con Proxy**: `ClientConnections` ≈ 100, pero `DatabaseConnections` de la instancia queda en un número pequeño (multiplexado); 0 errores de conexión.
- **Sin Proxy**: `DatabaseConnections` de la instancia sube hasta el máximo de `db.t4g.micro` y aparecen `ConnectionErrors` ("Too many connections"); `ConnectDuration` sube porque cada Lambda abre su propia conexión TLS.
- `QueryDuration` (p90) por modo y `QueryDatabaseResponseLatency` del Proxy en el mismo gráfico.

Deja la conexión directa cerrada al terminar: es la única excepción a la regla "solo vía Proxy".

### 3. Horario hábil
- `cron(0 8-17 ? * MON-FRI *)` en `America/Bogota` ejecuta la revisión a cada hora en punto de 08:00 a 17:00.
- 08:00 L-V habilita el consumidor y 18:00 lo deshabilita. Fuera de ese horario `POST /api/products/{id}/restock` responde 202 y la solicitud queda EN COLA en SQS (retención 14 días).
- Verificar: `aws scheduler list-schedules` y `aws lambda get-event-source-mapping --uuid $(terraform -chdir=infra output -raw consumer_mapping_uuid)`.

## Pruebas locales
```bash
cd backend && pip install -r requirements.txt tzdata
```
```bash
cd backend && python -m unittest discover -s tests -t . -v
```
Cubren: buffer circular, árbol (agregados y ciclos), reglas de producto/solicitud/orden, COMMIT/ROLLBACK con el SQL real,
fallo a mitad de transacción, idempotencia, rollback si falla SQS, fallo parcial de batch, rutas HTTP y horario hábil.

## GitHub
Recomendado. El workflow `.github/workflows/ci.yml` ejecuta pruebas, `terraform fmt -check` y `terraform validate` en cada PR.
Siguiente paso cuando haya estado remoto (S3): `terraform apply` desde Actions con OIDC, sin llaves de AWS en el repositorio.
