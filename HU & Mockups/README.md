# Historias de usuario — Inventario con Reabastecimiento Automático

Roles: **Encargado de bodega** (opera el inventario), **Comprador** (recibe órdenes de compra), **Administrador de plataforma** (opera AWS), **Gerente de operaciones** (consulta al agente).

Cada historia tiene su carpeta con la historia (`.md`), el mockup interactivo (`.html`, CSS + JS sin dependencias; la barra superior navega entre pantallas) y la imagen (`.jpg`).

| # | Historia | Rol | Puntos | Prioridad |
|---|----------|-----|--------|-----------|
| [HU-01](HU-01-consultar-stock/HU-01.md) | Consultar niveles de stock | Encargado de bodega | 2 | Alta |
| [HU-02](HU-02-reabastecimiento-automatico/HU-02.md) | Reabastecer automáticamente en horario hábil | Encargado de bodega | 3 | Alta |
| [HU-03](HU-03-reabastecimiento-manual/HU-03.md) | Solicitar reabastecimiento manual en cualquier horario | Encargado de bodega | 3 | Media |
| [HU-04](HU-04-orden-de-compra-atomica/HU-04.md) | Generar orden de compra atómica y notificar | Comprador | 5 | Alta |
| [HU-05](HU-05-rds-proxy-concurrencia/HU-05.md) | Proteger la BD bajo concurrencia (RDS Proxy) | Administrador | 5 | Alta |
| [HU-06](HU-06-agente-inventario/HU-06.md) | Consultar al agente de inventario | Gerente de operaciones | 5 | Media |
