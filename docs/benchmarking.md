# Benchmarking reproduzível

## Executar

```sh
make load-smoke
make benchmark STACK=go SCENARIO=queue-saturation REPEATS=3
make benchmark STACK=elixir SCENARIO=tenant-fairness REPEATS=3
```

Stacks: `go`, `rust`, `node`, `elixir`. Cenários: `queue-saturation`, `constant-rate`, `burst`, `rate-limit`, `slow-downstream`, `cpu`, `streaming`, `retry-storm`, `tenant-fairness`, `shutdown-drain`.

O script inicia apenas a stack escolhida em loopback, aguarda `/healthz`, executa aquecimento curto, captura `/metrics`, roda `oha`, espera o backlog drenar quando aplicável, e encerra o servidor. Cada repetição recebe uma instância nova. Portas, URLs e logs temporários não são gravados no relatório.

Exemplos diretos com `oha` seguem no README. `oha` JSON é resumido em latência p50/p95/p99, taxa, duração, HTTP status counts e erros de transporte; os dados completos do processo oha não são persistidos. Em Linux, o runner amostra o process group do servidor via `/proc` e registra apenas CPU média em cores e RSS máximo. PIDs, argv e linhas completas de `/proc` não são escritos.

## Relatórios

Cada execução gera `benchmarks/results/<timestamp>-<commit>-<stack>-<scenario>-<run-id>.json` e `.md`. O JSON segue [`../benchmarks/schema.json`](../benchmarks/schema.json); `scripts/benchmark.py` rejeita chaves sensíveis e caminhos pessoais antes de salvar.

O JSON registra configuração explicitamente passada pelo script, commit, indicador clean/dirty da source tree, versões de ferramentas e allowlist de OS, kernel em versão numérica, arquitetura, modelo da CPU, cores e memória arredondada. `working_tree_clean` ignora os próprios outputs gerados em `benchmarks/results/` e `benchmarks/profiling/results/`; código ou documentação modificados continuam marcando a execução como dirty. Não lê nem serializa o ambiente completo. Resultados locais são candidatos a baseline; revisar configuração e privacidade antes de adicioná-los ao Git.

## Como comparar

- Use a mesma máquina, runtimes/releases, limites de recurso, parâmetros e versão do oha.
- Não execute benchmarks simultaneamente entre stacks na mesma máquina.
- Compare taxas e percentis junto de códigos 429/503, queue depth e jobs ativos; throughput isolado não mede se o serviço protegeu o downstream.
- Registre aquecimento e repetições. O script separa as repetições e reporta a mediana de RPS e latências.
- Os resultados são exemplos reproduzíveis de backpressure; não constituem ranking universal de linguagens.
- Carga longa e profiling eBPF não rodam na CI normal. Rode localmente em ambiente controlado.

`shutdown-drain` mede quanto tempo o processo leva para sair com trabalho aceito pendente e registra timeout/exit status, sem manter logs brutos.
