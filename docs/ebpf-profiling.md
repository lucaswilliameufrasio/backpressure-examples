# Profiling, FlameGraph e eBPF

Este guia é uma trilha opcional depois dos testes com `oha`. Não é necessário para compilar, executar ou validar os exemplos básicos.

`make ebpf-check` grava um relatório sanitizado de readiness: Linux, disponibilidade de kernel BTF, `perf` e `bpftrace`. Ele não carrega um programa BPF. `make ebpf-probe` tenta explicitamente um programa mínimo com o bpftrace instalado, sem `sudo`; o relatório guarda apenas `passed`, `permission-denied`, `failed`, `timed-out` ou `tool-not-installed`, nunca stdout/stderr.

## Ordem sugerida

1. Reproduza o sintoma com uma carga de `oha` definida.
2. Consulte `/metrics` e determine se o limite ativo é fila, workers, downstream, CPU ou event loop.
3. Use o profiler do runtime para obter contexto por função.
4. Use amostragem de CPU/eBPF para confirmar o perfil do processo no Linux.
5. Faça uma alteração e repita exatamente o mesmo teste, ambiente e duração.

Uma fila cheia pode ser backpressure funcionando como planejado, não um bug. Primeiro procure crescimento sem limite, latência descontrolada, timeout ou saturação de uma dependência.

## Profiler por runtime

- **Go:** inicie com `ENABLE_PPROF=1`; o listener pprof escuta somente `127.0.0.1:6060`. Capture um profile durante `/cpu` com `go tool pprof 'http://127.0.0.1:6060/debug/pprof/profile?seconds=15'`. Evite expor o listener fora de localhost.
- **Rust:** compile com símbolos de debug para profiling (`cargo run` em dev ou configure símbolos no profile release) e use `samply` ou `perf` para capturar stacks.
- **Node.js:** rode o servidor com `node --cpu-prof src/server.js`, gere carga em `/cpu` e finalize o processo para gravar o `.cpuprofile`; abra no Chrome DevTools. O endpoint CPU-bound demonstra deliberadamente bloqueio do event loop.

## eBPF/bpftrace em Linux

Requisitos variam com distribuição e kernel. Em geral, é necessário um kernel com suporte e BTF/perf events, bpftrace instalado e permissões elevadas (normalmente root ou capacidades apropriadas). Em containers, seccomp, capabilities e acesso ao host podem impedir o uso. eBPF não é parte dos testes automatizados do repositório.

Exemplo de amostragem de stacks de usuário de um PID, se a versão instalada do bpftrace suportar `-p` e `ustack`:

```sh
PID=<pid-do-servidor>
sudo bpftrace -p "$PID" -e 'profile:hz:99 { @[ustack] = count(); }'
```

Mantenha a carga `oha` ativa enquanto captura as amostras. O endpoint `/cpu?ms=100` fornece trabalho de CPU para o profiler. Alguns runtimes/JITs podem exigir símbolos, frame pointers ou configuração adicional para stacks úteis; nomes ausentes não significam ausência de custo.

Para FlameGraph, exporte as stacks amostradas no formato aceito pelo conjunto Brendan Gregg FlameGraph e gere SVG. Para profiler eBPF contínuo, Parca é uma extensão possível; não é dependência desta demo.

## Limitações e interpretação

- eBPF é específico de Linux e pode exigir privilégios; não deve ser necessário em ambientes de desenvolvimento comuns.
- Carga sintética curta e profiling têm overhead e não representam automaticamente produção.
- `/sync` é espera artificial (I/O-bound); não se espera um perfil de CPU intenso nesse caminho.
- `/cpu` é sintético; serve para validar captura e ilustrar pressão de CPU, não para modelar uma aplicação real.
- Não compare FlameGraphs de máquinas, flags de compilação ou versões de runtime diferentes sem registrar essas condições.
