# Cenários e interpretação

## Fila local bounded

`POST /jobs` aceita um job e responde `202 Accepted` enquanto a fila local tem espaço. Se estiver cheia, responde `429 Too Many Requests` imediatamente. Workers fixos drenam a fila; um limite de downstream independente protege o recurso lento.

Capacidade da fila conta trabalho esperando. Workers em execução são medidos separadamente. Isso evita confundir “quantos jobs ainda aguardam” com “quantos jobs estão sendo processados”.

## Concorrência e shed load

`GET /sync` tenta adquirir um slot de downstream sem esperar. Se todos os slots estão ocupados, rejeita com `503 Service Unavailable`. Isso demonstra um bulkhead pequeno: uma operação saturada não cria uma fila de espera ilimitada dentro do handler.

`GET /jobs` tem semântica diferente: pode aceitar trabalho assíncrono limitado e o cliente recebe um identificador. Quando acaba a capacidade disponível, rejeita. Use `429` para pressão gerada pelo cliente/limite de entrada e `503` para indisponibilidade ou saturação temporária do serviço/dependência.

## Rate limit

`GET /limited` separa limite por tempo de limite de concorrência:

- Go aplica token bucket com `golang.org/x/time/rate`.
- Rust implementa um token bucket pequeno para fins didáticos.
- Node usa `@fastify/rate-limit` com uma janela de um segundo.

Os três endpoints são globais por processo, não por usuário/IP distribuído. Em múltiplas instâncias, rate limit por identidade normalmente precisa de estado compartilhado ou ser aplicado no gateway.

## Lotes

`GET /batch?items=N&concurrency=M` limita `items` e o número de operações em paralelo. Go adquire slot antes de iniciar cada goroutine; Rust usa stream com `buffer_unordered(M)`; Node usa `p-limit`. Existe um máximo explícito para a quantidade total de itens, evitando que o endpoint de demonstração vire uma fonte de alocação ilimitada.

## Downstream lento e timeout

`PROCESS_DELAY_MS` controla uma espera artificial, usada como stand-in para I/O lento. `JOB_TIMEOUT_MS` limita o tempo permitido por trabalho. Em código real, timeout deve propagar cancelamento até o cliente downstream sempre que a API permitir; abandonar apenas a espera do resultado pode deixar operação remota ativa.

## CPU-bound

`GET /cpu?ms=N` consome CPU por uma duração limitada, útil para testar profiler e saturação:

- Go verifica cancelamento durante o loop e usa o semáforo downstream.
- Rust faz yield periodicamente para não monopolizar um worker do runtime, respeitando o semáforo.
- Node bloqueia o event loop intencionalmente para expor a consequência de CPU síncrona. Em produção, CPU-heavy work deve sair do event loop principal, por exemplo para worker threads ou workers externos.

## Webhook persistido

Fluxo de referência para integração durável:

```text
receber webhook → validar assinatura → INSERT idempotente do evento bruto
    → confirmar recebimento → outbox/broker → consumidor com concorrência limitada
```

Não confirme como durável um evento que existe somente numa fila em memória. Duplicatas são esperadas em webhooks e consumidores devem ser idempotentes.

## PDF / job durável

Fluxo de referência:

```text
API → persistir job + idempotency key → outbox/broker
    → consumer bounded → renderizar → armazenar arquivo → atualizar status
```

Broker absorve burst entre processos, mas não remove a necessidade de limitar consumidores e chamadas downstream. Retry precisa de limite/backoff e falhas persistentes precisam de política de dead-letter/reprocessamento.

## Métricas

`GET /metrics` retorna JSON didático com:

- `queue_depth` e `queue_capacity`;
- `workers` e `jobs_in_flight`;
- `downstream_in_use` e `downstream_concurrency`;
- contadores enqueued, rejected, processed e failed.

Estas métricas são locais ao processo e não substituem Prometheus/OpenTelemetry em produção.
