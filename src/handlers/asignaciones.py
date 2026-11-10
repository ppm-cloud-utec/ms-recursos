"""Asignacion de personas a proyectos, con deteccion de sobreasignacion.

Aqui esta la funcion que el producto vende: si Ana queda al 150% entre dos
proyectos, el sistema AVISA en vez de esperar a que ella se queme.
"""
import math

from psycopg.rows import dict_row

from src import db, roles
from src import validar as v
from src.eventos import publicar
from src.respuesta import error, ok, tenant_de, usuario_de

SEMANAS_SIN_FECHA_FIN = 12   # horizonte de proyeccion si la asignacion no tiene fin


def _ocupacion(cur, recurso_id, fecha=None):
    """Porcentaje ocupado en `fecha` (hoy si es None) por las asignaciones
    activas que la cubren."""
    cur.execute("""
        SELECT COALESCE(SUM(porcentaje_dedicacion), 0) AS pct
        FROM   asignaciones
        WHERE  recurso_id = %s AND activa
          AND  fecha_inicio <= COALESCE(%s::date, CURRENT_DATE)
          AND  (fecha_fin IS NULL OR fecha_fin >= COALESCE(%s::date, CURRENT_DATE))
    """, (recurso_id, fecha, fecha))
    return float(cur.fetchone()["pct"])


@v.protegido
def listar(event, context):
    """GET /asignaciones?proyecto_id=&recurso_id=&activa=true"""
    q = event.get("queryStringParameters") or {}
    filtros, valores = ["TRUE"], []
    for campo in ("proyecto_id", "recurso_id"):
        if q.get(campo):
            filtros.append(f"a.{campo} = %s")
            valores.append(v.entero(q[campo], campo))
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


def _semanas(inicio, fin):
    """Semanas que dura la asignacion, redondeando hacia arriba.

    Del 1 al 7 de un mes son 7 dias = 1 semana; del 1 al 8, 2 semanas.
    """
    if fin is None:
        return SEMANAS_SIN_FECHA_FIN
    return max(1, math.ceil(((fin - inicio).days + 1) / 7))


@v.protegido
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
    c = v.cuerpo(event)

    faltan = [k for k in ("recurso_id", "proyecto_id", "porcentaje_dedicacion",
                          "fecha_inicio") if c.get(k) in (None, "")]
    if faltan:
        return error("DATOS_INCOMPLETOS", f"Falta: {', '.join(faltan)}")

    recurso_id = v.entero(c["recurso_id"], "recurso_id")
    proyecto_id = v.entero(c["proyecto_id"], "proyecto_id")
    try:
        dedicacion = v.numero(c["porcentaje_dedicacion"], "porcentaje_dedicacion",
                              minimo=0, incluye_minimo=False, maximo=100)
    except v.EntradaInvalida:
        return error("DEDICACION_INVALIDA",
                     "El porcentaje de dedicacion va de 1 a 100")
    inicio = v.fecha(c["fecha_inicio"], "fecha_inicio")
    fin = v.fecha(c["fecha_fin"], "fecha_fin") if c.get("fecha_fin") else None
    if fin and fin < inicio:
        return error("FECHAS_INVALIDAS",
                     "fecha_fin no puede ser anterior a fecha_inicio")
    codigo = c.get("codigo_proyecto")
    codigo = str(codigo).strip() if codigo not in (None, "") else None
    # asignado_por es UUID en la tabla. El `sub` del JWT de ms-identidad
    # deberia serlo; si no lo es, se guarda NULL en vez de tumbar el INSERT.
    asignado_por = _uuid_o_none(usuario_de(event))

    with db.conexion(tenant_id) as con:
        with con.cursor(row_factory=dict_row) as cur:
            # FOR UPDATE sobre el recurso: dos asignaciones simultaneas
            # leerian la misma ocupacion y ninguna detectaria el exceso.
            cur.execute("""
                SELECT id, usuario_id, nombre, costo_hora, capacidad_horas_semana
                FROM   recursos WHERE id = %s AND activo FOR UPDATE
            """, (recurso_id,))
            recurso = cur.fetchone()
            if not recurso:
                return error("NO_ENCONTRADO",
                             f"No existe el recurso activo {recurso_id}", 404)

            # La ocupacion se mide el dia en que la asignacion empieza a pesar:
            # hoy si ya empezo, o su fecha de inicio si es a futuro. Medirla
            # siempre "hoy" haria que una asignacion que arranca la proxima
            # semana nunca dispare la alerta.
            cur.execute("SELECT GREATEST(CURRENT_DATE, %s::date) AS f", (inicio,))
            referencia = cur.fetchone()["f"]
            if fin and fin < referencia:
                referencia = None   # ya termino: no ocupa capacidad hoy

            antes = _ocupacion(cur, recurso["id"], referencia)

            cur.execute("""
                INSERT INTO asignaciones
                    (recurso_id, proyecto_id, codigo_proyecto,
                     porcentaje_dedicacion, fecha_inicio, fecha_fin, asignado_por)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
            """, (recurso["id"], proyecto_id, codigo, dedicacion, inicio, fin,
                  asignado_por))
            asignacion = cur.fetchone()

            despues = _ocupacion(cur, recurso["id"], referencia)
    # <- aqui ya se hizo COMMIT. Recien ahora se publican los eventos.

    horas = float(recurso["capacidad_horas_semana"]) * dedicacion / 100

    # ms-presupuestos escucha esto para proyectar el costo de horas-hombre
    publicar("recurso.asignado", tenant_id, {
        "recurso_id": str(recurso["id"]),
        "usuario_id": str(recurso["usuario_id"]),
        "proyecto_id": proyecto_id,
        "asignacion_id": asignacion["id"],
        "porcentaje_dedicacion": dedicacion,
        "horas_semana": round(horas, 2),
        "costo_hora": float(recurso["costo_hora"]),
        "semanas": _semanas(asignacion["fecha_inicio"], asignacion["fecha_fin"]),
    })

    # Solo al CRUZAR el 100%, no en cada asignacion posterior
    alerta = despues > 100 >= antes
    if alerta:
        publicar("recurso.sobreasignado", tenant_id, {
            "recurso_id": str(recurso["id"]),
            "usuario_id": str(recurso["usuario_id"]),
            "nombre": recurso["nombre"],
            "ocupacion_anterior_pct": antes,
            "ocupacion_pct": despues,
            "exceso_pct": round(despues - 100, 2),
            "proyecto_id": proyecto_id,
            "asignacion_id": asignacion["id"],
            "fecha": referencia,
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
            "fecha_referencia": referencia,
            "alerta_publicada": alerta,
        },
    }, estado=201)


def _uuid_o_none(valor):
    try:
        return v.uuid_(valor, "usuario_id")
    except v.EntradaInvalida:
        return None


@v.protegido
def liberar(event, context):
    """DELETE /asignaciones/{id} — baja logica: libera capacidad sin perder
    el historial de quien estuvo en que proyecto.
    """
    negado = roles.exige(event, roles.PM, roles.PMO, roles.ADMIN)
    if negado:
        return negado

    aid = v.entero(event["pathParameters"]["id"], "id")
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

    return ok({"asignacion_id": aid, "activa": False,
               "recurso_id": asignacion["recurso_id"],
               "ocupacion_pct": ocupacion})
