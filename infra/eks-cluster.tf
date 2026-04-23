terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "ap-southeast-2"
}

locals {
  cluster_name = "otel-demo-prod"
  region       = "ap-southeast-2"
  tags = {
    project = "nz-tech-rally-demo"
    owner   = "dipin"
  }
}

# VPC
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${local.cluster_name}-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["ap-southeast-2a", "ap-southeast-2b", "ap-southeast-2c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway   = true
  single_nat_gateway   = true
  enable_dns_hostnames = true

  public_subnet_tags = {
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
    "kubernetes.io/role/elb"                       = "1"
  }

  private_subnet_tags = {
    "kubernetes.io/cluster/${local.cluster_name}" = "shared"
    "kubernetes.io/role/internal-elb"              = "1"
  }

  tags = local.tags
}

# EKS Cluster
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = local.cluster_name
  cluster_version = "1.29"

  vpc_id                         = module.vpc.vpc_id
  subnet_ids                     = module.vpc.private_subnets
  cluster_endpoint_public_access = true

  cluster_addons = {
    coredns = {
      most_recent = true
    }
    kube-proxy = {
      most_recent = true
    }
    vpc-cni = {
      most_recent = true
    }
    amazon-cloudwatch-observability = {
      most_recent = true
    }
  }

  eks_managed_node_groups = {
    standard_workers = {
      name           = "standard-workers"
      instance_types = ["m5.2xlarge"]

      min_size     = 2
      max_size     = 4
      desired_size = 3

      disk_size = 100

      iam_role_additional_policies = {
        CloudWatchAgentServerPolicy = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
        AWSXRayDaemonWriteAccess    = "arn:aws:iam::aws:policy/AWSXRayDaemonWriteAccess"
      }

      labels = {
        role = "worker"
      }

      tags = local.tags
    }
  }

  tags = local.tags
}

# CloudWatch log group for Container Insights
resource "aws_cloudwatch_log_group" "container_insights" {
  name              = "/aws/containerinsights/${local.cluster_name}/performance"
  retention_in_days = 7
  tags              = local.tags
}

# Disk pressure alarm
resource "aws_cloudwatch_metric_alarm" "node_disk_pressure" {
  alarm_name          = "EKS-NodeDiskPressure-${local.cluster_name}"
  alarm_description   = "Node disk usage above 80% in ${local.cluster_name} cluster"
  metric_name         = "node_filesystem_utilization"
  namespace           = "ContainerInsights"
  statistic           = "Average"
  period              = 60
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2

  dimensions = {
    ClusterName = local.cluster_name
  }

  alarm_actions = [var.slack_sns_topic_arn]
  tags          = local.tags
}

variable "slack_sns_topic_arn" {
  description = "SNS topic ARN that posts alerts to Slack"
  type        = string
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_name" {
  value = module.eks.cluster_name
}
