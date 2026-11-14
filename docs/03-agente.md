# Agente de inventario (Claude en Amazon Bedrock)

Implementación: [`backend/inventario/agent.py`](../backend/inventario/agent.py) — Lambda `inventario-agent`, Python, SDK oficial `anthropic[bedrock]` (cliente `AnthropicBedrockMantle`, endpoint `bedrock-mantle`) con el *tool runner*.
La configuración vive en un solo lugar: `AGENT_CONFIG` + `SYSTEM_PROMPT` en ese archivo.

## Rol
Analista de inventario en tiempo real. Lee stock, solicitudes y órdenes; explica el estado a gerentes y
registra alertas cuando detecta riesgos. **No ejecuta cambios de negocio** (no crea, edita ni borra órdenes o productos).

## Disparadores (tiempo real)
| Evento | Origen | Herramientas habilitadas |
|---|---|---|
| Nueva orden de compra | Suscripción al tópico SNS `purchase-orders` (segundos después del COMMIT) | lectura + `record_insight` |
| Pregunta del gerente | `POST /api/agent` en EC2 → `lambda:Invoke` | solo lectura |

Las preguntas de usuarios nunca reciben la herramienta de escritura, así un intento de *prompt injection* no puede generar alertas falsas.

## Herramientas
| Herramienta | Tipo | Qué hace |
|---|---|---|
| `get_inventory(only_low_stock)` | lectura | Stock, umbral, pendientes, consumo por hora, horas hasta agotarse y cantidad sugerida |
| `get_category_summary()` | lectura | Árbol de categorías con stock, alertas y consumo agregados por nivel |
| `get_recent_orders(limit)` | lectura | Solicitudes recientes con su OC y estado (EN COLA / CREADA) |
| `get_recent_insights(limit)` | lectura | Alertas previas, para no repetirlas |
| `record_insight(severity, summary)` | escritura acotada | Inserta en `agent_insights` (INFO/MEDIA/ALTA, ≤ 500 car.) |

No existe una herramienta de SQL libre: cada consulta está escrita en el código y parametrizada.

## Límites
| Límite | Valor | Dónde se aplica |
|---|---|---|
| Modelo | `anthropic.claude-haiku-5-5` (variable `bedrock_model`); respaldo `anthropic.claude-sonnet-5` (`bedrock_fallback_model`) | Terraform → env |
| Esfuerzo | `low` (consultas cortas, baja latencia; Haiku 5.5 usa `medium` por defecto) | `output_config.effort` |
| Tokens de salida por respuesta | 4096 | `max_tokens` |
| Iteraciones modelo↔herramienta | 6 | `max_iterations` del tool runner |
| Longitud de pregunta | 1000 caracteres | handler + `maxlength` en el frontend |
| Filas por herramienta | 50 (alertas: 20) | cada herramienta |
| Timeout HTTP a Bedrock | 45 s, 2 reintentos | cliente `AnthropicBedrockMantle` |
| Timeout Lambda | 60 s | Terraform |
| Permisos BD | usuario `agent_ro`: `SELECT` en `inventory.*`, `INSERT` solo en `agent_insights` | `migrate_handler` |
| Permisos AWS | `rds-db:connect` como `agent_ro` + `bedrock-mantle:CreateInference`. Nada más | rol IAM `inventario-agent` |

## Características
- **Datos, no memoria**: cada afirmación sale de una herramienta en esa misma conversación.
- **Contenido externo como datos**: el mensaje SNS va dentro de `<orden>…</orden>` y el prompt del sistema indica que nunca son instrucciones.
- **Refusal fallback**: Bedrock no soporta el parámetro `fallbacks` del servidor, así que se usa el middleware del SDK `BetaRefusalFallbackMiddleware`: si un clasificador rechaza, el cliente reintenta con `bedrock_fallback_model`. Si el resultado final es `refusal`, responde "No puedo ayudar con esa solicitud".
- **Observabilidad**: cada ejecución registra `stop_reason` y `usage` (tokens) en CloudWatch Logs; X-Ray traza la Lambda.
- **Errores**: `RateLimitError` → "agente ocupado"; `APIConnectionError` → "no se pudo contactar"; el resto falla la Lambda y dispara la alarma `inventario-agent-errors`.
- **Sin secretos**: no hay API key. El SDK firma cada petición con SigV4 usando el rol IAM de la Lambda; la facturación y los logs (CloudWatch/CloudTrail) quedan en la cuenta AWS.
- **Red**: la Lambda está en subred privada y llega a `bedrock-mantle.<región>.api.aws` por el NAT.

## Habilitar el modelo
Antes del primer uso, en la consola de Amazon Bedrock → *Model access*, solicita acceso a Claude Haiku 5.5
(Claude Sonnet 5, el respaldo, está abierto para todas las cuentas). La región (`us-east-1` por defecto) debe ofrecer `bedrock-mantle`.
