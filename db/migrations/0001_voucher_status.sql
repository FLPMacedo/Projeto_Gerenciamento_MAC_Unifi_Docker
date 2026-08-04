-- Situacao de uso do voucher, lida do controller pelo coletor.
--
-- Ate aqui guardavamos so o que FOI GERADO. Sem saber quais ja foram usados,
-- imprimir um lote de 100 depois que 37 foram consumidos entrega uma folha em
-- que 37 codigos nao funcionam.
--
-- used      -> quantas vezes o voucher foi utilizado (0 = disponivel)
-- status    -> como a UniFi classifica: VALID_ONE, VALID_MULTI, USED_UPDATED,
--              EXPIRED. AUSENTE e nosso: o voucher sumiu do controller (a
--              UniFi expurga os vencidos), mas o registro fica aqui.
-- synced_at -> quando essa informacao foi conferida pela ultima vez

ALTER TABLE voucher_grants ADD COLUMN IF NOT EXISTS used      SMALLINT NOT NULL DEFAULT 0;
ALTER TABLE voucher_grants ADD COLUMN IF NOT EXISTS status    TEXT;
ALTER TABLE voucher_grants ADD COLUMN IF NOT EXISTS synced_at BIGINT;

-- a consulta mais frequente passa a ser "o que ainda da para entregar"
CREATE INDEX IF NOT EXISTS idx_vgrants_disponivel
    ON voucher_grants(site_id, used)
    WHERE revogado_em IS NULL;
