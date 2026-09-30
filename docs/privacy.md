# Privacidade dos relatórios de performance

## Allowlist coletada

`scripts/collect_environment.py` coleta apenas campos definidos no código:

- família do OS, distro ID/versão, kernel reduzido a números e arquitetura;
- modelo da CPU, cores lógicos e RAM arredondada;
- limites de CPU/memória do cgroup quando visíveis;
- versões de Go, Rust/Cargo, Node/pnpm, Elixir/OTP/Mix, oha e ferramentas opcionais;
- commit Git curto e booleano `working_tree_clean` (sem lista de arquivos/diff).
- CPU média do process group e RSS máximo amostrados via `/proc` quando Linux disponibiliza os dados; PIDs são usados apenas em memória para selecionar o group e nunca são registrados.

Os valores de configuração são montados a partir dos argumentos do benchmark, não lendo um dump de `os.environ`. Configs são números enum/limites explícitos. IDs de execução são aleatórios.

`working_tree_clean` sinaliza mudanças de source/config/docs, ignorando os próprios relatórios gerados em `benchmarks/results/` e `benchmarks/profiling/results/` para que vários runs não tornem a árvore permanentemente dirty.

## Campos nunca registrados

Hostname, nome de usuário, IP/MAC, número de série, PID, argumentos completos, lista de processos, diretórios pessoais, request/response bodies, tokens/passwords e variáveis de ambiente arbitrárias não são coletados. O arquivo JSON não armazena URL/porta local nem stderr/logs do processo. Kernel fica reduzido a versão numérica; RAM e limites cgroup são arredondados.

Perfis brutos, pprof, cpuprofile, stacks e FlameGraphs podem conter caminhos/símbolos locais. Eles devem permanecer em `benchmarks/raw/` (ignorado pelo Git), ser revisados e só ser compartilhados depois de sanitização manual. O perfil bruto nunca é anexado automaticamente ao relatório resumido.

## Revisar antes de compartilhar

```sh
python3 scripts/collect_environment.py
python3 -m unittest scripts.test_reporting
python3 scripts/benchmark.py --stack go --scenario queue-saturation --repeats 1
```

Revise os JSON/Markdown em `benchmarks/results/` antes de versioná-los. Um relatório sanitizado não é uma garantia de anonimato: modelo/arquitetura e versões podem identificar uma classe de máquina, então remova também campos permitidos se o ambiente exigir maior sigilo.
