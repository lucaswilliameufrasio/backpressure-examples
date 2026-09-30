# Backpressure examples

Exemplos locais e executáveis de backpressure em Go, Rust e Node.js. O mesmo princípio aparece em todas as stacks: **limitar o trabalho aceito e em execução, observar saturação e rejeitar cedo quando não há capacidade**.

```text
cliente ──> API ──> fila local limitada ──> worker pool ──> downstream limitado
              ├── 429 se a fila estiver cheia
              ├── 503 se a operação síncrona saturar
              └── /metrics para observar backlog e rejeições
```

As filas desta demo são **locais e voláteis**. Elas são úteis para aprender e testar controle de carga; não sobrevivem a reinícios e não substituem um broker durável.

## Exemplos

| Diretório | HTTP / runtime | Mecanismos |
|---|---|---|
| [`go/`](go/) | `net/http` | channel bounded, worker pool, channel-semaphore, `context`, token bucket de `x/time/rate` |
| [`rust/`](rust/) | Axum / Tokio | `mpsc` bounded, workers Tokio, `Semaphore`, timeout, token bucket didático |
| [`node/`](node/) | Fastify / Node ESM | `p-queue`, `p-limit`, `@fastify/rate-limit` e `pnpm` |

Endpoints disponíveis em cada serviço:

| Endpoint | Comportamento |
|---|---|
| `GET /healthz` | Health check simples |
| `POST /jobs` | Enfileira um job e retorna `202`; fila cheia retorna `429` |
| `GET /sync` | Simula trabalho I/O-bound com limite de concorrência; saturação retorna `503` |
| `GET /cpu?ms=50` | Carga CPU-bound curta, limitada a 1 segundo |
| `GET /limited` | Demonstra rate limit HTTP |
| `GET /batch?items=100&concurrency=8` | Processa lote sem exceder a concorrência solicitada |
| `GET /metrics` | Retorna métricas didáticas em JSON |

## Pré-requisitos

- Go instalado.
- Rust e Cargo instalados.
- Node.js 22+ e pnpm.
- `oha` para os testes de carga (opcional): <https://github.com/hatoo/oha>.

## Executar

### Go

```sh
cd go
go mod tidy
go run .
```

Padrão: `http://localhost:8080`. Para reduzir capacidade e acelerar a saturação:

```sh
QUEUE_CAPACITY=4 WORKERS=1 DOWNSTREAM_CONCURRENCY=1 PROCESS_DELAY_MS=1500 go run .
```

### Rust

```sh
cd rust
cargo run
```

Padrão: `http://localhost:3000`.

```sh
QUEUE_CAPACITY=4 WORKERS=1 DOWNSTREAM_CONCURRENCY=1 PROCESS_DELAY_MS=1500 cargo run
```

### Node.js

```sh
cd node
pnpm install
pnpm start
```

Padrão: `http://localhost:3000`.

```sh
QUEUE_CAPACITY=4 WORKERS=1 DOWNSTREAM_CONCURRENCY=1 PROCESS_DELAY_MS=1500 pnpm start
```

Go usa a porta 8080 por padrão; Rust e Node usam 3000. Para rodar vários exemplos ao mesmo tempo, escolha portas distintas com `ADDR` no Go ou `PORT` no Rust/Node.

### Configuração por ambiente

| Variável | Padrão | Aplicação |
|---|---:|---|
| `ADDR` / `PORT` | Go `:8080`, Rust/Node `3000` | Endereço/porta HTTP |
| `QUEUE_CAPACITY` | `32` | Quantos jobs podem aguardar na fila local |
| `WORKERS` | `4` | Jobs processados em paralelo |
| `DOWNSTREAM_CONCURRENCY` | `2` | Limite de operações downstream simultâneas |
| `PROCESS_DELAY_MS` | `750` | Duração da operação simulada |
| `JOB_TIMEOUT_MS` | `5000` | Timeout de processamento |
| `RATE_PER_SECOND` | `10` | Taxa de `/limited` (Node: máximo em janela fixa de 1 segundo) |
| `RATE_BURST` | `20` | Burst inicial de `/limited` no token bucket Go/Rust |

`RATE_BURST` é usado apenas por Go e Rust. O plugin Fastify demonstra uma janela fixa, não token bucket.

## Experimentar com curl

Troque a porta para o serviço em execução:

