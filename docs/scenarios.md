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
- Elixir usa um contador ETS atômico em uma janela fixa de um segundo.

Os quatro endpoints são globais por processo, não por usuário/IP distribuído. Em múltiplas instâncias, rate limit por identidade normalmente precisa de estado compartilhado ou ser aplicado no gateway.

## Lotes

`GET /batch?items=N&concurrency=M` limita `items` e o número de operações em paralelo. Go adquire slot antes de iniciar cada goroutine; Rust usa stream com `buffer_unordered(M)`; Node usa `p-limit`. Existe um máximo explícito para a quantidade total de itens, evitando que o endpoint de demonstração vire uma fonte de alocação ilimitada.

## Downstream lento e timeout

`PROCESS_DELAY_MS` controla uma espera artificial, usada como stand-in para I/O lento. `JOB_TIMEOUT_MS` limita o tempo permitido por trabalho. Em código real, timeout deve propagar cancelamento até o cliente downstream sempre que a API permitir; abandonar apenas a espera do resultado pode deixar operação remota ativa.

## CPU-bound

`GET /cpu?ms=N` consome CPU por uma duração limitada, útil para testar profiler e saturação:

- Go verifica cancelamento durante o loop e usa o semáforo downstream.
- Rust faz yield periodicamente para não monopolizar um worker do runtime, respeitando o semáforo.
- Node bloqueia o event loop intencionalmente para expor a consequência de CPU síncrona. Em produção, CPU-heavy work deve sair do event loop principal, por exemplo para worker threads ou workers externos.
- Elixir consome CPU em um processo BEAM com limite de downstream; o scheduler preempta o processo, mas isso não torna a carga grátis.

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
- contadores enqueued, rejected, processed, failed, retries e rejeições de tenant.

Estas métricas são locais ao processo e não substituem Prometheus/OpenTelemetry em produção.

## Retry storm e falha injetada

`POST /jobs?failures=N` injeta N falhas transitórias antes do sucesso. O worker usa número máximo de retries, backoff exponencial com jitter e o mesmo prazo máximo do job. Se `failures` excede as tentativas disponíveis, o job termina como falha e atualiza `jobs_failed_total`; não há retry infinito. Experimente `MAX_RETRIES=2` e `failures=5`.

Retry pode multiplicar carga justamente quando o downstream está lento. Observe `jobs_retries_total`, `jobs_failed_total`, tempo total e profundidade da fila. Em produção, acople retry a budget/circuit breaker e política durable de dead-letter/reprocessamento.

## Fairness / bulkhead por tenant

O endpoint permite apenas os tenants didáticos `alpha`, `beta` e `default`. `TENANT_OUTSTANDING_LIMIT` limita jobs waiting + in-flight de cada tenant. Um tenant saturado recebe `429 TENANT_BUSY`, enquanto outro mantém sua própria cota; `tenant_rejected_total` não inclui valores identificáveis do tenant.

Esse é isolamento por cota, não uma agenda fair/weighted. Os jobs continuam em FIFO global, então uma aplicação com SLA por tenant pode precisar de filas separadas ou scheduler fair.

## Streaming de produtor para consumidor

`GET /stream?items=N&delay_ms=M` produz um item por vez:

- Go escreve e dá `Flush()` por chunk; o fluxo para quando o cliente cancela.
- Rust cria um stream lazy usado por `Body::from_stream`.
- Node respeita o retorno `false` de `write()` e espera `drain`.
- Elixir envia chunks com `Plug.Conn.chunk/2`.

O produtor não materializa o corpo inteiro antes de responder. Para experimentar pressão de leitura, use um cliente que leia devagar ou desconecte durante a transmissão.

## Shutdown com backlog

`shutdown-drain` da ferramenta de benchmark enfileira trabalho e envia SIGTERM enquanto há jobs outstanding. Registra duração da parada e timeout. Go fecha o canal depois do HTTP shutdown e drena workers; Rust fecha os sender handles após o server parar; Node fecha o Fastify e espera `p-queue` ficar idle; Elixir espera o outstanding ficar a zero até o deadline, então o supervisor encerra a árvore.

O limite de shutdown é finito: se ultrapassado, pode haver trabalho volátil perdido. Filas locais não oferecem garantia de retomada depois de kill -9 ou falha da máquina.

## Variáveis novas

- `TENANT_OUTSTANDING_LIMIT=0`: desativa limite por tenant; valor positivo habilita cota (o Elixir usa a mesma variável).
- `MAX_RETRIES=3`: retries além da primeira tentativa.
- `RETRY_BASE_MS=25`: base do backoff exponencial; inclui jitter em Go, Rust, Node e Elixir.
- `failures=N`: query param de `POST /jobs`, limitado a 0..10.
