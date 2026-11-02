-- Esquema de ms-recursos. Se aplica IGUAL en los 3 schemas de tenant
-- (t_acme, t_globex, t_initech) dentro de la database ppm_recursos.

-- -------------------------------------------------------------------- recursos
-- Las personas de la organizacion vistas como CAPACIDAD DE TRABAJO: cuantas
-- horas tienen por semana y cuanto cuesta su hora. No es un perfil ni un
-- directorio: es el insumo para saber quien puede tomar mas trabajo.
CREATE TABLE IF NOT EXISTS recursos (
    id                      BIGSERIAL PRIMARY KEY,
    usuario_id              UUID        NOT NULL UNIQUE,  -- logico, de ms-identidad
    nombre                  TEXT        NOT NULL,
    rol_tecnico             TEXT        NOT NULL,
    costo_hora              NUMERIC(10,2) NOT NULL DEFAULT 0,
    capacidad_horas_semana  NUMERIC(6,2) NOT NULL DEFAULT 40,
    activo                  BOOLEAN     NOT NULL DEFAULT TRUE,
    creado_en               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (capacidad_horas_semana > 0),
    CHECK (costo_hora >= 0)
);

CREATE INDEX IF NOT EXISTS ix_recursos_rol ON recursos (rol_tecnico);

-- ---------------------------------------------------------------- asignaciones
CREATE TABLE IF NOT EXISTS asignaciones (
    id                      BIGSERIAL PRIMARY KEY,
    recurso_id              BIGINT      NOT NULL REFERENCES recursos(id) ON DELETE CASCADE,
    proyecto_id             BIGINT      NOT NULL,   -- logico, de ms-portafolios
    codigo_proyecto         TEXT,
    porcentaje_dedicacion   NUMERIC(5,2) NOT NULL,
    fecha_inicio            DATE        NOT NULL,
    fecha_fin               DATE,
    activa                  BOOLEAN     NOT NULL DEFAULT TRUE,
    asignado_por            UUID,
    creado_en               TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (porcentaje_dedicacion > 0 AND porcentaje_dedicacion <= 100),
    CHECK (fecha_fin IS NULL OR fecha_fin >= fecha_inicio)
);

CREATE INDEX IF NOT EXISTS ix_asignaciones_recurso  ON asignaciones (recurso_id, activa);
CREATE INDEX IF NOT EXISTS ix_asignaciones_proyecto ON asignaciones (proyecto_id, activa);

-- ------------------------------------------------------------------ vista carga
-- La vista de Workload que tienen monday, Smartsheet y Planview: cada persona
-- con su porcentaje de ocupacion, en rojo si pasa del 100%.
CREATE OR REPLACE VIEW carga AS
SELECT  r.id                AS recurso_id,
        r.usuario_id,
        r.nombre,
        r.rol_tecnico,
        r.costo_hora,
        r.capacidad_horas_semana,
        r.activo,
        COALESCE(SUM(a.porcentaje_dedicacion), 0)               AS ocupacion_pct,
        COALESCE(SUM(a.porcentaje_dedicacion), 0) > 100          AS sobreasignado,
        GREATEST(100 - COALESCE(SUM(a.porcentaje_dedicacion), 0), 0) AS disponible_pct,
        ROUND(r.capacidad_horas_semana
              * COALESCE(SUM(a.porcentaje_dedicacion), 0) / 100, 2) AS horas_comprometidas,
        COUNT(a.id)                                              AS n_asignaciones
FROM    recursos r
LEFT JOIN asignaciones a
       ON a.recurso_id = r.id
      AND a.activa
      -- Solo las asignaciones VIGENTES hoy: una que ya termino no ocupa
      -- capacidad.
      AND a.fecha_inicio <= CURRENT_DATE
      AND (a.fecha_fin IS NULL OR a.fecha_fin >= CURRENT_DATE)
GROUP BY r.id;
