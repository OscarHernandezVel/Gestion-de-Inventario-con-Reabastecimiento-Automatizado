output "app_url" {
  value = "${local.https ? "https" : "http"}://${aws_lb.web.dns_name}"
}

output "proxy_endpoint" {
  value = aws_db_proxy.this.endpoint
}

output "queue_url" {
  value = aws_sqs_queue.restock.url
}

output "consumer_mapping_uuid" {
  value = aws_lambda_event_source_mapping.consumer.uuid
}

output "dashboard_url" {
  value = "https://${var.region}.console.aws.amazon.com/cloudwatch/home?region=${var.region}#dashboards:name=${var.project}"
}
