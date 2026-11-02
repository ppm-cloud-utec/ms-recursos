"""Personas, capacidad y asignaciones.

Los "recursos" son los empleados de la organizacion vistos como capacidad de
trabajo asignable: cuantas horas tienen por semana y a que proyectos estan
dedicados. No es un directorio de perfiles.
"""
from psycopg.rows import dict_row

from src import db, roles
from src.eventos import publicar
from src.respuesta import cuerpo_de, error, ok, tenant_de, usuario_de


def listar(event, context):
    """GET /recursos?rol=&sobreasignados=true&disponibles=true"""
    q = event.get("queryStringParameters") or {}
    limite = min(int(q.get("limite", 100)), 300)
    pagina = max(int(q.get("pagina", 1)), 1)

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
                ORDER BY ocupacion_pct DESC, nombre
                LIMIT %s OFFSET %s
            """, valores + [limite, (pagina - 1) * limite])
            filas = cur.fetchall()

    return ok(filas, meta={"total": total, "pagina": pagina})


def obtener(event, context):
    """GET /recursos/{id} — con sus asignaciones vigentes."""
    rid = event["pathParameters"]["id"]
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM carga WHERE recurso_id = %s", (rid,))
            recurso = cur.fetchone()
            if not recurso:
                return error("NO_ENCONTRADO", f"No existe el recurso {rid}", 404)
            cur.execute("""
                SELECT id, proyecto_id, codigo_proyecto, porcentaje_dedicacion,
                       fecha_inicio, fecha_fin, activa
                FROM   asignaciones WHERE recurso_id = %s
                ORDER BY activa DESC, fecha_inicio DESC
            """, (rid,))
            recurso["asignaciones"] = cur.fetchall()
    return ok(recurso)


def crear(event, context):
    """POST /recursos — solo admin y pmo registran capacidad."""
    negado = roles.exige(event, roles.ADMIN, roles.PMO)
    if negado:
        return negado

    c = cuerpo_de(event)
    faltan = [k for k in ("usuario_id", "nombre", "rol_tecnico") if not c.get(k)]
    if faltan:
        return error("DATOS_INCOMPLETOS", f"Falta: {', '.join(faltan)}")

    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                INSERT INTO recursos
                    (usuario_id, nombre, rol_tecnico, costo_hora,
                     capacidad_horas_semana)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (usuario_id) DO NOTHING
                RETURNING *
            """, (c["usuario_id"], c["nombre"].strip(), c["rol_tecnico"].strip(),
                  c.get("costo_hora", 0), c.get("capacidad_horas_semana", 40)))
            recurso = cur.fetchone()

    if not recurso:
        return error("RECURSO_DUPLICADO",
                     f"El usuario {c['usuario_id']} ya esta registrado", 409)
    return ok(recurso, estado=201)


def actualizar(event, context):
    """PUT /recursos/{id}"""
    negado = roles.exige(event, roles.ADMIN, roles.PMO)
    if negado:
        return negado

    rid = event["pathParameters"]["id"]
    c = cuerpo_de(event)
    permitidos = ("nombre", "rol_tecnico", "costo_hora",
                  "capacidad_horas_semana", "activo")
    cambios = {k: c[k] for k in permitidos if k in c}
    if not cambios:
        return error("SIN_CAMBIOS", f"Envia alguno de: {', '.join(permitidos)}")

    sets = ", ".join(f"{k} = %s" for k in cambios)
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute(f"UPDATE recursos SET {sets} WHERE id = %s RETURNING *",
                        list(cambios.values()) + [rid])
            recurso = cur.fetchone()

    if not recurso:
        return error("NO_ENCONTRADO", f"No existe el recurso {rid}", 404)
    return ok(recurso)


def carga(event, context):
    """GET /carga — la vista de Workload, con el total del equipo."""
    with db.conexion(tenant_de(event)) as con:
        with con.cursor(row_factory=dict_row) as cur:
            cur.execute("""
                SELECT recurso_id, usuario_id, nombre, rol_tecnico,
                       capacidad_horas_semana, ocupacion_pct, disponible_pct,
                       horas_comprometidas, sobreasignado, n_asignaciones
                FROM   carga WHERE activo
                ORDER BY ocupacion_pct DESC, nombre
            """)
            personas = cur.fetchall()
            cur.execute("""
                SELECT  COUNT(*)                                       AS personas,
                        COUNT(*) FILTER (WHERE sobreasignado)           AS sobreasignadas,
                        COUNT(*) FILTER (WHERE ocupacion_pct = 0)       AS sin_asignar,
                        ROUND(AVG(ocupacion_pct), 1)                    AS ocupacion_promedio,
                        COALESCE(SUM(capacidad_horas_semana), 0)        AS capacidad_total,
                        COALESCE(SUM(horas_comprometidas), 0)           AS horas_comprometidas
                FROM    carga WHERE activo
            """)
            resumen = cur.fetchone()
    return ok(personas, meta=resumen)
