# Análisis del entorno AWS y requerimientos no funcionales

## 1. Arquitectura

```
                    Internet
                       │ HTTP/HTTPS
                 ┌─────▼─────┐   subredes públicas (2 AZ)
                 │    ALB    │
                 └─────┬─────┘
 ┌─────────────────────┼───────────────────────── subredes privadas (2 AZ) ─┐
 │               ┌─────▼──────┐                                             │
 │               │ EC2 NGINX  │  /      → frontend estático                 │
 │               │ + API py   │  /api/* → API (127.0.0.1:8080)              │
 │               └─┬───────┬──┘                                             │
 │      SendMessage│       │Invoke                                          │
 │  ┌──────────────▼┐   ┌──▼─────────────┐                                  │
 │  │ SQS restock   │   │ Lambda agent   │◄──── SNS (alertas en tiempo real)│
 │  │ wait=20s      │   │ (Claude)       │──► Amazon Bedrock (vía NAT)      │
 │  │ DLQ (3 int.)  │   └──────┬─────────┘                                  │
 │  └──▲─────────┬──┘          │                                            │
 │     │         │ batch=10, ReportBatchItemFailures                        │
 │ ┌───┴──────┐ ┌▼───────────┐ │                                            │
 │ │ Lambda   │ │ Lambda     │─┼──► SNS purchase-orders ──► correo comprador│
 │ │ checker  │ │ consumer   │ │                                            │
 │ └───▲───┬──┘ └─────┬──────┘ │                                            │
 │     │   │  IAM auth + TLS   │                                            │
 │     │ ┌─▼──────────▼────────▼─┐   pool    ┌──────────────┐               │
 │     │ │      RDS Proxy        ├──────────►│ RDS MySQL 8  │ (solo acepta  │
 │     │ └───────────────────────┘           └──────────────┘  al Proxy)    │
 └─────┼──────────────────────────────────────────────────────────────────── ┘
       │ cron(0 8-17 ? * MON-FRI *)  America/Bogota
 EventBridge Scheduler ── 08:00 L-V: habilita el consumidor (UpdateEventSourceMapping)
                       └─ 18:00 L-V: deshabilita el consumidor
```

## 2. Cómo se cumple cada requerimiento objetivo

| Requerimiento | Implementación | Dónde |
|---|---|---|
| NGINX en EC2 sirve el frontend | AL2023 arm64 en subred privada, NGINX + API Python con systemd, detrás de ALB | `infra/app.tf`, `infra/user_data.sh` |
| RDS almacena stock | MySQL 8, cifrado, backups 7 días, contraseña maestra gestionada por Secrets Manager | `infra/data.tf`, `backend/inventario/schema.sql` |
| Revisión cada hora, solo L-V 8-18 | `cron(0 8-17 ? * MON-FRI *)` con zona horaria (última ejecución 17:00, dentro de la ventana) | `infra/app.tf` |
| Fuera de horario se acepta y encola sin procesar | La API encola siempre; dos schedules habilitan (08:00) y deshabilitan (18:00) el event source mapping del consumidor. Retención SQS 14 días cubre el fin de semana | `infra/app.tf` |
| SQS long polling 20 s | `receive_wait_time_seconds = 20` | `infra/app.tf` |
| Batch 10 + fallos parciales | `batch_size = 10`, `function_response_types = ["ReportBatchItemFailures"]`, el handler devuelve `batchItemFailures` solo con los fallidos | `backend/inventario/handlers.py` |
| RDS Proxy obligatorio | SG de RDS solo admite al SG del Proxy; Lambdas solo reciben la variable del endpoint del Proxy; IAM auth `REQUIRED` + TLS | `infra/main.tf`, `infra/data.tf` |
| Transacción atómica | `START TRANSACTION` → descuenta `pending_orders` → inserta orden → marca solicitud → publica SNS → `COMMIT`; cualquier error → `ROLLBACK` | `backend/inventario/services.py`, `mysql_repositories.py` |
| Simular fallo | Mensaje con `simulate_failure` (solo si `ALLOW_FAULT_INJECTION=true`) lanza excepción tras el `UPDATE` y antes del `INSERT` | `scripts/demo_tx_failure.sh` |
| Métricas con y sin Proxy | Lambda `bench` en modo `proxy`/`direct`, métrica EMF `Inventario/Bench QueryDuration`, dashboard con `DatabaseConnections` de instancia y Proxy | `scripts/load_test.py`, `infra/monitoring.tf` |
| Agente Anthropic | Lambda con Claude en Amazon Bedrock (tool runner), herramientas de solo lectura + registrar alerta, usuario BD `agent_ro` | `backend/inventario/agent.py`, `docs/03-agente.md` |

