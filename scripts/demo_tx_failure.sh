#!/usr/bin/env bash
# Demuestra (1) consistencia tras un fallo a mitad de la transacción y
#           (2) que en un batch solo el ítem fallido vuelve a la cola.
# Requiere: terraform apply hecho, allow_fault_injection=true, aws cli, jq, curl.
set -euo pipefail
cd "$(dirname "$0")/../infra"
APP=$(terraform output -raw app_url)
UUID=$(terraform output -raw consumer_mapping_uuid)
FN=inventario-consumer

snapshot() {
  echo "--- $1"
  curl -s "$APP/api/products" | jq -r '.[] | select(.sku=="SKU-001" or .sku=="SKU-003") | "\(.sku) pending_orders=\(.pending_orders)"'
  curl -s "$APP/api/orders" | jq -r '.[:4][] | "  \(.sku) qty=\(.qty) status=\(.status) po=\(.po_id)"'
}

echo "1) Pauso el consumidor (como fuera de horario) para controlar qué se procesa"
aws lambda update-event-source-mapping --uuid "$UUID" --no-enabled >/dev/null

echo "2) Encolo dos solicitudes manuales"
R1=$(curl -s -X POST "$APP/api/products/1/restock" -H 'Content-Type: application/json' -d '{"qty":10}' | jq -r .request_id)
R2=$(curl -s -X POST "$APP/api/products/3/restock" -H 'Content-Type: application/json' -d '{"qty":20}' | jq -r .request_id)
snapshot "ANTES (ambas EN COLA, pending_orders=1)"

echo "3) Invoco el consumidor con un batch: R1 normal, R2 con simulate_failure"
cat > /tmp/batch.json <<EOF
{"Records":[
 {"messageId":"ok-1","body":"{\"request_id\":\"$R1\"}"},
 {"messageId":"fail-2","body":"{\"request_id\":\"$R2\",\"simulate_failure\":true}"}
]}
EOF
aws lambda invoke --function-name "$FN" --cli-binary-format raw-in-base64-out \
  --payload file:///tmp/batch.json /tmp/out.json >/dev/null
echo "Respuesta del consumidor:"; jq . /tmp/out.json
snapshot "DESPUÉS: SKU-001 con OC y pending=0; SKU-003 sigue EN COLA con pending=1 (ROLLBACK)"

echo "4) Reactivo el consumidor: los mensajes reales de SQS se procesan; R1 se ignora (idempotente) y R2 crea su OC"
aws lambda update-event-source-mapping --uuid "$UUID" --enabled >/dev/null
sleep 60
snapshot "FINAL"
echo "Logs: aws logs tail /aws/lambda/$FN --since 10m"
