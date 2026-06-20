# Deployment View Guide

Deployment view (arc42 раздел 7) показывает где и как развёрнуты containers — environments, regions, networks, dependencies. Это **arc42** уровень, не C4 (хотя они дополняют друг друга).

Mermaid не имеет специального deployment-diagram type — используй `flowchart` с подграфами (subgraphs) для environments/regions.

## Когда нужен

Создавай ТОЛЬКО если PRD/SPEC явно описывает infrastructure complexity:
- Multi-region / multi-AZ
- HA / failover requirements (`99.9% uptime`, `99.99%`)
- Specific cloud (AWS, GCP, Azure) с named services
- k8s cluster topology
- CDN / edge locations
- Hybrid cloud / on-premise + cloud

**НЕ создавай** для standard "развернём в Docker контейнере" — это уже C4 Container уровень.

## Frontmatter

```yaml
---
type: deployment-view
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [NFR-001, NFR-003, NFR-005]  # обычно NFRs (availability, scalability, portability)
---
```

## Полный template файла `deployment.md`

```markdown
---
type: deployment-view
parent: DESIGN-001
created: 2026-04-07
realizes_requirements: [NFR-001, NFR-003, NFR-005]  # обычно NFRs (availability, scalability, portability)
---

# Deployment — {system name}

## Topology

​```mermaid
flowchart TB
  subgraph Internet
    User[User Browser]
    OAuth[OAuth Provider]
  end

  subgraph CloudFlare["CloudFlare CDN"]
    CDN[Static assets cache]
    WAF[WAF / DDoS]
  end

  subgraph AWS["AWS — us-east-1 (primary)"]
    subgraph PublicSubnet["Public Subnet"]
      ALB[Application Load Balancer]
    end

    subgraph PrivateSubnet["Private Subnet"]
      subgraph EKS["EKS Cluster"]
        WebPods["Web App Pods (×3)"]
        ApiPods["API Pods (×4)"]
        WorkerPods["Worker Pods (×2)"]
      end

      RDS[(RDS PostgreSQL Multi-AZ)]
      ElastiCache[(ElastiCache Redis Cluster)]
      MQ[(Amazon MQ — RabbitMQ)]
    end

    S3[(S3 — user uploads)]
  end

  subgraph AWS_DR["AWS — us-west-2 (DR)"]
    RDS_DR[(RDS Read Replica)]
    S3_DR[(S3 Cross-region replica)]
  end

  User --> CDN
  CDN --> WAF
  WAF --> ALB
  ALB --> WebPods
  WebPods --> ApiPods
  ApiPods --> RDS
  ApiPods --> ElastiCache
  ApiPods --> MQ
  ApiPods --> S3
  ApiPods --> OAuth
  WorkerPods --> MQ
  WorkerPods --> RDS

  RDS -.->|Streaming replication| RDS_DR
  S3 -.->|CRR| S3_DR
