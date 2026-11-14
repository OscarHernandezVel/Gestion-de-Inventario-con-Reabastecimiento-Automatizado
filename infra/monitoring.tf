locals {
  rds_id = aws_db_instance.this.identifier
  proxy  = aws_db_proxy.this.name
  widget = { type = "metric", width = 12, height = 6 }
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.project
  dashboard_body = jsonencode({
    widgets = [
      merge(local.widget, { properties = {
        title = "Conexiones: clientes vs. RDS (con Proxy)", region = var.region, stat = "Maximum", period = 60,
        metrics = [
          ["AWS/RDS", "ClientConnections", "ProxyName", local.proxy, { label = "Proxy - conexiones cliente" }],
          ["AWS/RDS", "DatabaseConnections", "ProxyName", local.proxy, { label = "Proxy - conexiones a BD" }],
          ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", local.rds_id, { label = "Instancia RDS - total" }],
        ]
      } }),
      merge(local.widget, { properties = {
        title = "QueryDuration (ms): proxy vs. direct", region = var.region, stat = "p90", period = 60,
        metrics = [
          ["Inventario/Bench", "QueryDuration", "Mode", "proxy"],
          ["Inventario/Bench", "QueryDuration", "Mode", "direct"],
          ["AWS/RDS", "QueryDatabaseResponseLatency", "ProxyName", local.proxy, { stat = "Average", label = "Proxy - latencia BD" }],
        ]
      } }),
      merge(local.widget, { properties = {
        title = "Conexión (ms) y errores de conexión", region = var.region, period = 60,
        metrics = [
          ["Inventario/Bench", "ConnectDuration", "Mode", "proxy", { stat = "p90" }],
          ["Inventario/Bench", "ConnectDuration", "Mode", "direct", { stat = "p90" }],
          ["Inventario/Bench", "ConnectionErrors", "Mode", "direct", { stat = "Sum", yAxis = "right" }],
          ["Inventario/Bench", "ConnectionErrors", "Mode", "proxy", { stat = "Sum", yAxis = "right" }],
        ]
      } }),
      merge(local.widget, { properties = {
        title = "Colas de reabastecimiento", region = var.region, stat = "Maximum", period = 300,
        metrics = [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.restock.name],
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.dlq.name],
        ]
      } }),
      merge(local.widget, { properties = {
        title   = "Errores Lambda", region = var.region, stat = "Sum", period = 300,
        metrics = [for k in keys(local.functions) : ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-${k}"]]
      } }),
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq" {
  alarm_name          = "${var.project}-dlq-not-empty"
  alarm_description   = "Solicitudes de reabastecimiento que fallaron 3 veces"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each            = toset(["checker", "consumer", "agent"])
  alarm_name          = "${var.project}-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = "${var.project}-${each.key}" }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
