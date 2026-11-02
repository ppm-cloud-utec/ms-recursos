"""Asignacion de personas a proyectos, con deteccion de sobreasignacion.

Aqui esta la funcion que el producto vende: si Ana queda al 150% entre dos
proyectos, el sistema AVISA en vez de esperar a que ella se queme.
"""
from psycopg.rows import dict_row

from src import db, roles
from src.eventos import publicar
from src.respuesta import cuerpo_de, error, ok, tenant_de, usuario_de


def _ocupacion(cur, recurso_id):
    """Porcentaje ocupado hoy por las asignaciones vigentes."""
    cur.execute("""
        SELECT COALESCE(SUM(porcentaje_dedicacion), 0) AS pct
        FROM   asignaciones
        WHERE  recurso_id = %s AND activa
          AND  fecha_inicio <= CURRENT_DATE
          AND  (fecha_fin IS NULL OR fecha_fin >= CURRENT_DATE)
    """, (recurso_id,))
    return float(cur.fetchone()["pct"])


def listar(event, context):
    """GET /asignaciones?proyecto_id=&recurso_id=&activa=true"""
    q = event.get("queryStringParameters") or {}
    filtros, valores = ["TRUE"], []
    for campo in ("proyecto_id", "recurso_id"):
        if q.get(campo):
            filtros.append(f"a.{campo} = %s")
            valores.append(q[campo])
    if q.get("activa", "true") == "true":
        filtros.append("a.activa")
    where = " AND ".join(filtros)

    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(f"""
                SELECT a.*, r.nombre AS recurso, r.rol_tecnico, r.costo_hora,
                       ROUND(r.capacidad_horas_semana
                             * a.porcentaje_dedicacion / 100, 2) AS horas_semana
                FROM   asignaciones a
                JOIN   recursos r ON r.id = a.recurso_id
                WHERE  {where}
                ORDER BY a.fecha_inicio DESC, a.id DESC
                LIMIT 300
            """, valores)
            filas = cur.fetchall()
    return ok(filas, meta={"total": len(filas)})


def crear(event, context):
    """POST /asignaciones — publica recurso.asignado y, si aplica,
    recurso.sobreasignado.

    No se BLOQUEA la sobreasignacion: a veces es una decision consciente del
    PM. Lo que hace el sistema es registrarla y avisar, que es lo que pide el
    producto.
    """
    negado = roles.exige(event, roles.PM, roles.PMO, roles.ADMIN)
    if negado:
        return negado

    tenant_id = tenant_de(event)
    c = cuerpo_de(event)

    faltan = [k for k in ("recurso_id", "proyecto_id", "porcentaje_dedicacion",
                          "fecha_inicio") if c.get(k) in (None, "")]
    if faltan:
        return error("DATOS_INCOMPLETOS", f"Falta: {', '.join(faltan)}")

    try:
        dedicacion = float(c["porcentaje_dedicacion"])
    except (TypeError, ValueError):
        return error("DEDICACION_INVALIDA", "El porcentaje debe ser numerico")
    if not 0 < dedicacion <= 100:
        return error("DEDICACION_INVALIDA",
                     "El porcentaje de dedicacion va de 1 a 100")

    with db.conexion(tenant_id) as con:
        with con.cursor(row_factory=dict_row) as cur:
            # FOR UPDATE sobre el recurso: dos asignaciones simultaneas
            # leerian la misma ocupacion y ninguna detectaria el exceso.
            cur.execute("""
                SELECT id, usuario_id, nombre, costo_hora, capacidad_horas_semana
                FROM   recursos WHERE id = %s AND activo FOR UPDATE
            """, (c["recurso_id"],))
            recurso = cur.fetchone()
            if not recurso:
                return error("NO_ENCONTRADO",
                             f"No existe el recurso activo {c['recurso_id']}", 404)

            antes = _ocupacion(cur, recurso["id"])

            cur.execute("""
                INSERT INTO asignaciones
                    (recurso_id, proyecto_id, codigo_proyecto,
                     porcentaje_dedicacion, fecha_inicio, fecha_fin, asignado_por)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
            """, (recurso["id"], c["proyecto_id"], c.get("codigo_proyecto"),
                  dedicacion, c["fecha_inicio"], c.get("fecha_fin"),
                  usuario_de(event)))
            asignacion = cur.fetchone()

            despues = _ocupacion(cur, recurso["id"])

    horas = float(recurso["capacidad_horas_semana"]) * dedicacion / 100
    semanas = 12
    if asignacion["fecha_fin"]:
        semanas = max(1, (asignacion["fecha_fin"] - asignacion["fecha_inicio"]).days // 7)

    # ms-presupuestos escucha esto para proyectar el costo de horas-hombre
    publicar("recurso.asignado", tenant_id, {
        "recurso_id": str(recurso["id"]),
        "usuario_id": str(recurso["usuario_id"]),
        "proyecto_id": int(c["proyecto_id"]),
        "asignacion_id": asignacion["id"],
        "porcentaje_dedicacion": dedicacion,
        "horas_semana": round(horas, 2),
        "costo_hora": float(recurso["costo_hora"]),
        "semanas": semanas,
    })

    # Solo al CRUZAR el 100%, no en cada asignacion posterior
    if despues > 100 >= antes:
        publicar("recurso.sobreasignado", tenant_id, {
            "recurso_id": str(recurso["id"]),
            "nombre": recurso["nombre"],
            "ocupacion_pct": despues,
            "exceso_pct": round(despues - 100, 2),
            "proyecto_id": int(c["proyecto_id"]),
        })

    return ok({
        "asignacion": asignacion,
        "recurso": {
            "id": recurso["id"],
            "nombre": recurso["nombre"],
            "ocupacion_anterior_pct": antes,
            "ocupacion_pct": despues,
            "sobreasignado": despues > 100,
            "exceso_pct": round(max(0.0, despues - 100), 2),
        },
    }, estado=201)


def liberar(event, context):
    """DELETE /asignaciones/{id} — baja logica: libera capacidad sin perder
    el historial de quien estuvo en que proyecto.
    """
    negado = roles.exige(event, roles.PM, roles.PMO, roles.ADMIN)
    if negado:
        return negado

    aid = event["pathParameters"]["id"]
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                UPDATE asignaciones SET activa = FALSE
                WHERE  id = %s AND activa
                RETURNING recurso_id, proyecto_id
            """, (aid,))
            asignacion = cur.fetchone()
            if not asignacion:
                return error("NO_ENCONTRADO",
                             f"La asignacion {aid} no existe o ya estaba liberada",
                             404)
            ocupacion = _ocupacion(cur, asignacion["recurso_id"])

    return ok({"asignacion_id": int(aid), "activa": False,
               "ocupacion_pct": ocupacion})
