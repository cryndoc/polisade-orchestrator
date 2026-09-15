<!-- polisade:corpus-schema COPY — канон этой схемы живёт в Polisade Reverse
     docs/corpus/ (перенесён вместе с плоскостью гейтов, WP4.1/ADR-015).
     Здесь — копия для скилла design-corpus; правки канона делай в Reverse. -->
# context-map schema (`docs/architecture/model/context-map.yaml`)

LIVING: шов между bounded-context (DDD context map). Описывает контексты и
рёбра-интеграции между ними; используется для C4 и для проверки запретных циклов.

```yaml
contexts:
  - id: ordering
    containers: [api-gateway, order-service]
  - id: payments
    containers: [payment-service]
relationships:
  - upstream: ordering
    downstream: payments
    pattern: customer-supplier        # shared-kernel | conformist | acl | open-host | published-language
    via: flows/checkout/FLOW-001
```

**Инварианты:** каждый `upstream`/`downstream` существует в `contexts[].id`;
`via` (если есть) резолвится в реальный flow/contract; **нет запретного цикла**
(напр. взаимный customer-supplier) — такой цикл → halt to PM. `containers`
резолвятся в `model/containers.yaml`.