```sh
curl -i -X POST http://localhost:8080/jobs
curl -i http://localhost:8080/sync
curl -i http://localhost:8080/cpu?ms=50
curl -i http://localhost:8080/limited
curl -i http://localhost:8080/batch?items=100\&concurrency=8
curl -s http://localhost:8080/metrics
```

Envie `POST /jobs` repetidamente com a fila pequena e o processamento lento. As primeiras requisições devem ser aceitas; as que excederem a capacidade de espera recebem `429` com `QUEUE_FULL`. `GET /sync` usa limite de concorrência separado e responde `503` ao saturar.

## Teste de carga com oha

Rode um comando por vez e consulte `/metrics` durante e depois da carga. O serviço Go está nos exemplos abaixo; substitua a porta conforme necessário.

### Carga concorrente para encher a fila

```sh
oha --no-tui -n 5000 -c 100 -m POST http://localhost:8080/jobs
```

Use `QUEUE_CAPACITY=4`, `WORKERS=1` e `PROCESS_DELAY_MS=1500` para facilitar a saturação. Observe a proporção de `202` e `429`, a latência e `queue_depth`.

### Taxa controlada / teste prolongado

```sh
oha --no-tui -z 60s -w -c 20 -q 50 -m POST http://localhost:8080/jobs
```

Para um soak test mais longo, aumente `-z` (por exemplo, `-z 10m`). A opção `-q` controla a taxa de chegada; ajuste-a abaixo e acima da capacidade aproximada dos workers.

### Burst

```sh
oha --no-tui -n 2000 -c 200 --burst-rate 200 -m POST http://localhost:8080/jobs
```

### Rate limit

```sh
oha --no-tui -n 500 -c 50 http://localhost:8080/limited
```

Go usa token bucket via `x/time/rate`; Rust usa token bucket didático; Node usa a janela de rate limit do plugin Fastify. Pequenas diferenças de semântica são intencionais e estão explicadas nos exemplos.

### Downstream lento e operação síncrona

Inicie com `DOWNSTREAM_CONCURRENCY=1 PROCESS_DELAY_MS=1500` e rode:

```sh
oha --no-tui -n 1000 -c 50 http://localhost:8080/sync
```

Observe os `503` antecipados e confirme que `downstream_in_use` não ultrapassa o limite.

### CPU-bound para profiling

```sh
oha --no-tui -z 30s -c 8 'http://localhost:8080/cpu?ms=100'
```

Isso fornece atividade de CPU para profiler/FlameGraph. O endpoint Node é deliberadamente síncrono para tornar visível o bloqueio do event loop; não deve ser copiado como padrão de produção para CPU-heavy work.

### Como interpretar resultados

- Verifique status codes, throughput e percentis de latência reportados pelo `oha`.
- Compare o comportamento antes/depois do limite, não somente requests por segundo.
- Correlacione latência e rejeições com `/metrics`.
- Registre máquina, versões, configuração, duração e concorrência.
- Não trate uma execução isolada como benchmark comparativo entre linguagens.

## Testes e verificações

```sh
(cd go && go test -count=1 ./... && go vet ./... && go build ./...)
(cd rust && cargo fmt --check && cargo test && cargo clippy --all-targets -- -D warnings)
(cd node && pnpm check && pnpm test)
```

O workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) executa formatação, lint, testes e build por linguagem em cada push e pull request. Os testes de carga com `oha` continuam manuais para manter CI determinística.

## Cenários de produção: conceitos, não dependências da demo

### Webhook de pagamento

O fluxo seguro é validar assinatura, persistir o evento bruto com chave única do provedor e só então confirmar recebimento; o processamento é idempotente e assíncrono. Para não perder eventos entre persistência e publicação, use transactional outbox ou publique a partir de um registro durável. Um `202` depois de enfileirar somente em memória não é garantia de entrega.

### Geração de PDF / ingresso

Persistir job e chave de idempotência, publicar para broker durável, limitar consumidores e concorrência do downstream e registrar resultado/erro. A fila interna desta demo ilustra controle local de pressão, não substitui SQS, NATS JetStream, RabbitMQ ou outro broker.

Consulte [`docs/scenarios.md`](docs/scenarios.md) e [`docs/ebpf-profiling.md`](docs/ebpf-profiling.md) para detalhes adicionais.
