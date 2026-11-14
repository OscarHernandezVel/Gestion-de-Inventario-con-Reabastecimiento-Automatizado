# ---------- RDS MySQL ----------
resource "aws_db_subnet_group" "this" {
  name       = var.project
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "this" {
  identifier                  = var.project
  engine                      = "mysql"
  engine_version              = "8.0"
  instance_class              = var.db_instance_class
  allocated_storage           = 20
  storage_type                = "gp3"
  storage_encrypted           = true
  db_name                     = "inventory"
  username                    = "dbadmin"
  manage_master_user_password = true # contraseña maestra en Secrets Manager, nunca en el estado
  db_subnet_group_name        = aws_db_subnet_group.this.name
  vpc_security_group_ids      = [aws_security_group.rds.id]
  multi_az                    = var.multi_az
  backup_retention_period     = 7
  publicly_accessible         = false
  skip_final_snapshot         = true # ponytail: entorno académico; false + final_snapshot_identifier en producción
  apply_immediately           = true
}

# ---------- Usuarios de aplicación (contraseña solo la usa el Proxy) ----------
locals {
  db_users = { app_user = "app_user", agent_ro = "agent_ro" }
}

resource "random_password" "db" {
  for_each = local.db_users
  length   = 32
  special  = false
}

resource "aws_secretsmanager_secret" "db" {
  for_each                = local.db_users
  name                    = "${var.project}/db/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "db" {
  for_each      = local.db_users
  secret_id     = aws_secretsmanager_secret.db[each.key].id
  secret_string = jsonencode({ username = each.value, password = random_password.db[each.key].result })
}

# ---------- RDS Proxy ----------
resource "aws_iam_role" "proxy" {
  name = "${var.project}-rds-proxy"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "rds.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

locals {
  proxy_secret_arns = concat(
    [aws_db_instance.this.master_user_secret[0].secret_arn],
    [for s in aws_secretsmanager_secret.db : s.arn],
  )
}

resource "aws_iam_role_policy" "proxy" {
  role = aws_iam_role.proxy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = local.proxy_secret_arns },
      {
        Effect    = "Allow", Action = "kms:Decrypt", Resource = "*",
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_db_proxy" "this" {
  name                   = var.project
  engine_family          = "MYSQL"
  role_arn               = aws_iam_role.proxy.arn
  vpc_subnet_ids         = module.vpc.private_subnets
  vpc_security_group_ids = [aws_security_group.proxy.id]
  require_tls            = true
  idle_client_timeout    = 1800

  dynamic "auth" {
    for_each = local.proxy_secret_arns
    content {
      auth_scheme = "SECRETS"
      iam_auth    = "REQUIRED" # los clientes solo entran con token IAM
      secret_arn  = auth.value
    }
  }
}

resource "aws_db_proxy_default_target_group" "this" {
  db_proxy_name = aws_db_proxy.this.name
  connection_pool_config {
    max_connections_percent      = 90
    max_idle_connections_percent = 50
    connection_borrow_timeout    = 120
  }
}

resource "aws_db_proxy_target" "this" {
  db_proxy_name          = aws_db_proxy.this.name
  target_group_name      = aws_db_proxy_default_target_group.this.name
  db_instance_identifier = aws_db_instance.this.identifier
}

locals {
  # arn:aws:rds:region:acct:db-proxy:prx-xxxx -> prx-xxxx (recurso usado en rds-db:connect)
  proxy_resource_id = element(split(":", aws_db_proxy.this.arn), 6)
  dbuser_arn        = "arn:aws:rds-db:${var.region}:${local.account}:dbuser:${local.proxy_resource_id}"
}

