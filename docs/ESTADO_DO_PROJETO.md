# Estado do projeto — Handoff para a próxima sessão

## v4 — correções finais (LER PRIMEIRO)
- **Bug "abre e fecha em segundos" CORRIGIDO** (commit `1ef7e2d`): o `pagehide` do
  navegador (dispara em qualquer navegação) chamava `/api/close` que zerava o
  heartbeat → watchdog encerrava o app. Agora: `pagehide` removido; `api_close`
  só encerra a sessão de presença (não derruba); heartbeat também na tela de
  login; `/api/ping` e `/api/close` são públicos. Watchdog encerra só por
  ausência REAL de ping (~90s).
- **Login por usuário + secret.key LOCAL** (commit `e69e08b`): cada usuário usa a
  PRÓPRIA conta UniFi. As credenciais ficam em **`creds.enc` LOCAL por máquina**
  (`unifi/config.py`), criptografadas com **`secret.key` gerada na 1ª vez em cada
  PC** (NÃO se distribui chave; NADA de senha no banco compartilhado). `app.py`:
  `CREDS_PATH`; login/`config` gravam via `config.save`. `collect.py` lê de
  `creds.enc`. Build NÃO copia mais `secret.key`. Marca via `BRAND` (.env).
- **Repo SANITIZADO** (commit `ac7f0da`, histórico reescrito/force-push): sem
  IPs/nomes de sites/identidade. Nomes de sites/unidades reais ficam em
  `sites_map.json` (local, gitignored). Logo da empresa em `static/logo_brand.*`
  (local) + `BRAND` no `.env`.

## v4 (base) — versionado no Git/GitHub (FLPMacedo/Projeto_Gerenciamento_MAC_Unifi)
- **Auditoria durável + espelho do log nativo da UniFi**: a cada coleta importa
  `POST /proxy/network/v2/api/site/<site>/system-log/admin-activity` para a tabela
  `unifi_audit` (dedup por uid), preservado mesmo após a UniFi purgar. Tela
  Auditoria tem 2 fontes: "Sistema" (nossa, derivada) e "UniFi (nativo)".
- **Auto-update distribuído com LEASE**: `db.claim_collection` garante que só UM
  terminal coleta por janela (= `COLLECT_BASE`60s + 30s×conectados); os demais só
  re-leem (auto-refresh de tela). Coleta ao abrir/fechar + botão manual (force=15s).
- **Credenciais/secret LOCAIS por máquina** (ver "correções finais" acima):
  `secret.key` + `creds.enc` gerados em cada PC; DB na rede via `DB_PATH`/login.
- **Presença**: `active_sessions` + heartbeat `/api/ping` (25s) → "👥 N conectados".
- **App SILENT** (build `--windowed`, sem console) + **watchdog** em `iniciar.py`
  (sem heartbeat ~90s → coleta final + `os._exit`). Logs em **`logs/`** (rotação
  diária, `TimedRotatingFileHandler`).
- **Trava de escrita por WLAN** (`wlan_locks`) em add/remover/troca (serializa só
  o mesmo site; aviso amigável se ocupado).
- `APP_VERSION="v4"`. Novas tabelas: unifi_audit, active_sessions, wlan_locks.
- **Git**: commits por bloco; tag `v4`. `.gitignore` protege .env, *.key, *.xlsx,
  data/, dist/, etc. (nada sensível no repo).

## v3
- Entry do exe: `iniciar.py` (abre navegador). Build: `build_exe.ps1` (ícone UniFi).
- **Escrita controlada** reativada: módulos **/adicionar** e **/remover** (1 MAC,
  add só com folga<512, remover bloqueia VIP), + **troca** de aparelho na ficha
  (transfere cadastro, evento `troca`). Tudo logado na auditoria com o usuário.
- Cadastro tem: nome, setor, unidade(checklist), função, líder, **gestor_autorizou**,
  chamado, observações + checks **termo** e **vip**. Filtros: VIP / Sem termo /
  unidade / busca. Banco simultâneo (rede): busy_timeout 20s, edit_locks,
  COLLECT_ON_OPEN, pasta do banco apontada no login (dbpath.cfg).
- Refator: removidos mortos (admin_user, collect_all_sites, summarize_all,
  hash/verify_password) e rascunhos (explore.py, server.*, inicio.txt).
- `APP_VERSION="v3"` (rodapé/login). 19 rotas.


Última atualização: 2026-06-25. Idioma do usuário: **português**.

