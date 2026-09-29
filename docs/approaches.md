# Abordagens por stack

Este projeto usa workloads equivalentes onde isso ajuda a comparar, mas preserva as primitivas idiomáticas de cada runtime. Os endpoints são uma bancada didática local, não um desenho de produção completo.

| Stack | Fila / fluxo | Concorrência e downstream | Rate limit | Ponto a observar |
|---|---|---|---|---|
| Go | `chan Job` buffered | worker pool fixo e channel-semaphore | token bucket `x/time/rate` | `select` com `default` implementa rejeição sem bloquear o handler |
| Rust | `tokio::sync::mpsc` bounded | worker tasks fixas e `Semaphore` | token bucket didático | `try_send` permite rejeitar quando o canal está cheio; `buffer_unordered(n)` limita lotes |
| Node.js | `p-queue` com máximo de espera | `p-queue` compartilhada pelos jobs e `/sync` | `@fastify/rate-limit` (janela fixa) | promises não limitam concorrência sozinhas; `/cpu` bloqueia intencionalmente o event loop |
| Elixir | GenStage com buffer explícito e demanda | consumidores supervisionados e limite ETS de downstream | contador de janela fixa ETS | mailboxes OTP são unbounded por padrão; a reserva atômica ocorre antes de publicar no producer |

## API de teste

Todas as aplicações oferecem health, fila assíncrona, trabalho síncrono, CPU-bound, rate limit, batch e métricas. `/stream` usa produção lazy e escrita incremental para observar fluxo sem materializar a resposta inteira.

- `POST /jobs?tenant=alpha&failures=2`: tenta aceitar um job para um tenant permitido. `failures` injeta falhas transientes antes do sucesso; exceder `MAX_RETRIES` registra falha final.
- `GET /sync`: tenta obter um slot de downstream imediatamente; sem slot, responde `503`.
- `GET /stream?items=100&delay_ms=10`: emite itens aos poucos; o servidor não prepara uma lista/resposta completa em memória.
- `GET /metrics`: contadores e gauges locais ao processo; não são métricas Prometheus nem estado distribuído.

## Como os limites se relacionam

- A fila mede jobs **aguardando**; workers ativos são contados separadamente.
- `DOWNSTREAM_CONCURRENCY` limita chamadas simuladas ao recurso lento; pode ser menor que o número de workers.
- `TENANT_OUTSTANDING_LIMIT=0` desativa o bulkhead por tenant. Valor positivo limita o total em espera + processamento por tenant (alpha, beta ou default), sem criar labels arbitrárias.
- `MAX_RETRIES` limita retries após a tentativa inicial. O backoff é exponencial e inclui jitter; retries consomem tempo e o mesmo orçamento de timeout do job.
- Timeout de uma espera local cancela o trabalho local. Um sistema remoto pode continuar processando se sua chamada não suportar cancelamento.

## Elixir e GenStage

GenStage encaminha eventos segundo a demanda dos consumers. Cada consumer processa eventos sincronamente em seu callback; múltiplos consumers fornecem concorrência sem iniciar uma task ilimitada por evento. `max_demand` e `min_demand` controlam eventos em trânsito.

O stage tem `buffer_size` explícito. Antes de `GenStage.cast`, uma reserva ETS atômica limita o total de trabalho outstanding a `QUEUE_CAPACITY + WORKERS`; isso impede que uma enxurrada de casts simplesmente transfira a fila ilimitada para a mailbox do producer. Ao terminar o job, a reserva é liberada.

Broadway não é dependência deste exemplo. É uma extensão possível para integrar brokers, acknowledgements, batchers e políticas de retry durável.

## Streaming, retry e fairness

Use `/stream` para ver produtores e consumidores em velocidades diferentes. Use `POST /jobs?failures=N` para observar contagem de retries e falha final. Use `TENANT_OUTSTANDING_LIMIT=2` e carga em `tenant=alpha` seguida de uma requisição para `tenant=beta` para exercitar isolamento. A fila continua FIFO global; o limite por tenant impede monopolização, mas não implementa uma agenda fair/weighted.

## Durabilidade

Filas locais são voláteis. Para webhooks/jobs de produção, persistir antes de confirmar, usar idempotency key, outbox/broker durável, consumidores com concorrência limitada e política explícita de retry/dead-letter. Ver também [`scenarios.md`](scenarios.md).
