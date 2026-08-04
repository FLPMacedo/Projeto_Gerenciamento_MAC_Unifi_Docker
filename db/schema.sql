-- Schema PostgreSQL — Gestao MAC UniFi
--
-- Traducao do schema SQLite de unifi/db.py preservando a semantica original.
--
-- Decisao: os booleanos continuam SMALLINT 0/1 (e nao BOOLEAN). Motivo: as
-- consultas de agregacao usam MAX(in_allow_list) e MAX(blocked) (ver _AGG em
-- db.py); MAX() nao aceita boolean no PostgreSQL, exigiria bool_or() e mudaria
-- varias consultas. Manter SMALLINT mantem o codigo de leitura identico.
--
-- Decisao: os timestamps continuam BIGINT epoch (e nao timestamptz). Toda a
-- aplicacao e os templates tratam epoch; converter tocaria ~40 pontos sem ganho
-- funcional. Registrado como melhoria futura.

-- ---------------------------------------------------------------- coletas
CREATE TABLE IF NOT EXISTS collections(
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts BIGINT NOT NULL
);

-- Estado atual por (site, mac). O merge historico guarda sempre o MAIOR
-- last_seen ja observado — base da regra dos 35 dias.
CREATE TABLE IF NOT EXISTS mac_state(
    site_id         TEXT NOT NULL,
    site_desc       TEXT,
    wlan_id         TEXT,
    wlan_name       TEXT,
    mac             TEXT NOT NULL,
    name            TEXT,
    hostname        TEXT,
    oui             TEXT,
    in_allow_list   SMALLINT NOT NULL DEFAULT 1,
    blocked         SMALLINT NOT NULL DEFAULT 0,
    last_seen       BIGINT NOT NULL DEFAULT 0,   -- 0 = nunca visto
    last_online     BIGINT NOT NULL DEFAULT 0,
    first_seen      BIGINT NOT NULL DEFAULT 0,
    first_collected BIGINT NOT NULL,
    last_collected  BIGINT NOT NULL,
    PRIMARY KEY (site_id, mac)
);

CREATE TABLE IF NOT EXISTS seen_history(
    collection_id BIGINT,
    site_id       TEXT,
    mac           TEXT,
    online        SMALLINT,
    last_seen     BIGINT
);

-- Auditoria propria: cada transicao detectada entre coletas.
-- event: cadastrado | voltou | removido | vip_removido | bloqueado |
--        desbloqueado | add_manual | remove_manual | troca
CREATE TABLE IF NOT EXISTS events(
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts        BIGINT NOT NULL,
    site_id   TEXT,
    site_desc TEXT,
    mac       TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT
);

-- Espelho do log NATIVO de atividade da UniFi (preservado apos a purga deles)
CREATE TABLE IF NOT EXISTS unifi_audit(
    uid         TEXT PRIMARY KEY,   -- id do registro na UniFi (dedup)
    ts          BIGINT,
    site_id     TEXT,
    site_desc   TEXT,
    key         TEXT,
    operation   TEXT,
    actor       TEXT,
    message     TEXT,
    raw         TEXT,
    imported_at BIGINT
);

-- Cadastro do cliente (dados de RH por MAC). Persiste mesmo quando o MAC sai
-- da allow-list -> aparece em "Usuarios removidos" sem perder os dados.
CREATE TABLE IF NOT EXISTS client_info(
    mac              TEXT PRIMARY KEY,
    nome             TEXT,
    setor            TEXT,
    unidade          TEXT,
    funcao           TEXT,
    lider            TEXT,
    chamado          TEXT,
    notes            TEXT,
    gestor_autorizou TEXT,
    termo            SMALLINT NOT NULL DEFAULT 0,
    vip              SMALLINT NOT NULL DEFAULT 0,
    created_at       BIGINT,
    updated_at       BIGINT
);

-- Configuracoes gerais (flask_secret, host/site do UniFi, etc.)
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Credenciais por usuario (modelo hibrido).
--
-- Cada pessoa entra com a PROPRIA conta UniFi, como na versao desktop. A senha
-- e guardada cifrada (Fernet) para dois fins:
--   1) as telas e as acoes de escrita usarem a conta de quem esta logado, de
--      modo que o log nativo da UniFi registre o nome real do autor;
--   2) o coletor, que roda sem ninguem logado, ter uma credencial valida --
--      ele usa a mais recente que funcionou.
--
-- last_ok guarda a ultima vez que a credencial autenticou de fato: e por ele
-- que o coletor escolhe qual usar, preferindo a que comprovadamente funciona.
CREATE TABLE IF NOT EXISTS user_creds(
    username     TEXT PRIMARY KEY,
    host         TEXT NOT NULL,
    site         TEXT NOT NULL DEFAULT 'default',
    verify       SMALLINT NOT NULL DEFAULT 0,
    password_enc TEXT NOT NULL,
    updated_at   BIGINT,
    last_ok      BIGINT
);
CREATE INDEX IF NOT EXISTS idx_user_creds_ok ON user_creds(last_ok DESC);

-- ==================================================== vouchers de hotspot
-- Usuarios do PORTAL: autenticacao LOCAL, separada do login administrativo.
--
-- O admin entra com a conta UniFi (validada no controller). Quem so vai retirar
-- voucher -- portaria, recepcao, lider de unidade -- nao tem conta no
-- controller, entao precisa de credencial propria, criada pela TI aqui dentro.
--
-- A senha e guardada como HASH (scrypt, via werkzeug.security), nunca cifrada:
-- nao existe motivo para o sistema conseguir ler a senha de volta.
CREATE TABLE IF NOT EXISTS portal_users(
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    nome          TEXT,
    setor         TEXT,
    unidade       TEXT,
    password_hash TEXT NOT NULL,
    ativo         SMALLINT NOT NULL DEFAULT 1,
    -- obriga a trocar a senha no primeiro acesso (a TI define uma provisoria)
    must_change   SMALLINT NOT NULL DEFAULT 1,
    criado_por    TEXT,
    created_at    BIGINT,
    updated_at    BIGINT,
    last_login    BIGINT,
    -- freio contra tentativa de forca bruta: o portal usa senha simples, bem
    -- mais fraca que a validacao contra o controller do lado administrativo
    failed_count  SMALLINT NOT NULL DEFAULT 0,
    locked_until  BIGINT
);
CREATE INDEX IF NOT EXISTS idx_portal_users_ativo ON portal_users(ativo);

