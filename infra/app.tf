# ---------- Mensajería ----------
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.project}-restock-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

resource "aws_sqs_queue" "restock" {
  name                       = "${var.project}-restock"
  receive_wait_time_seconds  = 20      # long polling
  visibility_timeout_seconds = 180     # 6 x timeout del consumidor (30 s)
  message_retention_seconds  = 1209600 # 14 días: cubre fines de semana y festivos
  sqs_managed_sse_enabled    = true
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.dlq.arn, maxReceiveCount = 3 })
}

resource "aws_sns_topic" "orders" {
  name              = "${var.project}-purchase-orders"
  kms_master_key_id = "alias/aws/sns"
}

# Alarmas en tópico aparte: CloudWatch no puede publicar con la clave administrada aws/sns.
resource "aws_sns_topic" "alarms" {
  name = "${var.project}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = var.alert_email == "" ? {} : { orders = aws_sns_topic.orders.arn, alarms = aws_sns_topic.alarms.arn }
  topic_arn = each.value
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------- Lambdas (un solo paquete, un rol por función) ----------
locals {
  lambda_pkg = "${path.module}/../build/lambda.zip"
  functions = {
    checker = {
      handler = "inventario.handlers.checker_handler", timeout = 60, memory = 256, db_user = "app_user"
      env     = { QUEUE_URL = aws_sqs_queue.restock.url }
    }
    consumer = {
      handler = "inventario.handlers.consumer_handler", timeout = 30, memory = 256, db_user = "app_user"
      env     = { TOPIC_ARN = aws_sns_topic.orders.arn, ALLOW_FAULT_INJECTION = tostring(var.allow_fault_injection) }
    }
    agent = {
      handler = "inventario.agent.handler", timeout = 60, memory = 512, db_user = "agent_ro"
      env     = { BEDROCK_MODEL = var.bedrock_model, BEDROCK_FALLBACK_MODEL = var.bedrock_fallback_model }
    }
    bench = {
      handler = "inventario.handlers.bench_handler", timeout = 60, memory = 256, db_user = "app_user"
      env     = { DB_INSTANCE_ENDPOINT = aws_db_instance.this.address, APP_USER_SECRET_ARN = aws_secretsmanager_secret.db["app_user"].arn }
    }
    migrate = {
      handler = "inventario.handlers.migrate_handler", timeout = 360, memory = 256, db_user = "dbadmin"
      env = {
        DB_ADMIN_USER       = "dbadmin"
        APP_USER_SECRET_ARN = aws_secretsmanager_secret.db["app_user"].arn
        AGENT_RO_SECRET_ARN = aws_secretsmanager_secret.db["agent_ro"].arn
      }
    }
  }
  extra_statements = {
    checker = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [aws_sqs_queue.restock.arn] }]
    consumer = [
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], Resource = [aws_sqs_queue.restock.arn] },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = [aws_sns_topic.orders.arn] },
    ]
    # Claude en Bedrock (endpoint bedrock-mantle). ponytail: Resource "*", restringir a los ARN de modelo cuando AWS publique el formato.
    agent   = [{ Effect = "Allow", Action = ["bedrock-mantle:CreateInference"], Resource = ["*"] }]
    bench   = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.db["app_user"].arn] }]
    migrate = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [for s in aws_secretsmanager_secret.db : s.arn] }]
  }
}

resource "aws_iam_role" "fn" {
  for_each = local.functions
  name     = "${var.project}-${each.key}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "fn_vpc" {
  for_each   = local.functions
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "fn_xray" {
  for_each   = local.functions
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.functions
  role     = aws_iam_role.fn[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{ Effect = "Allow", Action = ["rds-db:connect"], Resource = ["${local.dbuser_arn}/${each.value.db_user}"] }],
      local.extra_statements[each.key],
    )
  })
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${var.project}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${var.project}-${each.key}"
  role             = aws_iam_role.fn[each.key].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = each.value.handler
  timeout          = each.value.timeout
  memory_size      = each.value.memory
  filename         = local.lambda_pkg
  source_code_hash = filebase64sha256(local.lambda_pkg)

  vpc_config {
    subnet_ids         = module.vpc.private_subnets
    security_group_ids = [each.key == "bench" ? aws_security_group.bench.id : aws_security_group.lambda.id]
  }
  tracing_config { mode = "Active" }
  environment {
    # Solo el endpoint del Proxy; el de la instancia solo lo recibe bench.
    variables = merge({ DB_PROXY_ENDPOINT = aws_db_proxy.this.endpoint, DB_NAME = "inventory", DB_USER = each.value.db_user, BUSINESS_TZ = var.business_timezone }, each.value.env)
  }
  depends_on = [aws_cloudwatch_log_group.fn, aws_iam_role_policy_attachment.fn_vpc]
}

resource "aws_lambda_invocation" "migrate" {
  function_name = aws_lambda_function.fn["migrate"].function_name
  input         = jsonencode({ schema_sha = filesha256("${path.module}/../backend/inventario/schema.sql") })
  depends_on    = [aws_db_proxy_target.this, aws_secretsmanager_secret_version.db, aws_iam_role_policy.fn]
}

