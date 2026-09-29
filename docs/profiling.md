# Profiling e eBPF

Profiling é opcional e vem **depois** de reproduzir o gargalo com `oha`. Não execute o eBPF como pré-requisito de setup ou CI.

## Profiler por runtime

- Go: `make profile-go` inicia o servidor com pprof somente em loopback, injeta carga em `/cpu`, captura com `go tool pprof` e salva apenas símbolos/percentuais sumarizados. O `.pprof` bruto fica num diretório temporário e é apagado. Também é possível iniciar com `ENABLE_PPROF=1`; por padrão o listener fica em `127.0.0.1:6060`.
- Rust: use `samply` ou `perf` com símbolos de debug. Build release inclui debug symbols neste exemplo.
- Node: `node --cpu-prof src/server.js`, rode carga em `/cpu?ms=100` e examine o `.cpuprofile` em Chrome DevTools. `/cpu` bloqueia deliberadamente o event loop.
- Elixir/BEAM: use profiler adequado ao BEAM; compare schedulers/CPU e processos, não confunda número de processos com concorrência ilimitada.

O endpoint CPU-bound oferece amostras sintéticas. `/sync` simula espera I/O-bound; o profile de CPU deve mostrar comportamento distinto.

## eBPF em Linux

Veja [`ebpf-profiling.md`](ebpf-profiling.md) para bpftrace, requisitos de kernel/BTF, permissões e exemplo de captura. `make ebpf-check` registra readiness; `make ebpf-probe` tenta um programa mínimo sem sudo e grava apenas o status. Para perfis reais, execute em Linux com permissões apropriadas e `oha` ativo. Registre só ferramenta/versão, modo, frequência, duração e conclusão resumida.

Stashes de stacks e FlameGraphs brutos não são adicionados automaticamente a `benchmarks/results/`; caminhos de usuário e símbolos podem revelar detalhes locais. Mantenha os dados brutos em `benchmarks/raw/`, ignore-os no Git e revise/sanitiza antes de compartilhar.
