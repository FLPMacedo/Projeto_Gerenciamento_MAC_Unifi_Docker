# v2 — Multiusuário com banco em pasta de rede

Vários computadores rodando o `GestaoMAC.exe`, todos lendo/gravando o **mesmo
banco** numa **pasta de rede compartilhada**. Acesso simultâneo suportado.

## Como o SQLite lida com isso
- O banco fica num arquivo único (`history.db`) numa pasta UNC (ex.:
  `\\SERVIDOR\Compartilhada\GestaoMAC\data\history.db`).
- Leitura simultânea: sem problema.
- Escrita: uma de cada vez. O sistema usa **busy_timeout (20s)** — se duas
  gravações coincidirem, uma espera em vez de dar erro.
- **Não** usamos WAL (não funciona em compartilhamento SMB); usamos o modo
  padrão (journal), que é o seguro para pasta de rede.

> Recomendado para uma equipe (poucas dezenas de usuários, muita leitura e
> escrita pontual). Para concorrência pesada, o ideal seria migrar para
> PostgreSQL — mas para este uso, SQLite em rede atende bem.

## Arquitetura recomendada
1. **Pasta de rede** (ex.: `\\SERVIDOR\Compartilhada\GestaoMAC\`):
   ```
   GestaoMAC\
    └─ data\
        ├─ history.db     (banco compartilhado)
        └─ secret.key     (chave que descriptografa a senha do UniFi)
   ```
   Dê permissão de **leitura/escrita** a todos os usuários nessa pasta.

2. **Coletor central** (1 máquina só — pode ser o servidor ou um PC fixo):
   - `.env` com:
     ```
     DB_PATH=\\SERVIDOR\Compartilhada\GestaoMAC\data\history.db
     COLLECT_ON_OPEN=1
     ```
   - Agende o `collect.py` (ou `GestaoMAC.exe` aberto nessa máquina) para
     coletar de tempos em tempos. Só ele escreve as coletas → menos disputa.
     ```powershell
     schtasks /Create /TN "UnifiMacCollect" /SC HOURLY /MO 2 `
       /TR "'C:\caminho\GestaoMAC.exe' --collect"   # ou collect.py via python
     ```

3. **PCs dos usuários** (quantos quiser):
   - Copie o `GestaoMAC.exe` para cada PC (NÃO precisa da pasta data local).
   - `.env` ao lado do exe com:
     ```
     DB_PATH=\\SERVIDOR\Compartilhada\GestaoMAC\data\history.db
     COLLECT_ON_OPEN=0
     ```
   - `COLLECT_ON_OPEN=0` faz o app **só ler** (não grava coletas) — evita vários
     PCs escrevendo ao mesmo tempo. Eles ainda fazem cadastros/auditoria normalmente.

## Login (v2)
- O acesso é a **conta do UniFi** (validada no controller a cada login).
- A **senha do UniFi é salva (criptografada) no 1º login** e mantida até ser
  alterada na tela **⚙ Configuração**. Logins seguintes só validam o acesso.
- A `secret.key` precisa estar na pasta de rede (junto do banco) para todos
  conseguirem descriptografar a senha do serviço.

## Backup
- Pela tela **⬇ Backup → Baixar banco (.db)** (cópia consistente, mesmo em uso).
- Ou copie o `history.db` da pasta de rede (de preferência fora do horário de pico).

## Resumo do que muda por máquina
| Máquina            | DB_PATH            | COLLECT_ON_OPEN |
|--------------------|--------------------|-----------------|
| Coletor central    | pasta de rede      | 1               |
| PCs dos usuários   | pasta de rede      | 0               |
| Uso single (1 PC)  | local (padrão)     | 1               |
