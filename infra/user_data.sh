#!/bin/bash
# Arranque de EC2 (AL2023): NGINX sirve el frontend y hace proxy de /api a la API Python.
# code_version=${code_version} (cambia => instancia reemplazada con el código nuevo)
set -euxo pipefail

dnf install -y nginx python3.11 python3.11-pip
python3.11 -m pip install pymysql boto3

mkdir -p /opt/inventario /usr/share/nginx/html
aws s3 cp s3://${bucket}/app/ /opt/inventario/ --recursive --region ${region}
mv /opt/inventario/index.html /usr/share/nginx/html/index.html
useradd --system --no-create-home inventario || true

cat > /etc/inventario.env <<EOF
AWS_REGION=${region}
DB_PROXY_ENDPOINT=${proxy_endpoint}
DB_NAME=inventory
DB_USER=app_user
QUEUE_URL=${queue_url}
AGENT_FUNCTION=${agent_function}
BUSINESS_TZ=${timezone}
EOF

cat > /etc/systemd/system/inventario-api.service <<'EOF'
[Unit]
Description=API de inventario
After=network-online.target

[Service]
User=inventario
EnvironmentFile=/etc/inventario.env
WorkingDirectory=/opt/inventario
ExecStart=/usr/bin/python3.11 -m inventario.api
Restart=always
NoNewPrivileges=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/conf.d/inventario.conf <<'EOF'
server {
    listen 80 default_server;
    server_tokens off;
    root /usr/share/nginx/html;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Content-Security-Policy "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'" always;
    client_max_body_size 16k;

    location / { try_files $uri /index.html; }
    location /api/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 70s;
    }
}
EOF
systemctl daemon-reload
systemctl enable --now inventario-api nginx