resource "aws_lambda_event_source_mapping" "consumer" {
  event_source_arn        = aws_sqs_queue.restock.arn
  function_name           = aws_lambda_function.fn["consumer"].arn
  batch_size              = 10
  function_response_types = ["ReportBatchItemFailures"] # solo los fallidos vuelven a la cola
  scaling_config {
    maximum_concurrency = 5 # protege la BD aun con Proxy
  }
  lifecycle {
    ignore_changes = [enabled] # lo gestiona el Scheduler (horario hábil)
  }
}

resource "aws_sns_topic_subscription" "agent" {
  topic_arn = aws_sns_topic.orders.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.fn["agent"].arn
}

resource "aws_lambda_permission" "agent_sns" {
  statement_id  = "AllowSNS"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn["agent"].function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.orders.arn
}

# ---------- EventBridge Scheduler: horario hábil L-V 8-18 ----------
resource "aws_iam_role" "scheduler" {
  name = "${var.project}-scheduler"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "scheduler.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.fn["checker"].arn },
      {
        Effect   = "Allow", Action = "lambda:UpdateEventSourceMapping",
        Resource = "arn:aws:lambda:${var.region}:${local.account}:event-source-mapping:${aws_lambda_event_source_mapping.consumer.uuid}"
      },
    ]
  })
}

resource "aws_scheduler_schedule" "stock_check" {
  name                         = "${var.project}-stock-check"
  schedule_expression          = "cron(0 8-17 ? * MON-FRI *)" # cada hora en punto, 08:00..17:00, lunes a viernes
  schedule_expression_timezone = var.business_timezone
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.fn["checker"].arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy { maximum_retry_attempts = 2 }
  }
}

# Fuera de horario el consumidor queda deshabilitado: las solicitudes se aceptan y esperan en SQS.
resource "aws_scheduler_schedule" "consumer_window" {
  for_each = {
    open  = { cron = "cron(0 8 ? * MON-FRI *)", enabled = true }
    close = { cron = "cron(0 18 ? * MON-FRI *)", enabled = false }
  }
  name                         = "${var.project}-consumer-${each.key}"
  schedule_expression          = each.value.cron
  schedule_expression_timezone = var.business_timezone
  flexible_time_window { mode = "OFF" }
  target {
    arn      = "arn:aws:scheduler:::aws-sdk:lambda:updateEventSourceMapping"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ UUID = aws_lambda_event_source_mapping.consumer.uuid, Enabled = each.value.enabled })
  }
}

# ---------- EC2 (NGINX + API) ----------
resource "aws_s3_bucket" "code" {
  bucket_prefix = "${var.project}-code-"
  force_destroy = true
}

locals {
  # La API de EC2 usa el mismo paquete que las Lambdas (sin agent.py, que necesita el SDK de Anthropic).
  ec2_files = merge(
    { for f in fileset("${path.module}/../backend/inventario", "*.py") : "inventario/${f}" => "../backend/inventario/${f}" if f != "agent.py" },
    { "index.html" = "../frontend/index.html" },
  )
}

resource "aws_s3_object" "code" {
  for_each = local.ec2_files
  bucket   = aws_s3_bucket.code.id
  key      = "app/${each.key}"
  source   = "${path.module}/${each.value}"
  etag     = filemd5("${path.module}/${each.value}")
}

resource "aws_iam_role" "ec2" {
  name = "${var.project}-ec2"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "ec2" {
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.code.arn}/app/*" },
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.code.arn },
      { Effect = "Allow", Action = "rds-db:connect", Resource = "${local.dbuser_arn}/app_user" },
      { Effect = "Allow", Action = "sqs:SendMessage", Resource = aws_sqs_queue.restock.arn },
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.fn["agent"].arn },
    ]
  })
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project}-ec2"
  role = aws_iam_role.ec2.name
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# ponytail: una instancia; Auto Scaling Group con min 2 en 2 AZ cuando se necesite HA del frontend.
resource "aws_instance" "web" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = "t4g.micro"
  subnet_id              = module.vpc.private_subnets[0]
  vpc_security_group_ids = [aws_security_group.ec2.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name
  metadata_options { http_tokens = "required" }
  root_block_device { encrypted = true }

  user_data = templatefile("${path.module}/user_data.sh", {
    bucket         = aws_s3_bucket.code.id
    region         = var.region
    proxy_endpoint = aws_db_proxy.this.endpoint
    queue_url      = aws_sqs_queue.restock.url
    agent_function = aws_lambda_function.fn["agent"].function_name
    timezone       = var.business_timezone
    code_version   = sha1(join("", [for o in aws_s3_object.code : o.etag]))
  })
  user_data_replace_on_change = true
  tags                        = { Name = "${var.project}-web" }
}

# ---------- ALB ----------
locals { https = var.certificate_arn != "" }

resource "aws_lb" "web" {
  name                       = var.project
  load_balancer_type         = "application"
  subnets                    = module.vpc.public_subnets
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "web" {
  name     = var.project
  port     = 80
  protocol = "HTTP"
  vpc_id   = module.vpc.vpc_id
  health_check {
    path    = "/api/health"
    matcher = "200"
  }
}

resource "aws_lb_target_group_attachment" "web" {
  target_group_arn = aws_lb_target_group.web.arn
  target_id        = aws_instance.web.id
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.web.arn
    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}