-- Vouchers gerados por nos e atribuidos nominalmente a alguem do portal.
--
-- A UniFi ja guarda o voucher e o admin_name de quem criou, mas expira e apaga
-- os antigos. Esta tabela preserva o historico -- quem gerou, para quem, com
-- que finalidade e quando a pessoa retirou -- do mesmo modo que ja fazemos com
-- o espelho do log nativo.
CREATE TABLE IF NOT EXISTS voucher_grants(
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code           TEXT NOT NULL,          -- codigo impresso do voucher
    voucher_id     TEXT,                   -- _id na UniFi (necessario p/ revogar)
    site_id        TEXT NOT NULL,
    site_desc      TEXT,
    note           TEXT,                   -- campo "Name" do formulario
    quota          SMALLINT,               -- 0=ilimitado | 1=uso unico | N=N usos
    duration_min   BIGINT,                 -- expiracao em MINUTOS (campo da API)
    data_limit_mb  BIGINT,
    down_kbps      BIGINT,
    up_kbps        BIGINT,
    portal_user_id BIGINT REFERENCES portal_users(id) ON DELETE SET NULL,
    criado_por     TEXT,                   -- conta UniFi do admin que gerou
    create_time    BIGINT,                 -- create_time devolvido pela UniFi
    created_at     BIGINT,
    retirado_em    BIGINT,                 -- quando a pessoa viu o codigo no portal
    revogado_em    BIGINT,
    revogado_por   TEXT,
    -- Situacao de uso, sincronizada do controller pelo coletor (ver
    -- db/migrations/0001_voucher_status.sql). Sem isso nao da para saber
    -- quais codigos ainda funcionam na hora de imprimir um lote.
    used           SMALLINT NOT NULL DEFAULT 0,  -- 0 = ainda nao usado
    status         TEXT,                   -- VALID_ONE | USED_UPDATED | EXPIRED | AUSENTE
    synced_at      BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vgrants_code ON voucher_grants(site_id, code);
CREATE INDEX IF NOT EXISTS idx_vgrants_user ON voucher_grants(portal_user_id);
CREATE INDEX IF NOT EXISTS idx_vgrants_criado ON voucher_grants(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vgrants_disponivel
    ON voucher_grants(site_id, used) WHERE revogado_em IS NULL;

-- ------------------------------------------------------- estado efemero
-- Estas tres tabelas expiram sozinhas por TTL (20s a 180s). Sao migradas por
-- fidelidade, mas qualquer residuo se resolve em menos de 3 minutos.

-- Aviso de edicao simultanea no cadastro do cliente (TTL 180s)
CREATE TABLE IF NOT EXISTS edit_locks(
    mac TEXT PRIMARY KEY,
    who TEXT,
    ts  BIGINT
);

-- Presenca: sessoes ativas do app ("N conectados") (TTL 90s)
CREATE TABLE IF NOT EXISTS active_sessions(
    sid       TEXT PRIMARY KEY,
    who       TEXT,
    machine   TEXT,
    last_ping BIGINT
);

-- Trava de escrita por WLAN: serializa add/remover/troca no mesmo site (TTL 20s)
CREATE TABLE IF NOT EXISTS wlan_locks(
    key TEXT PRIMARY KEY,
    who TEXT,
    ts  BIGINT
);

-- ----------------------------------------------------- lease de coleta
-- Substitui o antigo settings['last_collect_ts'].
--
-- O codigo SQLite fazia: WHERE CAST(value AS INTEGER) <= ?  sobre a coluna TEXT
-- de settings. O SQLite devolve 0 silenciosamente para valor vazio/nao-numerico;
-- o PostgreSQL LANCA EXCECAO. Coluna BIGINT dedicada resolve na raiz.
--
-- O mecanismo (UPDATE atomico condicional, vencedor unico pelo rowcount)
-- continua identico e permanece correto no PostgreSQL.
CREATE TABLE IF NOT EXISTS collect_lease(
    id      SMALLINT PRIMARY KEY,
    last_ts BIGINT NOT NULL DEFAULT 0,
    last_by TEXT,
    CONSTRAINT collect_lease_singleton CHECK (id = 1)
);
INSERT INTO collect_lease(id, last_ts) VALUES (1, 0)
    ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------- indices
CREATE INDEX IF NOT EXISTS idx_events_mac    ON events(mac);
CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS idx_uaudit_ts     ON unifi_audit(ts);
CREATE INDEX IF NOT EXISTS idx_state_site    ON mac_state(site_id, in_allow_list);
CREATE INDEX IF NOT EXISTS idx_state_mac     ON mac_state(mac);
CREATE INDEX IF NOT EXISTS idx_state_allow   ON mac_state(in_allow_list);
CREATE INDEX IF NOT EXISTS idx_client_vip    ON client_info(vip) WHERE vip = 1;
-- seen_history nao tinha indice no SQLite; com 550k linhas passa a ter.
CREATE INDEX IF NOT EXISTS idx_seen_coll     ON seen_history(collection_id);
CREATE INDEX IF NOT EXISTS idx_seen_mac      ON seen_history(mac);