​```

## Environments

| Env | Purpose | URL | Region | Scale |
|---|---|---|---|---|
| Production | Live traffic | app.example.com | us-east-1 | 3 web, 4 api, 2 worker |
| Staging | Pre-prod testing | staging.example.com | us-east-1 | 1 of each |
| DR | Disaster recovery | (failover only) | us-west-2 | RDS replica + S3 only |
| Local | Developer machines | localhost | — | docker-compose |

## Components

### CloudFlare (Edge)
- **WAF**: rate limiting, OWASP Top 10 rules
- **CDN**: статика (`/assets/*`) — TTL 1 day; HTML — no-cache
- **DDoS**: автоматическая Layer 3/4/7 защита

### AWS Application Load Balancer
- HTTPS termination (ACM cert)
- Sticky sessions: НЕТ (stateless API)
- Health check: `GET /healthz` каждые 10 сек
- Idle timeout: 60 сек

### EKS Cluster
- **Node groups**:
  - `web-nodes`: 3× t3.medium, autoscale 3-10
  - `api-nodes`: 4× t3.large, autoscale 4-20
  - `worker-nodes`: 2× t3.medium, autoscale 2-5
- **Helm chart**: `charts/auth-system/`
- **Secrets**: AWS Secrets Manager → ExternalSecrets operator
- **Logs**: stdout → CloudWatch Logs → Datadog
- **Metrics**: Prometheus + Grafana (managed)

### RDS PostgreSQL
- Engine: PostgreSQL 15.4
- Instance: `db.r6g.xlarge`
- Multi-AZ: enabled (sync replication внутри region)
- Read replica: us-west-2 (async, для DR)
- Backups: daily, 30-day retention
- PITR: enabled

### ElastiCache Redis
- Engine: Redis 7.1
- Node type: `cache.r6g.large`
- Cluster mode: enabled (3 shards × 2 replicas)
- Failover: automatic

### Amazon MQ (RabbitMQ)
- Mode: cluster (2 brokers)
- Storage: EBS gp3
- Queues: persistent with TTL

## Networks & Security

### VPC
- **CIDR**: 10.0.0.0/16
- **Subnets**:
  - Public: 10.0.1.0/24, 10.0.2.0/24 (2 AZ) — только ALB
  - Private: 10.0.10.0/24, 10.0.11.0/24 (2 AZ) — EKS, RDS, Cache, MQ
- **NAT Gateway**: для outbound traffic из private subnets
- **VPC Endpoints**: S3 (gateway), Secrets Manager, ECR (interface)

### Security Groups
- `alb-sg`: ingress 443 from 0.0.0.0/0
- `eks-sg`: ingress 443/8080 from `alb-sg`
- `rds-sg`: ingress 5432 from `eks-sg`
- `cache-sg`: ingress 6379 from `eks-sg`
- `mq-sg`: ingress 5671 from `eks-sg`

### Egress
- API/Worker → OAuth Provider: HTTPS 443 (через NAT)
- API → S3: через VPC endpoint (без NAT)

## NFRs

| Requirement | Target | Mechanism |
|---|---|---|
| Uptime SLA | 99.9% | Multi-AZ RDS, EKS HA, ALB |
| RTO (Recovery Time Objective) | 15 min | Auto-failover RDS, manual failover EKS |
| RPO (Recovery Point Objective) | 5 min | Sync replication Multi-AZ, 5-min S3 CRR |
| P95 latency | < 200ms | Read replicas, Redis cache, CDN |
| Throughput | 1000 req/s | Horizontal scale of api-nodes |
| Disaster Recovery | RTO 1 hour | RDS read replica promote in us-west-2 |

## Failover scenarios

### Single AZ failure
- Multi-AZ RDS auto-failover (~60 sec)
- EKS reschedules pods на здоровые ноды
- ALB исключает unhealthy targets
- **Recovery**: автоматическая, < 5 min

### Region failure (DR)
- Manual decision (chaos cost vs business cost)
- Promote RDS read replica в us-west-2
- Update Route 53 → DR endpoint
- Spin up EKS cluster в us-west-2 from Helm chart
- **Recovery**: ~ 1 hour

### Database corruption
- Restore from PITR (point-in-time recovery)
- Choose timestamp before corruption event
- **Recovery**: 30-60 min

## Deployment process

1. CI builds Docker images → push to ECR (tagged by git SHA)
2. CD updates Helm values with new image tag
3. ArgoCD syncs Helm chart to EKS
4. Rolling deployment: max 25% pods unavailable
5. Health check passes → traffic shifts
6. Smoke test job runs synthetic transaction
7. On failure → automatic rollback (Helm revision)

## Cost overview

| Component | Monthly cost (USD, est.) |
|---|---|
| EKS cluster | $300 |
| Compute (3+4+2 nodes) | $450 |
| RDS Multi-AZ | $400 |
| ElastiCache cluster | $300 |
| Amazon MQ | $200 |
| ALB + NAT + VPC endpoints | $80 |
| S3 + transfer | $50 |
| CloudFlare Pro | $20 |
| **Total** | **~$1,800/month** |

(Production. Staging ~30% этой суммы.)
```

## Mermaid flowchart cheatsheet (для deployment)

| Element | Syntax |
|---|---|
| Direction | `flowchart TB` (top-bottom) / `LR` / `RL` / `BT` |
| Node | `Alias[Display]` |
| Database shape | `Alias[(Database)]` |
| Round shape | `Alias((Cache))` |
| Subgraph | `subgraph Name["Display"] ... end` |
| Solid arrow | `A --> B` |
| Dashed arrow | `A -.-> B` |
| Labeled arrow | `A -->\|label\| B` |
| Thick arrow | `A ==> B` |

## Принципы

1. **Subgraphs для environments / regions / networks** — визуальная группировка
2. **Separate diagrams для different concerns**: основной (containers + storage) и опционально network (VPC + subnets + SGs)
3. **Tables после диаграммы** для деталей (instance types, scaling rules, costs)
4. **NFRs explicit** — каждое NFR имеет mechanism (как именно достигаем)
5. **Failover scenarios** — табличка "что если упадёт X" → "как восстанавливаемся"
6. **Cost** — даже грубая оценка полезна для планирования

## Critical: container name consistency

Containers в deployment.md ДОЛЖНЫ совпадать с container labels в C4 Container Diagram. Если в `c4-container.md` это "API" — в deployment subgraph для EKS пиши `ApiPods["API Pods (×4)"]`, не `BackendPods` или `ServerPods`.

## References

- Mermaid Flowchart syntax (used for deployment diagrams): https://mermaid.js.org/syntax/flowchart.html
- arc42 §7 Deployment View: https://docs.arc42.org/section-7/
- UML 2.5 Deployment Diagrams: https://www.omg.org/spec/UML/2.5.1/
