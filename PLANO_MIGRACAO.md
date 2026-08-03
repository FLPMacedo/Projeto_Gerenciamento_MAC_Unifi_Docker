# Plano — Gestão MAC UniFi: Docker + PostgreSQL

Projeto novo: `Projeto_Gerenciamento_MAC_Unifi_Docker`
Repo destino: https://github.com/FLPMacedo/Projeto_Gerenciamento_MAC_Unifi_Docker
Origem (permanece intocada, versão desktop/Windows): `C:\Temp\_Projeto_Gerenciamento_MAC_Unifi`

Decisões tomadas: **conta de serviço + login por usuário** · **migrar todos os dados**.

---

## 1. O que existe hoje (diagnóstico)

Flask + SQLite, single-process, desktop-first. 19 rotas, 11 tabelas, 4,0 MB de banco.

Volume real (banco de **produção**, 26,6 MB, última coleta em 01/08/2026 10:49 —
não confundir com a cópia de desenvolvimento, que parou em 01/07 e tem ~10× menos
dados):

| Tabela | Linhas |
|---|---|
| `seen_history` | 549.995 |
| `unifi_audit` | 4.464 |
| `mac_state` | 1.530 |
| `client_info` | 1.169 |
| `collections` | 371 |
| `events` | 165 |
| `edit_locks` / `settings` / `active_sessions` / `wlan_locks` | 37 / 10 / 1 / 0 |
| **Total** | **557.742** |

Conferência prévia da origem (executada): nenhum NULL em coluna que o schema novo
exige `NOT NULL`, nenhuma chave primária duplicada, nenhum `collection_id` órfão
em `seen_history`. Os ids vão de 1 a 371 (`collections`) e 1 a 165 (`events`) —
daí a necessidade de reposicionar as sequências `IDENTITY` após a carga.

Regras de negócio que **não mudam**: 35 dias sem logar = disponível · cap de 512 por WLAN ·
`NEVER_MODE=grace` · VIP não removível · trava por WLAN · auditoria dupla (sistema + log nativo UniFi) ·
somente `GET`/`PUT` no controller (`unifi/client.py:83`).

**Ponto a favor da migração:** o `_UPSERT` (`unifi/db.py:162`) já usa sintaxe
`ON CONFLICT ... DO UPDATE SET ... excluded.x`, que é nativa do PostgreSQL. E `sqlite3.Row`
tem equivalente direto (`psycopg.rows.dict_row`), o que mantém todo o acesso `r["coluna"]`
funcionando — **`app.py` e os templates ficam praticamente intocados**.

---

## 2. Bloqueadores reais encontrados

Estes não são "ajustes de container", são coisas que quebram ou corrompem em produção:

### 2.1 O watchdog mata o container
`iniciar.py:50-63` encerra o processo (`os._exit(0)`) após 90 s sem heartbeat do navegador.
Em Docker isso vira **restart loop** assim que ninguém estiver com a aba aberta.
→ Entrypoint passa a ser `gunicorn app:app`. `iniciar.py`, `desktop.py` e `GestaoMAC.spec`
não entram no repo novo (ficam no repo antigo, que continua sendo o do desktop).

### 2.2 `pywebview` quebra o build da imagem
`requirements.txt:6` puxa GTK/Qt no Linux. Sai do requirements do servidor.

### 2.3 Credenciais sobrescritas entre usuários — **resolvido pela decisão tomada**
Hoje `app.py:365` grava a senha de quem logou em `creds.enc` (arquivo único).
Com N pessoas no mesmo container, a última sobrescreve as outras e a coleta roda com a conta dela.
→ Conta de serviço via env/secret faz a coleta e as escritas; o login continua validando a conta
pessoal no controller (`app.py:359-361`), mas **não persiste mais a senha do usuário**.
A auditoria passa a registrar `session["user"]` como autor real da ação — isso já acontece
(`app.py:619`, `app.py:675`), então a rastreabilidade por pessoa é preservada.

### 2.4 Estado global compartilhado → badge "online" errado (bug latente)
`unifi/db.py:306-311`: `_LATEST` é um dict de módulo que `site_inventory` e `overview_summary`
escrevem e `_is_online` lê. Com gunicorn multi-thread, duas requisições concorrentes se
sobrescrevem e o status "online" sai errado. Hoje passa despercebido porque é single-user.
→ `latest_ts` passa a ser parâmetro explícito das funções de leitura.

### 2.5 `threading.Lock` não serializa entre workers
`app.py:123` protege o caminho de escrita no UniFi apenas **dentro de um processo**.
Com múltiplos workers/réplicas, dois add/remover simultâneos podem estourar o cap de 512.
→ Trava no banco: `pg_advisory_xact_lock` + a tabela `wlan_locks` que já existe.

### 2.6 Escritas em disco do container se perdem
`backups/` (`app.py:512`) e `logs/` (`app.py:60`) gravam ao lado do código.
→ `backups/` vira volume; logs vão para stdout (padrão Docker/Portainer).

### 2.7 `/backup.db` usa API do SQLite
`app.py:539-557` chama `sqlite3.Connection.backup`. → `pg_dump` (custom format), streamado
como download.

---

## 3. Dialeto SQL — inventário do que muda em `unifi/db.py`

A API pública do módulo (nomes e assinaturas das ~40 funções) **fica idêntica**. Só a
implementação muda. Isso é o que mantém `app.py` estável.

