terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.80" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Para trabajo en equipo: backend "s3" { bucket = "...", key = "inventario.tfstate", use_lockfile = true }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.project, ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "me" {}
data "aws_availability_zones" "az" { state = "available" }

locals {
  account = data.aws_caller_identity.me.account_id
  azs     = slice(data.aws_availability_zones.az.names, 0, 2)
}

# ---------- Red: 2 AZ, ALB en públicas; EC2, Lambdas, Proxy y RDS en privadas ----------
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.16"

  name               = var.project
  cidr               = "10.20.0.0/16"
  azs                = local.azs
  public_subnets     = ["10.20.0.0/24", "10.20.1.0/24"]
  private_subnets    = ["10.20.10.0/24", "10.20.11.0/24"]
  enable_nat_gateway = true
  single_nat_gateway = true # ponytail: un NAT (costo); uno por AZ en producción
}

# ---------- Security groups (aquí se impone "solo vía Proxy") ----------
resource "aws_security_group" "alb" {
  name   = "${var.project}-alb"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "ec2" {
  name   = "${var.project}-ec2"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "lambda" {
  name   = "${var.project}-lambda"
  vpc_id = module.vpc.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# SG aparte para la Lambda bench: es la única a la que se le puede abrir RDS directo.
resource "aws_security_group" "bench" {
  name   = "${var.project}-bench"
  vpc_id = module.vpc.vpc_id
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "proxy" {
  name   = "${var.project}-proxy"
  vpc_id = module.vpc.vpc_id
  ingress {
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id, aws_security_group.ec2.id, aws_security_group.bench.id]
  }
  egress {
    from_port   = 3306
    to_port     = 3306
    protocol    = "tcp"
    cidr_blocks = module.vpc.private_subnets_cidr_blocks
  }
  egress {
    description = "Secrets Manager vía NAT"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "rds" {
  name   = "${var.project}-rds"
  vpc_id = module.vpc.vpc_id
  ingress {
    description     = "Solo RDS Proxy"
    from_port       = 3306
    to_port         = 3306
    protocol        = "tcp"
    security_groups = [aws_security_group.proxy.id]
  }
}

resource "aws_vpc_security_group_ingress_rule" "rds_direct_bench" {
  count                        = var.enable_direct_benchmark ? 1 : 0
  description                  = "TEMPORAL: benchmark sin Proxy"
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.bench.id
  from_port                    = 3306
  to_port                      = 3306
  ip_protocol                  = "tcp"
}
