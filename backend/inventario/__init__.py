"""Backend del inventario con reabastecimiento automático.

Capas (dependencias en una sola dirección, todas hacia el dominio):
  presentación  handlers.py (Lambdas), api.py (HTTP), agent.py, dto.py
  negocio       services.py
  puertos       ports.py
  datos/infra   mysql_repositories.py, aws_adapters.py, db.py
  dominio       domain.py  (entidades, árbol de categorías, buffer circular)
  composición   container.py
"""
