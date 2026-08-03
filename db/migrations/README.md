# Migrações de schema

Alterações **em tabelas que já existem**. Rodam no `init`, uma vez cada, na
ordem do número do arquivo.

## Por que isto existe

`db/schema.sql` é a fotografia do schema atual e só tem `CREATE TABLE IF NOT
EXISTS`. Ele cria o que falta, mas **não altera o que já está lá**. Sem as
migrações, acrescentar uma coluna funcionaria num banco novo e passaria
silenciosamente em branco num banco com dados — o `init` rodaria, não
reclamaria de nada, e a coluna simplesmente não existiria em produção.

A versão desktop resolvia isso com `ALTER TABLE` dentro de `_migrate()`; foi
assim que `hostname`, `first_seen`, `blocked` e `vip` entraram ao longo do
tempo.

## Como criar uma

1. Crie `NNNN_descricao_curta.sql` (número com 4 dígitos, em sequência).
2. **Atualize também o `db/schema.sql`**, para que um banco novo já nasça certo.
3. Escreva SQL idempotente sempre que der (`IF NOT EXISTS`, `IF EXISTS`).

```sql
-- 0001_client_info_matricula.sql
ALTER TABLE client_info ADD COLUMN IF NOT EXISTS matricula TEXT;
CREATE INDEX IF NOT EXISTS idx_client_matricula ON client_info(matricula);
```

## Regras que o runner impõe

- **Cada migração roda na própria transação.** Se a 3 falhar, a 1 e a 2
  continuam aplicadas e o log aponta onde parou.
- **Migração aplicada não pode ser editada.** O runner guarda um checksum e
  aborta se o arquivo mudou depois de aplicado — senão os bancos ficariam
  diferentes entre si sem ninguém perceber. Precisa corrigir? Crie uma nova.
- **Banco novo não executa migração.** Se o banco estava vazio, o `schema.sql`
  já trouxe tudo e as migrações existentes são apenas *marcadas* como
  aplicadas. Sem isso, um banco novo tentaria dar `ALTER` numa coluna que já
  nasceu correta.

## Conferir o que foi aplicado

```bash
docker compose exec -T db psql -U gestaomac -d gestaomac \
  -c "SELECT versao, nome, to_timestamp(aplicada_em) AS quando, baseline
      FROM schema_migrations ORDER BY versao;"
```

A coluna `baseline` em 1 significa "registrada sem executar, porque o banco
nasceu já com essa alteração".