## Resumo do que é
Sistema de **gestão** das redes "mobile" (Wi-Fi) de um controller **UniFi OS**
(multi-site). Ele coleta tudo, guarda histórico/auditoria, faz backup e sugere
limpeza; ações de escrita (add/remover/troca) são **manuais e unitárias**.
Cadastro de clientes (RH) é gravado em banco local. Host, credenciais e nomes
dos sites são do ambiente (não ficam no código).

## Decisões já tomadas (NÃO reverter sem pedir)
- **Somente leitura no controller**: a camada HTTP (`unifi/client.py::_request`)
  bloqueia qualquer método != GET. Não existem métodos de escrita no client.
- **Regra de disponibilidade = 35 dias** sem logar (à prova de férias).
  `NEVER_MODE=grace` (padrão): quem nunca conectou só vira "disponível" 35 dias
  após a 1ª coleta (protege cadastro novo). Alternativa: `NEVER_MODE=immediate`.
- **Desktop por enquanto**; Linux (multiusuário) **depois** (mesmo código Flask).
- Cadastro de cliente fica e é editável (banco local), separado da UniFi.
- **Unidade** é um **checklist** de números: 101,102,103,104,105,106,107,110,
  111,113,115,117 (configurável via env `UNITS`). Pessoa pode estar em várias.

## Mapeamento sites/unidades
Os sites são **descobertos automaticamente** do controller (não há nomes/IDs no
código). O mapeamento "nome da aba da planilha → número da unidade" usado na
importação fica em **`sites_map.json`** na raiz do projeto (arquivo LOCAL, fora
do versionamento). Veja `sites_map.json` para o de-para real do ambiente.
WLAN mobile por site: o nome contém **"MOBILE"** (`get_mobile_wlans`).
Limite da allow-list = **512** por WLAN.

## Arquitetura / arquivos
- `app.py` — Flask (web). Rotas: `/`, `/overview`, `/site/<id>`, `/clientes`,
  `/clientes/removidos`, `/cliente/<mac>` (GET/POST cadastro local), `/auditoria`,
  `/auditoria.csv`, `/refresh` (Atualizar status), `/backup.csv`,
  `/site/<id>/wlan/<wid>/export.csv`. Coleta automática throttle 120s.
  Caminhos robustos p/ rodar como .exe (FROZEN/_MEIPASS). `UNITS`, `NEVER_MODE`,
  `DB_PATH` lidos do ambiente.
- `desktop.py` — abre janela (pywebview). Modos: janela | `--collect` | `HEADLESS=1`.
- `collect.py` — coleta 1x e grava no banco (para agendar).
- `importar.py` — importa planilha .xlsx (dry-run; `--apply` grava).
- `cli.py` — CLI somente leitura (`sites`, `list`).
- `explore.py` — script de diagnóstico (descartável).
- `unifi/client.py` — cliente UniFi OS **read-only** (login/CSRF, get_sites,
  get_wlans, get_mobile_wlans, get_all_users, get_clients, normalize_mac).
- `unifi/inventory.py` — coleta detalhada (`snapshot_all`/`snapshot_site`) e
  classificação; captura name, hostname, oui, last_seen, first_seen, blocked.
- `unifi/db.py` — SQLite: merge histórico (maior last_seen), regra 35d,
  classificação, overview, dashboard por site, clientes, **auditoria (events)**,
  backup. Tabelas: `collections`, `mac_state`, `seen_history`, `client_info`,
  `events`. Migrações via `_migrate` (ALTER TABLE p/ hostname/first_seen/blocked).
- `unifi/sheet_import.py` — parser da planilha (mapeia por aba, MAC por regex,
  unidade pelo nome da aba, extras → notes; mescla MAC repetido em várias abas).
- `templates/` — base, index, overview, dashboard, clientes, cliente, auditoria.
- `static/style.css` — tema escuro + gráficos (donut/barras em CSS puro).
- `build_exe.ps1` — gera `dist\GestaoMAC.exe` (PyInstaller + pywebview).
- `.env` — credenciais (NÃO versionar). `.env.example` é o modelo.
- `data/history.db` — banco. `backups/` — backups CSV. `.venv` — Python 3.12.

## Funcionalidades prontas (todas LEITURA no controller)
- **Visão Geral**: cards, donut, distribuição, faixas de prioridade
  **>50d / >100d**, "vagas liberáveis por site", tabela por site ordenada por >100d.
- **Dashboard por site**: ocupação (ex.: 512/512), filtros **Todos / Sem uso /
  >50d / >100d / Online / Bloqueados**, ordenado do mais antigo; export CSV.
- **Clientes** (cadastro local): nome, setor, **unidade (checklist)**, função,
  líder, chamado, observações + infos do aparelho vindas do UniFi (nome do
  dispositivo, hostname, fabricante, 1º/último acesso, online, sites). Busca +
  filtro por unidade. **Removidos** com coluna "Removido em / Voltou em".