| SQLite | PostgreSQL | Onde |
|---|---|---|
| `?` | `%s` | ~60 ocorrências |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ALWAYS AS IDENTITY` | `SCHEMA` (l. 38, 66) |
| `cur.lastrowid` | `INSERT ... RETURNING id` | `record_snapshot` (l. 195) |
| `MAX(a,b)` / `MIN(a,b)` escalar | `GREATEST` / `LEAST` | `_UPSERT` (l. 176-181) |
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` | l. 731, 788, 803 |
| `GROUP_CONCAT(DISTINCT x)` | `string_agg(DISTINCT x, ', ')` | `_AGG` (l. 564) |
| `PRAGMA busy_timeout/synchronous` | remover | l. 155-156 |
| `_migrate` via `PRAGMA table_info` | migrações versionadas em `db/migrations/` | l. 125-140 |
| `sqlite3.Row` | `psycopg.rows.dict_row` | l. 154 |
| `conn = connect(); conn.close()` por request | pool (`psycopg_pool`) | `app.py:127` |

**Atenção específica —** `claim_collection` (l. 785-796) faz
`WHERE CAST(value AS INTEGER) <= %s` sobre a coluna TEXT de `settings`. No PostgreSQL isso
**lança exceção** se o valor estiver vazio ou não-numérico (SQLite devolve 0 silenciosamente).
O lease vira tabela própria com coluna `BIGINT`. O mecanismo (UPDATE atômico condicional)
continua válido e correto em PG.

**Timestamps:** ficam como estão — `BIGINT` epoch. Converter para `timestamptz` tocaria ~40
pontos de leitura mais os filtros de template, sem ganho funcional. Anotado como melhoria futura.

---

## 4. Arquitetura alvo

```
Portainer  ─┬─ web        gunicorn app:app        (N réplicas, stateless)
            ├─ collector  coleta agendada          (1 réplica, conta de serviço)
            └─ db         postgres:16 + volume
```

A coleta sai do caminho da requisição HTTP. Hoje `maybe_collect()` roda a cada page load
(`app.py:432`, `445`, `492`, `731`…) com um lease para evitar duplicidade. Com o collector
dedicado, o `web` só lê — as telas ficam mais rápidas e o lease vira uma segunda linha de
defesa em vez do mecanismo principal.

### Estrutura do repositório

Módulos separados desde já, mas subindo juntos — para segmentar depois só se removem serviços
do compose:

```
├─ docker-compose.yml          # web + collector + db
├─ .env.example                # todas as variáveis, sem segredo real
├─ app/
│  ├─ Dockerfile
│  ├─ requirements.txt         # SEM pywebview/pyinstaller
│  ├─ app.py  cli.py  collect.py  importar.py
│  ├─ unifi/  templates/  static/
├─ db/
│  ├─ Dockerfile               # postgres + init
│  ├─ schema.sql
│  └─ migrations/
└─ scripts/
   └─ migrate_sqlite_to_pg.py
```

---

## 5. Fases

**Fase 0 — Repo novo.** `git init` em `C:\Temp\_Projeto_UnifiDocker`, remote para o repo Docker.
Copiar só o fonte. **Não entram:** `GestaoMAC.exe` (24 MB), `GestaoMAC_v4.zip` (25 MB),
`Wifi (1).xlsx` (dados pessoais — nomes e MACs), `*.db`, `secret.key`, `creds.enc`,
`sites_map.json`, `dist/`, `build/`, `pacote/`, `.venv/`, `logs/`. O repo antigo não é tocado.

**Fase 1 — Camada de banco.** Reescrever `unifi/db.py` sobre psycopg3 mantendo a API pública.
`schema.sql` + migrações. Pool de conexões. Corrigir 2.4 (`_LATEST`) e 2.5 (advisory lock).

**Fase 2 — Adequação do app.** Conta de serviço (2.3), `pg_dump` no `/backup.db` (2.7),
logs para stdout, `backups/` em volume, `/healthz`, `COLLECT_ON_OPEN` desativado por padrão.

**Fase 3 — Containers.** Dockerfile (`python:3.12-slim`, usuário não-root, gunicorn),
compose com healthcheck e `depends_on: service_healthy`, volume nomeado para o Postgres.

**Fase 4 — Migração dos dados.** `migrate_sqlite_to_pg.py` idempotente, com conferência de
contagem tabela a tabela contra os números da seção 1. Rodar contra cópia do banco primeiro.

**Fase 5 — Validação.** Subir o stack, percorrer as 19 rotas, conferir: regra dos 35 dias,
cap 512, alerta VIP, auditoria nas duas fontes, add/remover/troca, export CSV, backup.
Teste de concorrência: dois add simultâneos no mesmo site têm que serializar.

**Fase 6 — Segmentação (depois).** Separar em imagens/repos independentes se fizer sentido
para o pipeline de implantação.

---

## 6. Pontos que exigem atenção na validação

- **Fuso horário.** `time.localtime()` (`unifi/db.py:628`, `app.py:901`) usa o TZ do sistema.
  Container Linux sobe em UTC → datas exibidas 3 h atrás. Definir `TZ=America/Sao_Paulo`.
- **`unifi/inventory.py` tem `_classify`/`STATUS_LABEL` duplicados** dos de `db.py`, com
  faixas divergentes ("8-30d" vs "8-35d"). Só `db.py` está em uso nas telas. Não mexer agora,
  mas registrar — é armadilha para quem for manter depois.
- **`secret.key`/Fernet continua necessário** para a senha da conta de serviço em repouso.
  A chave vira secret do Portainer, não arquivo no volume.
- **`sites_map.json`** (de-para unidade↔site) é local e gitignored. Vira ConfigMap/volume ou
  variável de ambiente no stack.
