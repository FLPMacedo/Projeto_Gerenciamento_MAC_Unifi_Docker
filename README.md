# Gestão MAC Mobile UniFi — Docker + PostgreSQL

Gestão das redes Wi-Fi "mobile" de um controller **UniFi OS** multi-site: coleta o
inventário das allow-lists, guarda histórico e auditoria, mantém o cadastro de
usuários (RH) e permite adicionar/remover/trocar aparelhos de forma unitária e
auditada.

Esta é a versão **servidor/Linux/Docker**. A versão desktop (`.exe`, SQLite,
pywebview) continua no repositório anterior:
[Projeto_Gerenciamento_MAC_Unifi](https://github.com/FLPMacedo/Projeto_Gerenciamento_MAC_Unifi).

---

## Arquitetura

```
Portainer ─┬─ web        Flask + gunicorn        (stateless, escalável)
           ├─ collector  coleta a cada 10 min    (1 réplica)
           ├─ init       aplica o schema         (one-shot)
           └─ db         PostgreSQL 17 + volume
```

Os módulos sobem juntos pelo mesmo `docker-compose.yml` para facilitar os testes
de implantação. Para segmentar depois, basta separar os serviços em stacks
diferentes — eles só se comunicam pelo banco.

| Pasta | Conteúdo |
|---|---|
| `app/` | aplicação (Flask, coletor, CLI, importador) + `Dockerfile` |
| `db/` | `schema.sql` do PostgreSQL |
| `scripts/` | `init_db.py`, `migrate_sqlite_to_pg.py` |
| `docs/` | manual técnico e histórico do projeto |

---

## Subir pelo Portainer

1. **Stacks → Add stack → Repository**
   - Repository URL: `https://github.com/FLPMacedo/Projeto_Gerenciamento_MAC_Unifi_Docker`
   - Compose path: `docker-compose.yml`
2. Em **Environment variables**, preencha o que está em [`.env.example`](.env.example).
   Obrigatórios: `PGPASSWORD`, `UNIFI_HOST`, `UNIFI_SERVICE_USERNAME`,
   `UNIFI_SERVICE_PASSWORD`.
3. **Deploy the stack**. A aplicação sobe em `http://SERVIDOR:8080`
   (ajustável por `WEB_PORT`).

Localmente é o mesmo: `cp .env.example .env`, preencher, `docker compose up -d`.

---

## Credenciais: como funciona

A versão desktop gravava a senha UniFi de **cada usuário** num `creds.enc` local
da máquina. Num container único atendendo várias pessoas isso não funciona: só
existe um arquivo, e o último login sobrescreveria a credencial de todos — a
coleta passaria a rodar silenciosamente com a conta de quem entrou por último.

No servidor:

- Uma **conta de serviço** dedicada (`UNIFI_SERVICE_*`) faz toda a comunicação
  com o controller: coleta e as ações de add/remover/troca.
- O **login de cada pessoa** continua sendo validado ao vivo contra o controller
  com a conta pessoal dela. Só quem tem acesso no UniFi entra. **A senha do
  usuário não é gravada em lugar nenhum.**
- A **autoria continua rastreada**: os eventos em `events` registram o usuário
  logado como autor de cada ação.

Em produção, prefira `UNIFI_SERVICE_PASSWORD_FILE` apontando para um Docker
secret — assim a senha não aparece em `docker inspect` nem na tela de variáveis
do stack.

A conta de serviço precisa de: leitura de sites/WLANs/clientes e permissão de
editar a `mac_filter_list` das WLANs mobile.

---

## Migrar os dados do SQLite

```bash
# 1. conferir a origem (não grava nada; roda em qualquer máquina)
python scripts/migrate_sqlite_to_pg.py --sqlite /caminho/history.db --dry-run

# 2. migrar de verdade, de dentro do container
docker compose cp /caminho/history.db web:/tmp/history.db
docker compose exec web python /app/scripts/migrate_sqlite_to_pg.py \
    --sqlite /tmp/history.db --reset
```

Migra as 10 tabelas integralmente (557.742 linhas na base atual), preserva as
chaves primárias, reposiciona as sequências, transporta o lease da coleta e
confere a contagem de cada tabela ao final.

**Duas chaves de `settings` ficam de fora por padrão**, por higiene e não por
espaço:

| Chave | Por quê |
|---|---|
| `unifi_password_enc` | senha cifrada com a `secret.key` local de cada máquina. Como essa chave não vai para o servidor (o modelo agora é conta de serviço), seria texto cifrado que ninguém consegue abrir. |
| `admin_hash` | hash scrypt do admin da v1, obsoleto desde que o login passou a ser validado no controller. |

Use `--incluir-segredos` para trazê-las mesmo assim. As outras 8 chaves vão
normalmente — inclusive `unifi_host` e `unifi_site`, que servem de referência
para preencher as variáveis do stack.

---

## Regras de negócio (inalteradas)

- **35 dias** sem logar = disponível para liberar (regra à prova de férias).
  `NEVER_MODE=grace`: quem nunca conectou só vira "disponível" 35 dias após a
  primeira coleta.
- **512** entradas por allow-list de WLAN.
- **VIP** não pode ser removido sem antes desmarcar; se sumir da lista, gera
  evento `vip_removido` e banner de alerta.
- Auditoria de duas fontes: a nossa (derivada das coletas) e o espelho do log
  nativo da UniFi, preservado mesmo depois que eles purgam.
- Escrita no controller restrita a `GET`/`PUT` por trava no cliente HTTP.

---

## O que mudou em relação ao desktop

| Tema | Desktop | Servidor |
|---|---|---|
| Banco | SQLite em pasta de rede | PostgreSQL |
| Servidor | `iniciar.py` + pywebview | gunicorn (`app:app`) |
| Encerramento | watchdog matava o processo sem heartbeat | não existe — mataria o container |
| Coleta | a cada page load, com lease | serviço `collector` dedicado |
| Credenciais | `creds.enc` por máquina | conta de serviço via env/secret |
| Logs | arquivo rotativo em `logs/` | stdout (Docker/Portainer) |
| Backup do banco | `sqlite3.backup` | `pg_dump -Fc` |
| Trava de escrita | `threading.Lock` (1 processo) | `wlan_locks` no banco (entre containers) |

Detalhes e justificativas em [PLANO_MIGRACAO.md](PLANO_MIGRACAO.md).

---

## Operação

```bash
docker compose logs -f collector     # acompanhar as coletas
docker compose exec web python /app/collect.py --once   # forçar uma coleta
docker compose exec web python /app/cli.py sites        # listar sites
curl http://localhost:8080/healthz                      # saúde do serviço
```

**Backup:** o botão "Baixar banco" gera um `.dump` do `pg_dump`. Restaurar:
`pg_restore --clean --if-exists -d "$DATABASE_URL" arquivo.dump`.
Inclua também o volume `pgdata` na rotina de backup do servidor.

**Fuso horário:** as datas são formatadas com o TZ do container. `TZ` já vem
como `America/Sao_Paulo`; sem isso as telas mostrariam 3 horas a menos.

---

## Arquivos fora do versionamento

`.env`, `sites_map.json` (de-para unidade↔site), `app/static/logo_brand.png`
(logo da empresa), planilhas `.xlsx` (contêm nomes e MACs), bancos e backups.
Veja [`.gitignore`](.gitignore).