### Sobre la transacción
El requerimiento menciona la Lambda de revisión, pero la orden de compra la crea la consumidora. Se aplica transacción en **ambas**:
- **checker**: `FOR UPDATE` del producto → `pending_orders + 1` → inserta solicitud → `SendMessage` → `COMMIT`. Si SQS falla, `ROLLBACK`.
- **consumer**: la transacción pedida (descontar contador + crear orden). Es idempotente: si la solicitud ya no está `QUEUED`, no hace nada.

Los efectos externos (SQS/SNS) van **antes del COMMIT**: si el COMMIT falla después, el reintento produce como máximo un mensaje duplicado, que la idempotencia absorbe. Así se evita perder una notificación.

## 3. Requerimientos no funcionales técnicos

| ID | Categoría | Requerimiento | Cómo se implementa |
|---|---|---|---|
| RNF-01 | Seguridad | Ningún componente de datos expuesto a Internet | RDS, Proxy, EC2 y Lambdas en subredes privadas; solo el ALB es público |
| RNF-02 | Seguridad | Sin contraseñas en código ni variables | IAM database auth al Proxy (token de 15 min); secretos en Secrets Manager; el agente usa Bedrock con el rol IAM (sin API key) |
| RNF-03 | Seguridad | Mínimo privilegio | Un rol IAM por Lambda; `rds-db:connect` solo para su usuario; `agent_ro` solo `SELECT` + `INSERT` en `agent_insights` |
| RNF-04 | Seguridad | Cifrado en tránsito y reposo | TLS obligatorio en Proxy; RDS, SQS y SNS cifrados; HTTPS en ALB si se define `certificate_arn` |
| RNF-05 | Seguridad | Acceso a EC2 sin SSH | Sin key pair ni puerto 22; acceso por SSM Session Manager; IMDSv2 obligatorio |
| RNF-06 | Disponibilidad | Tolerar caída de una AZ | Subredes en 2 AZ; `multi_az` activable en RDS; Proxy en 2 subredes |
| RNF-07 | Confiabilidad | No perder solicitudes | DLQ tras 3 intentos, retención 14 días, alarma si la DLQ tiene mensajes |
| RNF-08 | Confiabilidad | Consistencia ante fallos | Transacciones ACID, `CHECK (pending_orders >= 0)`, `request_id` único, idempotencia |
| RNF-09 | Rendimiento | La BD no se satura por concurrencia | RDS Proxy (pool 90 %); `maximum_concurrency = 5` en el consumidor |
| RNF-10 | Rendimiento | API responde < 500 ms p95 | Consultas indexadas, conexión vía Proxy por petición |
| RNF-11 | Observabilidad | Trazabilidad extremo a extremo | Logs con retención 14 días, X-Ray activo, dashboard y alarmas en CloudWatch |
| RNF-12 | Costos | Bajo costo en entorno académico | Graviton (arm64), `db.t4g.micro`, un solo NAT, Lambdas por uso |
| RNF-13 | Mantenibilidad | Infraestructura reproducible | Todo en Terraform; CI en GitHub Actions (fmt, validate, tests) |
| RNF-14 | Seguridad IA | El agente no puede modificar datos de negocio ni ser desviado | Herramientas cerradas sin SQL libre, límites de iteraciones/tokens/longitud, usuario BD de solo lectura |

## 4. ¿Repositorio en GitHub?
Sí, conviene: versiona Terraform y código juntos, ejecuta CI (`terraform validate` + tests) en cada PR y permite despliegue con OIDC hacia AWS sin llaves estáticas. Se incluye `.github/workflows/ci.yml`. El despliegue automático (`terraform apply` desde Actions) queda fuera hasta tener un backend remoto de estado (S3 + DynamoDB).

## 5. Riesgos y decisiones
- **Pinning en RDS Proxy**: se usa `autocommit=True` y transacciones explícitas; no se usan `SET` de sesión ni tablas temporales, para no fijar conexiones y perder el multiplexado.
- **Un solo NAT**: punto único de falla para salida a Internet (agente, APIs de AWS). En producción: un NAT por AZ o VPC endpoints.
- **Lambda `bench` en modo `direct`**: única excepción a "solo vía Proxy", desactivada por defecto (`enable_direct_benchmark = false`) y con su propio SG; existe solo para la demostración.
