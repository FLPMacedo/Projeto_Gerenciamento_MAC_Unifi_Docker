# Implantação no Portainer

## 1. Antes de começar

No host que vai rodar a stack:

```bash
# o servidor PRECISA alcançar o controller UniFi
curl -sk -o /dev/null -w '%{http_code}\n' --max-time 10 https://SEU-CONTROLLER

# as portas do stack precisam estar livres
ss -ltn | grep -E ':8080|:8081' || echo "8080 e 8081 livres"
```

Se o controller não responder, a stack sobe mas **nada funciona**: o coletor não
coleta e ninguém consegue entrar — o login é validado contra o controller.

O host também precisa de saída para `github.com`, `pypi.org` e
`apt.postgresql.org`, porque a imagem é construída nele.

## 2. Criar a stack

**Stacks → Add stack → Repository**

| Campo | Valor |
|---|---|
| Name | `gestaomac` |
| Repository URL | `https://github.com/FLPMacedo/Projeto_Gerenciamento_MAC_Unifi_Docker` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |

## 3. Variáveis de ambiente

Em **Environment variables → Advanced mode**, cole o conteúdo abaixo com os
**seus** valores. Gere segredos novos — nunca reaproveite os de teste:

```bash
openssl rand -base64 24                                              # PGPASSWORD
openssl rand -hex 32                                                 # FLASK_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # CREDS_KEY
```

```
PGDATABASE=gestaomac
PGUSER=gestaomac
PGPASSWORD=<gere>
WEB_PORT=8080
PORTAL_PORT=8081
FLASK_SECRET_KEY=<gere>
CREDS_KEY=<gere>
TZ=America/Sao_Paulo
LOG_LEVEL=INFO
BRAND=<nome da empresa>
UNITS=101,102,103,104,105,106,107,110,111,113,115,117
NEVER_MODE=grace
COLLECT_INTERVAL=600
VOUCHER_MAX_QTD=200
UNIFI_HOST=https://SEU-CONTROLLER
UNIFI_SITE=<id interno do site padrão>
UNIFI_VERIFY_SSL=false
```

**`CREDS_KEY` importa.** Sem ela, a chave que cifra as senhas do UniFi é gerada
sozinha e guardada na tabela `settings` — ou seja, no mesmo banco do texto
cifrado. Definindo aqui, a chave nunca toca o banco.

**`TZ` importa.** As datas são formatadas com o fuso do container. Sem isso ele
roda em UTC e as telas mostram 3 horas a menos.

`UNIFI_HOST` e `UNIFI_SITE` são apenas semente do primeiro acesso: depois se
alteram pela tela de Configuração.

## 4. Deploy

**Deploy the stack.** O primeiro build leva alguns minutos (baixa a imagem base,
instala dependências e o `postgresql-client`).

Ordem esperada: `db` fica *healthy* → `init` roda e sai → `web`, `portal` e
`collector` sobem.

```bash
docker compose -p gestaomac logs init
# schema.sql aplicado / nenhuma migracao pendente / 15 tabelas / banco pronto
```

## 5. Trazer os dados do SQLite

Migre a partir do `history.db` da instalação desktop — não copie o Postgres de
teste. Assim a produção nasce sem artefato de teste.

```bash
# da sua estação
scp history_producao.db usuario@SERVIDOR:/tmp/

# no servidor
docker cp /tmp/history_producao.db gestaomac-web-1:/tmp/history.db
docker exec gestaomac-web-1 python /app/scripts/migrate_sqlite_to_pg.py \
    --sqlite /tmp/history.db --reset
```

Termina com **"Todas as contagens conferem"**. Referência: 557.740 linhas em
~5 s. `--reset` limpa as tabelas antes — é o que se quer num banco recém-criado.

### Conferir DEPOIS, com o sistema de pé

