# Backpressure examples

Exemplos locais e executáveis de backpressure em Go, Rust, Node.js e Elixir. O mesmo princípio aparece em todas as stacks: **limitar o trabalho aceito e em execução, observar saturação e rejeitar cedo quando não há capacidade**.

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
| [`elixir/`](elixir/) | Plug / Bandit / OTP | `GenStage`, demanda bounded, admissão ETS atômica e consumers supervisionados |

Endpoints disponíveis em cada serviço:

| Endpoint | Comportamento |
|---|---|
| `GET /healthz` | Health check simples |
| `POST /jobs?tenant=alpha&failures=2` | Enfileira um job e retorna `202`; fila/tenant cheio retorna `429`; injeta falhas transitórias para demonstrar retry |
| `GET /sync` | Simula trabalho I/O-bound com limite de concorrência; saturação retorna `503` |
| `GET /cpu?ms=50` | Carga CPU-bound curta, limitada a 1 segundo |
| `GET /limited` | Demonstra rate limit HTTP |
| `GET /batch?items=100&concurrency=8` | Processa lote sem exceder a concorrência solicitada |
| `GET /stream?items=100&delay_ms=10` | Escreve chunks progressivamente sem montar toda a resposta |
| `GET /metrics` | Retorna métricas didáticas em JSON |

## Pré-requisitos

- Go instalado.
- Rust e Cargo instalados.
- Node.js 22+ e pnpm.
- Elixir 1.19+ e Erlang/OTP 28+.
- Python 3 para scripts de desenvolvimento/benchmark.
- `oha` para os testes de carga (opcional): <https://github.com/hatoo/oha>.
- `mise` é recomendado para instalar as versões fixadas em `mise.toml`, mas não é obrigatório.

## Caminho rápido

```sh
mise install go rust node pnpm erlang elixir golangci-lint oha  # opcional: instala ferramentas pinadas
make doctor        # verifica runtimes e ferramentas
make setup         # resolve dependências das quatro stacks
make run-all       # inicia os quatro serviços em portas locais distintas
```

`Ctrl-C` encerra os processos iniciados por `make run-all`. Para validar ou comparar rapidamente:

```sh
make check         # format, lint, build e testes das quatro linguagens
make doctor-ebpf   # detecta BTF, ferramenta e necessidade de privilégio
make load-smoke    # carga curta oha e relatórios sanitizados
make benchmark STACK=elixir SCENARIO=queue-saturation REPEATS=3
make profile-go    # pprof CPU e relatório sumarizado sem manter o perfil bruto
make ebpf-check    # verifica ferramentas/requisitos opcionais de Linux
make ebpf-probe    # tenta carregar um programa mínimo (opcional, sem sudo)
sudo -v && make ebpf-probe-sudo  # registra uma tentativa via sudo sem gravar como root
```

O script `scripts/benchmark.py` permite selecionar cenário, limites e número de repetições. Use `make help` para listar os alvos.

Os cenários automatizados pelo runner são `queue-saturation`, `constant-rate`, `burst`, `rate-limit`, `slow-downstream`, `cpu`, `streaming`, `retry-storm`, `tenant-fairness` e `shutdown-drain`. Cada execução grava JSON e Markdown sanitizados em `benchmarks/results/`; confira `working_tree_clean` antes de tratar uma saída como baseline.

## Executar

### Go

```sh
cd go
go mod tidy
go run .
```

Padrão: `http://127.0.0.1:8080`. Para reduzir capacidade e acelerar a saturação:

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

### Elixir

```sh
cd elixir
mix local.hex --force
mix local.rebar --force
mix deps.get
mix run --no-halt
```

Padrão: `http://localhost:3003`. O pipeline GenStage usa demanda explícita e o produtor possui `buffer_size` configurado. Antes de publicar, a admissão reserva atomicamente capacidade para o total outstanding (`fila + workers`), evitando transferir a fila ilimitada para a mailbox OTP.

```sh
QUEUE_CAPACITY=4 WORKERS=1 DOWNSTREAM_CONCURRENCY=1 PROCESS_DELAY_MS=1500 mix run --no-halt
```

Go usa a porta 8080; Rust e Node usam 3000; Elixir usa 3003. A execução conjunta por `make run-all` escolhe portas livres em loopback.

### Configuração por ambiente