- **Status Bloqueado** em toda a leitura (card na visão geral, filtro/badge no
  dashboard, na ficha e no backup). É por (site, MAC).
- **Auditoria de eventos**: removido / voltou / bloqueado / desbloqueado /
  cadastrado, detectados entre coletas. Tela `/auditoria` + export CSV +
  timeline na ficha do cliente. (Baseline da 1ª coleta não gera ruído.)
- **Backup CSV** (botão) + cópia em `backups/`. **Atualizar status** (coleta).
- **Importação de planilha** concluída: arquivo `Wifi (1).xlsx`, **1.135 cadastros
  gravados** (1.067 ativos + 68 removidos com dados guardados).

## Login e credenciais (ATUALIZADO)
- **Login = conta do UniFi**: a tela `/login` valida usuário+senha **direto no
  controller** (`UnifiClient.login()` + `get_sites()`). Só contas autorizadas no
  UniFi entram. Não há mais admin/setup separado. `before_request` protege tudo
  (exceto `login`/`static`). Logout em `/logout`. Host fica em "Avançado" no login.
- No login bem-sucedido, host/site/usuário são salvos e a **senha é gravada
  criptografada** (para a coleta agendada funcionar). Hash scrypt do admin antigo
  ficou obsoleto (não usado).
- **Credenciais do UniFi**: tela `/config` (⚙ no menu). Host/site/usuário/senha
  ficam na tabela `settings`; a **senha é criptografada** (Fernet) com a chave
  `data/secret.key`. `unifi/config.py::resolve()` lê isso (e, na 1ª vez, SEMEIA
  do `.env` por compatibilidade). `unifi/secret.py` faz hash e cripto.
- O `.env` ainda pode ter a senha (semente). **Pode remover `UNIFI_PASSWORD` do
  `.env`** agora — ela já está no banco criptografada.
- `app.secret_key` é persistido em `settings.flask_secret` (sessões sobrevivem a
  restart). `collect.py` também usa `config.resolve()` (funciona sem .env).
- Arquivos novos: `unifi/secret.py`, `unifi/config.py`, `templates/login.html`,
  `templates/config.html`. Dep nova: `cryptography`.

## MAC VIP / Diretoria (NOVO)
- No cadastro do cliente há a opção **VIP** (`client_info.vip`). VIP = prioritário,
  não deve ser apagado.
- Se um MAC VIP sair da allow-list, a coleta gera o evento **`vip_removido`** e
  aparece um **banner de alerta vermelho** em todas as telas (`db.vip_alerts`),
  com link para os removidos VIP. O selo ★ aparece no dashboard (não remover),
  na lista de Clientes e na ficha. Filtro "★ VIP" na tela Clientes.
- Logos: UniFi (esquerda + ícone do .exe + favicon) e logo da empresa (direita,
  via `BRAND` no .env + `static/logo_brand.*` local, fora do versionamento).
  `static/unifi.ico` (quadrado 256), `static/logo_unifi.jpg`, `static/logo_brand.*` (local).
- Apontamento do banco no login (campo "Pasta do banco") -> grava `dbpath.cfg`
  ao lado do exe. Aviso de edição simultânea via tabela `edit_locks`.

## Pendências / ideias (próximos passos sugeridos)
1. Botão **"Importar planilha"** dentro da interface (upload, sem terminal).
2. Usar a coluna **MAC ANTERIOR** da planilha para **ligar trocas de aparelho**
   (vincular cadastro antigo ao MAC novo).
3. **Deploy Linux multiusuário** (gunicorn + systemd + cron). Ajustar
   `requirements.txt` (pywebview/pyinstaller são só do desktop; usar gunicorn no
   servidor). NÃO removido ainda para preservar o desktop.
4. Filtro **Bloqueados** também na tela Clientes.
5. **Export respeitando o filtro ativo** no dashboard.
6. Contador por unidade na Visão Geral.

## Como rodar (resumo)
Ver `COMO_RODAR.md`. Essência:
`.\.venv\Scripts\python.exe app.py` → http://127.0.0.1:5000
`.\.venv\Scripts\python.exe desktop.py` → janela
`.\.venv\Scripts\python.exe importar.py "Wifi (1).xlsx" [--apply]`
`.\.venv\Scripts\python.exe collect.py`

## Segurança
- Credenciais ficam no `.env` (usuário local do controller). A senha já trafegou
  no chat numa sessão anterior — **recomendado trocá-la** por precaução.
- Nada é escrito no controller por design.