A mensagem de sucesso da migração **não é garantia**: ela só prova que a carga
funcionou naquele instante. Um redeploy que recrie o volume apaga tudo em
seguida, e o sistema continua parecendo saudável — a coleta nova enche a Visão
Geral normalmente. O que não volta são os cadastros e o histórico.

```bash
docker exec <stack>-db-1 psql -U gestaomac -d gestaomac -c \
"SELECT (SELECT COUNT(*) FROM client_info) AS cadastros,
        (SELECT COUNT(*) FROM seen_history) AS historico,
        (SELECT COUNT(*) FROM collections)  AS coletas;"
```

E abra a tela **Clientes**: os nomes das pessoas precisam aparecer.

**Sintoma de banco zerado no log do coletor:**

```
coleta #1: ...          <-- deveria continuar de onde a migração parou (#372)
... | N novos no log UniFi   <-- centenas de "novos" que já deveriam existir
```

Se a numeração recomeçar do 1, o banco foi apagado: refaça a migração.

> **Atualizar a stack não pode remover o volume `pgdata`.** É onde vivem os
> 1.169 cadastros de RH e as 371 coletas de histórico — nada disso se recupera
> do controller, porque só existe aqui. A regra dos 35 dias depende desse
> histórico: sem ele, todo MAC vira "novo" e ninguém aparece como liberável,
> **sem nenhuma mensagem de erro**.

## 6. Primeiro acesso

1. `http://SERVIDOR:8080` → entre com **sua conta do UniFi**.
   É esse login que grava a credencial cifrada e destrava o coletor, que até
   então registra *"nenhuma credencial disponivel ainda"*.
2. Confira a Visão geral: os números devem bater com a instalação antiga.
3. `docker compose -p gestaomac logs -f collector` → em até `COLLECT_INTERVAL`
   deve aparecer `coleta #N: ... linhas`.

## 7. Logo da empresa (opcional)

A logo não é versionada (o repositório é público). Para exibi-la:

```bash
docker exec gestaomac-web-1 mkdir -p /data/branding
docker cp logo_brand.png gestaomac-web-1:/data/branding/
```

Fica no volume `appdata` e sobrevive a redeploy. Sem arquivo, aparece só a
UniFi.

## 8. Usuários do portal

Em **Vouchers → Usuários do portal**, crie as contas de quem só retira voucher.
Cada uma recebe uma senha provisória e é obrigada a trocá-la no primeiro acesso,
em `http://SERVIDOR:8081`.

---

## Manutenção

**Atualizar o código:** commit e push aqui, depois **Pull and redeploy** no
Portainer. O volume `pgdata` persiste — os dados não se perdem.

**Alterar o schema:** crie um arquivo em `db/migrations/` e atualize também o
`db/schema.sql`. Ver [db/migrations/README.md](db/migrations/README.md).
`schema.sql` sozinho **não altera tabela existente**.

**Backup:** a tela Backup gera `.dump` do `pg_dump`. Inclua também o volume
`pgdata` na rotina do servidor.

```bash
# restaurar
docker exec -i gestaomac-db-1 pg_restore --clean --if-exists \
    -U gestaomac -d gestaomac < arquivo.dump
```

**Desligar a instalação antiga:** depois que a produção estiver validada, pare o
`.exe` desktop e a stack de teste — cada instância com banco próprio mantém um
coletor consultando o controller à toa.

## Se algo der errado

| Sintoma | Causa provável |
|---|---|
| `init` falha ao subir | `PGPASSWORD` vazio, ou `db` ainda não *healthy* |
| Login recusa todo mundo | servidor não alcança o `UNIFI_HOST` |
| Coletor repete "nenhuma credencial" | ninguém fez login ainda — é esperado |
| Datas 3 h atrasadas | `TZ` não definido |
| "Baixar banco" falha | `pg_dump` ausente; a imagem instala o `postgresql-client-17` da PGDG, então o host precisa de acesso a `apt.postgresql.org` no build |
| Portal mostra 404 em tudo | acessando a porta do painel de gestão; o portal é a `PORTAL_PORT` |
