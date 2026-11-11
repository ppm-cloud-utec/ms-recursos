"""Personas, capacidad y asignaciones.

Los "recursos" son los empleados de la organizacion vistos como capacidad de
trabajo asignable: cuantas horas tienen por semana y a que proyectos estan
dedicados. No es un directorio de perfiles.
"""
from psycopg.rows import dict_row

from src import db, roles
from src import validar as v
from src.respuesta import error, ok, tenant_de

MAX_HORAS_SEMANA = 168      # una semana tiene 168 horas


@v.protegido
def listar(event, context):
    """GET /recursos?rol=&sobreasignados=true&disponibles=true&limite=&pagina="""
    q = event.get("queryStringParameters") or {}
    limite = min(v.entero(q.get("limite", 100), "limite"), 300)
    pagina = v.entero(q.get("pagina", 1), "pagina")

    filtros, valores = ["activo"], []
    if q.get("rol"):
        filtros.append("rol_tecnico = %s")
        valores.append(q["rol"])
    if q.get("sobreasignados") == "true":
        filtros.append("sobreasignado")
    if q.get("disponibles") == "true":
        filtros.append("disponible_pct > 0")
    where = " AND ".join(filtros)

    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM carga WHERE {where}", valores)
            total = cur.fetchone()["n"]
            cur.execute(f"""
                SELECT * FROM carga WHERE {where}
                ORDER BY ocupacion_pct DESC, nombre, recurso_id
                LIMIT %s OFFSET %s
            """, valores + [limite, (pagina - 1) * limite])
            filas = cur.fetchall()

    return ok(filas, meta={"total": total, "pagina": pagina, "limite": limite})


@v.protegido
def obtener(event, context):
    """GET /recursos/{id} — con sus asignaciones (vigentes primero)."""
    rid = v.entero(event["pathParameters"]["id"], "id")
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM carga WHERE recurso_id = %s", (rid,))
            recurso = cur.fetchone()
            if not recurso:
                return error("NO_ENCONTRADO", f"No existe el recurso {rid}", 404)
            cur.execute("""
                SELECT id, proyecto_id, codigo_proyecto, porcentaje_dedicacion,
                       fecha_inicio, fecha_fin, activa,
                       (activa
                        AND fecha_inicio <= CURRENT_DATE
                        AND (fecha_fin IS NULL OR fecha_fin >= CURRENT_DATE))
                           AS vigente
                FROM   asignaciones WHERE recurso_id = %s
                ORDER BY vigente DESC, activa DESC, fecha_inicio DESC, id DESC
            """, (rid,))
            recurso["asignaciones"] = cur.fetchall()
    return ok(recurso)


def _campos_recurso(c, parcial):
    """Valida y normaliza los campos de un recurso.

    parcial=True (PUT): solo valida lo que venga en el body.
    """
    datos = {}
    if not parcial or "nombre" in c:
        datos["nombre"] = v.texto(c.get("nombre"), "nombre")
    if not parcial or "rol_tecnico" in c:
        datos["rol_tecnico"] = v.texto(c.get("rol_tecnico"), "rol_tecnico")
    if "costo_hora" in c or not parcial:
        datos["costo_hora"] = v.numero(c.get("costo_hora", 0), "costo_hora",
                                       minimo=0)
    if "capacidad_horas_semana" in c or not parcial:
        datos["capacidad_horas_semana"] = v.numero(
            c.get("capacidad_horas_semana", 40), "capacidad_horas_semana",
            minimo=0, incluye_minimo=False, maximo=MAX_HORAS_SEMANA)
    if parcial and "activo" in c:
        datos["activo"] = v.booleano(c["activo"], "activo")
    return datos


@v.protegido
def crear(event, context):
    """POST /recursos — solo admin y pmo registran capacidad."""
    negado = roles.exige(event, roles.ADMIN, roles.PMO)
    if negado:
        return negado

    c = v.cuerpo(event)
    faltan = [k for k in ("usuario_id", "nombre", "rol_tecnico") if not c.get(k)]
    if faltan:
        return error("DATOS_INCOMPLETOS", f"Falta: {', '.join(faltan)}")

    usuario_id = v.uuid_(c["usuario_id"], "usuario_id")
    d = _campos_recurso(c, parcial=False)

    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                INSERT INTO recursos
                    (usuario_id, nombre, rol_tecnico, costo_hora,
                     capacidad_horas_semana)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id) DO NOTHING
                RETURNING *
            """, (usuario_id, d["nombre"], d["rol_tecnico"], d["costo_hora"],
                  d["capacidad_horas_semana"]))
            recurso = cur.fetchone()

    if not recurso:
        return error("RECURSO_DUPLICADO",
                     f"El usuario {usuario_id} ya esta registrado", 409)
    return ok(recurso, estado=201)


@v.protegido
def actualizar(event, context):
    """PUT /recursos/{id} — actualizacion parcial."""
    negado = roles.exige(event, roles.ADMIN, roles.PMO)
    if negado:
        return negado

    rid = v.entero(event["pathParameters"]["id"], "id")
    c = v.cuerpo(event)
    permitidos = ("nombre", "rol_tecnico", "costo_hora",
                  "capacidad_horas_semana", "activo")
    if not any(k in c for k in permitidos):
        return error("SIN_CAMBIOS", f"Envia alguno de: {', '.join(permitidos)}")
    cambios = _campos_recurso(c, parcial=True)

    # Los nombres de columna salen de la lista blanca `permitidos`, nunca
    # del body, asi que el f-string no abre una inyeccion SQL.
    sets = ", ".join(f"{k} = %s" for k in cambios)
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(f"UPDATE recursos SET {sets} WHERE id = %s RETURNING *",
                        list(cambios.values()) + [rid])
            recurso = cur.fetchone()

    if not recurso:
        return error("NO_ENCONTRADO", f"No existe el recurso {rid}", 404)
    return ok(recurso)


@v.protegido
def carga(event, context):
    """GET /carga — la vista de Workload, con el total del equipo."""
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT recurso_id, usuario_id, nombre, rol_tecnico,
                       capacidad_horas_semana, ocupacion_pct, disponible_pct,
                       horas_comprometidas, sobreasignado, n_asignaciones
                FROM   carga WHERE activo
                ORDER BY ocupacion_pct DESC, nombre, recurso_id
            """)
            personas = cur.fetchall()
            cur.execute("""
                SELECT  COUNT(*)                                       AS personas,
                        COUNT(*) FILTER (WHERE sobreasignado)           AS sobreasignadas,
                        COUNT(*) FILTER (WHERE ocupacion_pct = 0)       AS sin_asignar,
                        COALESCE(ROUND(AVG(ocupacion_pct), 1), 0)       AS ocupacion_promedio,
                        COALESCE(SUM(capacidad_horas_semana), 0)        AS capacidad_total,
                        COALESCE(SUM(horas_comprometidas), 0)           AS horas_comprometidas
                FROM    carga WHERE activo
            """)
            resumen = cur.fetchone()
    return ok(personas, meta=resumen)
