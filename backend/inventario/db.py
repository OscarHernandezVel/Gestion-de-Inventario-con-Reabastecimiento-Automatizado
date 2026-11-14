"""Conexión a MySQL a través de RDS Proxy (IAM auth + TLS). Infraestructura pura, sin reglas de negocio.

No hay contraseñas en variables de entorno: el token IAM se firma localmente y dura 15 minutos.
"""
import logging
import os
import ssl

import boto3
import pymysql
import pymysql.cursors

log = logging.getLogger(__name__)
REGION = os.environ.get("AWS_REGION", "us-east-1")
DB_NAME = os.environ.get("DB_NAME", "inventory")
PORT = 3306

_rds = boto3.client("rds", region_name=REGION)
_conn = None


def connect(user=None, host=None, password=None, verify_tls=True):
    """Abre una conexión. Sin `password` usa un token IAM (camino normal vía Proxy)."""
    user = user or os.environ["DB_USER"]
    host = host or os.environ["DB_PROXY_ENDPOINT"]
    if password is None:
        password = _rds.generate_db_auth_token(DBHostname=host, Port=PORT, DBUsername=user, Region=REGION)
    tls = ssl.create_default_context()  # el Proxy usa certificados ACM: basta el bundle del sistema
    if not verify_tls:  # solo el benchmark directo a RDS (CA propia de RDS)
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
    # autocommit=True + START TRANSACTION explícito: evita SET de sesión que fijan (pin) la conexión en el Proxy.
    return pymysql.connect(
        host=host, port=PORT, user=user, password=password, database=DB_NAME,
        ssl=tls, autocommit=True, connect_timeout=5, read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_conn():
    """Conexión reutilizada entre invocaciones del mismo contenedor Lambda."""
    global _conn
    if _conn is not None:
        try:
            _conn.ping(reconnect=False)  # reconnect=True reusaría un token IAM vencido
            return _conn
        except pymysql.Error:
            _conn = None
    _conn = connect()
    return _conn
