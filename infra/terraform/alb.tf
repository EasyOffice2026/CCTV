# The one public entrance: https://<domain_name>. /v1 (REST, WebSocket, edge long polls) and
# /metrics go to the API, everything else to the dashboard (web). TLS ends here.

resource "aws_lb" "main" {
  name                       = local.name
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  idle_timeout               = 120 # edge long polls hold up to 55 s
  drop_invalid_header_fields = true
  enable_deletion_protection = local.production
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30
  health_check {
    path                = "/readyz" # also checks the database
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_target_group" "web" {
  name                 = "${local.name}-web"
  port                 = 8080
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 15
  health_check {
    path    = "/"
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06" # TLS 1.2 and 1.3 only
  certificate_arn   = local.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 10
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
  condition {
    path_pattern {
      values = ["/v1/*", "/metrics"]
    }
  }
}

# --- certificate and DNS -------------------------------------------------------------------

resource "aws_acm_certificate" "main" {
  count             = var.certificate_arn == "" ? 1 : 0
  domain_name       = var.domain_name
  validation_method = "DNS"
  lifecycle {
    create_before_destroy = true
  }
}

# One domain, so one validation record (known only once the certificate exists).
resource "aws_route53_record" "certificate" {
  count           = var.certificate_arn == "" ? 1 : 0
  zone_id         = var.route53_zone_id
  name            = tolist(aws_acm_certificate.main[0].domain_validation_options)[0].resource_record_name
  type            = tolist(aws_acm_certificate.main[0].domain_validation_options)[0].resource_record_type
  records         = [tolist(aws_acm_certificate.main[0].domain_validation_options)[0].resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "main" {
  count                   = var.certificate_arn == "" ? 1 : 0
  certificate_arn         = aws_acm_certificate.main[0].arn
  validation_record_fqdns = aws_route53_record.certificate[*].fqdn
}

locals {
  certificate_arn = var.certificate_arn != "" ? var.certificate_arn : aws_acm_certificate_validation.main[0].certificate_arn
}

resource "aws_route53_record" "site" {
  count   = var.route53_zone_id == "" ? 0 : 1
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}