| Variável | Padrão | Aplicação |
|---|---:|---|
| `ADDR` / `PORT` | Go `127.0.0.1:8080`, Rust/Node `3000`, Elixir `3003` | Endereço/porta HTTP (loopback) |
| `QUEUE_CAPACITY` | `32` | Quantos jobs podem aguardar na fila local |
| `WORKERS` | `4` | Jobs processados em paralelo |
| `DOWNSTREAM_CONCURRENCY` | `2` | Limite de operações downstream simultâneas |
| `PROCESS_DELAY_MS` | `750` | Duração da operação simulada |
| `JOB_TIMEOUT_MS` | `5000` | Timeout de processamento |
| `RATE_PER_SECOND` | `10` | Taxa de `/limited` (Node: máximo em janela fixa de 1 segundo) |
| `RATE_BURST` | `20` | Burst inicial de `/limited` no token bucket Go/Rust |
| `TENANT_OUTSTANDING_LIMIT` | `0` (desativado) | Total waiting + in-flight por tenant (`alpha`, `beta`, `default`) |
| `MAX_RETRIES` | `3` | Retries além da tentativa inicial para falha simulada |
| `RETRY_BASE_MS` | `25` | Base do backoff exponencial com jitter |
| `ENABLE_PPROF` | `0` | Só Go: habilita pprof em `127.0.0.1:6060` |
| `PPROF_ADDR` | `127.0.0.1:6060` | Go: endereço local configurável do listener pprof |

`RATE_BURST` é usado por Go/Rust; Elixir usa `RATE_PER_SECOND` com janela fixa de 1 segundo. Fastify também demonstra janela fixa, não token bucket.

`MAX_RETRIES=0` desativa retries. `TENANT_OUTSTANDING_LIMIT=0` desativa as cotas alpha/beta. `ENABLE_PPROF=1` só cria um listener pprof separado em loopback; nunca o exponha em uma interface pública.

`make setup` não instala pacotes de sistema nem usa `sudo`. Se `bpftrace` faltar, o diagnóstico final sugere o pacote da distro; consulte [`docs/ebpf-profiling.md`](docs/ebpf-profiling.md).

## Experimentar com curl

Troque a porta para o serviço em execução:

```sh
curl -i -X POST http://localhost:8080/jobs
curl -i http://localhost:8080/sync
curl -i http://localhost:8080/cpu?ms=50
curl -i http://localhost:8080/limited
curl -i http://localhost:8080/batch?items=100\&concurrency=8
curl -i 'http://localhost:8080/stream?items=20&delay_ms=10'
curl -i -X POST 'http://localhost:8080/jobs?tenant=alpha&failures=2'
curl -s http://localhost:8080/metrics
```

Envie `POST /jobs` repetidamente com a fila pequena e o processamento lento. As primeiras requisições devem ser aceitas; as que excederem a capacidade de espera recebem `429` com `QUEUE_FULL`. `GET /sync` usa limite de concorrência separado e responde `503` ao saturar.

## Teste de carga com oha

Rode um comando por vez e consulte `/metrics` durante e depois da carga. Os comandos servem para qualquer stack; use a porta mostrada por `make run-all` ou a porta padrão do serviço.

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

Go usa token bucket via `x/time/rate`; Rust usa token bucket didático; Node/Fastify e Elixir/ETS demonstram janela fixa. Pequenas diferenças de semântica são intencionais e estão explicadas nos exemplos.

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
make check
```

Os mesmos checks rodam em jobs independentes no workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml). `oha` continua local/manual para manter a CI determinística.

## Resultados e privacidade

`make benchmark STACK=go SCENARIO=queue-saturation REPEATS=3` gera JSON/Markdown em `benchmarks/results/`. O coletor usa allowlist para hardware/software e nunca copia o ambiente completo. Revisar os sumários antes de versionar; perfis brutos ficam em `benchmarks/raw/` (ignorado pelo Git). Veja [`docs/benchmarking.md`](docs/benchmarking.md), [`docs/privacy.md`](docs/privacy.md) e [`docs/profiling.md`](docs/profiling.md).

## Cenários de produção: conceitos, não dependências da demo

### Webhook de pagamento

O fluxo seguro é validar assinatura, persistir o evento bruto com chave única do provedor e só então confirmar recebimento; o processamento é idempotente e assíncrono. Para não perder eventos entre persistência e publicação, use transactional outbox ou publique a partir de um registro durável. Um `202` depois de enfileirar somente em memória não é garantia de entrega.

### Geração de PDF / ingresso

Persistir job e chave de idempotência, publicar para broker durável, limitar consumidores e concorrência do downstream e registrar resultado/erro. A fila interna desta demo ilustra controle local de pressão, não substitui SQS, NATS JetStream, RabbitMQ ou outro broker.

Consulte [`docs/approaches.md`](docs/approaches.md), [`docs/scenarios.md`](docs/scenarios.md) e [`docs/ebpf-profiling.md`](docs/ebpf-profiling.md) para detalhes adicionais.
