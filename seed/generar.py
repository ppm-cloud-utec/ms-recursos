#!/usr/bin/env python3
"""Carga 30,000 registros en ms-recursos, repartidos en 3 tenants.

    python seed/generar.py --esquema
    python seed/generar.py
    python seed/generar.py --contar

Reparto por tenant (10,000):
    recursos         800
    asignaciones   9,200
                  ------
                  10,000

Se deja a proposito un 15% de personas SOBREASIGNADAS (por encima del 100%),
porque la vista de Workload y la alerta de sobreasignacion son funciones del
producto que hay que poder mostrar en la demo con datos reales.
"""
import os
import sys
import uuid
import random
import argparse
from datetime import date, timedelta

import psycopg

SCHEMAS = {"t-acme": "t_acme", "t-globex": "t_globex", "t-initech": "t_initech"}

PERFILES = {
    "t-acme": dict(
        nombre="Acme Ingenieria", proyectos=500,
        roles=["Maestro de obra", "Ingeniero residente", "Topografo",
               "Operario", "Supervisor SSOMA", "Capataz", "Almacenero",
               "Ingeniero de costos"],
        costo=(18, 95), capacidad=[40, 48],
    ),
    "t-globex": dict(
        nombre="Globex Consulting", proyectos=250,
        roles=["Consultor senior", "Consultor junior", "Analista funcional",
               "Gerente de proyecto", "Especialista en procesos",
               "Arquitecto de soluciones"],
        costo=(35, 180), capacidad=[40],
    ),
    "t-initech": dict(
        nombre="Initech Software", proyectos=25,
        roles=["Desarrollador backend", "Desarrollador frontend", "QA",
               "DevOps", "Diseñador UX", "Tech lead", "Data engineer"],
        costo=(30, 140), capacidad=[40, 30],
    ),
}

NOMBRES = ["Ana", "Luis", "Carmen", "Jose", "Rosa", "Carlos", "Maria", "Jorge",
           "Elena", "Miguel", "Lucia", "Pedro", "Sofia", "Diego", "Valeria",
           "Andres", "Paola", "Ricardo", "Gabriela", "Fernando", "Claudia",
           "Javier", "Patricia", "Raul", "Monica", "Cesar", "Silvia", "Oscar"]
APELLIDOS = ["Quispe", "Flores", "Rojas", "Vargas", "Castillo", "Ramos",
             "Torres", "Diaz", "Mendoza", "Chavez", "Huaman", "Salazar",
             "Paredes", "Guerrero", "Ibanez", "Zarate", "Palomino", "Cordova"]

N_RECURSOS, N_ASIGNACIONES = 800, 9_200
PCT_SOBREASIGNADOS = 0.15


def dsn():
    return (f"host={os.environ['DB_HOST']} port=5432 "
            f"dbname={os.environ.get('DB_NAME', 'ppm_recursos')} "
            f"user={os.environ['DB_USER']} password={os.environ['DB_PASSWORD']}")


def aplicar_esquema():
    sql = open(os.path.join(os.path.dirname(__file__), "esquema.sql")).read()
    with psycopg.connect(dsn()) as con:
        for schema in SCHEMAS.values():
            with con.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
                cur.execute(f"SET search_path TO {schema}")
                cur.execute(sql)
            con.commit()
            print(f"  esquema aplicado en {schema}")


def reparto_dedicacion(objetivo):
    """Parte un porcentaje objetivo en asignaciones de 10 a 100.

    Un objetivo de 130 puede salir como 100+30 o como 60+50+20: asi la vista
    de carga tiene personas con 1, 2 y 3 proyectos a la vez.
    """
    partes = []
    restante = objetivo
    while restante > 0:
        trozo = min(restante, random.choice([10, 20, 25, 30, 40, 50, 60, 80, 100]))
        partes.append(trozo)
        restante -= trozo
    return partes


def generar(tenant_id, perfil):
    schema = SCHEMAS[tenant_id]
    hoy = date.today()
    total = 0

    with psycopg.connect(dsn()) as con:
        with con.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")

            # Re-ejecutable: sin esto, la segunda corrida choca con el UNIQUE
            # de usuario_id (la semilla es fija) o duplica las asignaciones.
            cur.execute("TRUNCATE asignaciones, recursos RESTART IDENTITY")

            # --- recursos ---
            lote = []
            for _ in range(N_RECURSOS):
                lote.append((
                    str(uuid.UUID(int=random.getrandbits(128), version=4)),
                    f"{random.choice(NOMBRES)} {random.choice(APELLIDOS)}",
                    random.choice(perfil["roles"]),
                    random.randint(*perfil["costo"]),
                    random.choice(perfil["capacidad"]),
                    random.random() > 0.04,          # 4% inactivos
                ))
            cur.executemany("""
                INSERT INTO recursos
                    (usuario_id, nombre, rol_tecnico, costo_hora,
                     capacidad_horas_semana, activo)
                VALUES (%s,%s,%s,%s,%s,%s)
            """, lote)
            total += len(lote)
            print(f"    {len(lote)} recursos")

            cur.execute("SELECT id FROM recursos WHERE activo ORDER BY id")
            ids = [f[0] for f in cur.fetchall()]

            # --- asignaciones ---
            # Primero se reparte una ocupacion objetivo por persona, de modo
            # que un 15% quede por encima del 100%.
            objetivos = {}
            for rid in ids:
                if random.random() < PCT_SOBREASIGNADOS:
                    objetivos[rid] = random.choice([110, 120, 130, 150, 180])
                else:
                    objetivos[rid] = random.choice([0, 20, 40, 50, 60, 70, 80, 100])

            lote = []
            for rid, objetivo in objetivos.items():
                for dedicacion in reparto_dedicacion(objetivo):
                    pid = random.randint(1, perfil["proyectos"])
                    # VIGENTE hoy: empezo hace 10-300 dias y termina dentro de
                    # 30-300 dias. Si la fecha_fin pudiera caer en el pasado,
                    # la vista `carga` no la contaria y el % de sobreasignados
                    # quedaria muy por debajo del 15% prometido.
                    inicio = hoy - timedelta(days=random.randint(10, 300))
                    fin = hoy + timedelta(days=random.randint(30, 300))
                    lote.append((
                        rid, pid, f"PRY-{tenant_id[2:5].upper()}-{pid:05d}",
                        dedicacion, inicio, fin, True,
                    ))

            # El resto son asignaciones HISTORICAS (inactivas o ya terminadas),
            # que aportan volumen sin alterar la ocupacion de hoy.
            while len(lote) < N_ASIGNACIONES:
                rid = random.choice(ids)
                pid = random.randint(1, perfil["proyectos"])
                inicio = hoy - timedelta(days=random.randint(400, 1000))
                lote.append((
                    rid, pid, f"PRY-{tenant_id[2:5].upper()}-{pid:05d}",
                    random.choice([20, 40, 50, 80, 100]), inicio,
                    inicio + timedelta(days=random.randint(60, 250)), False,
                ))
            lote = lote[:N_ASIGNACIONES]

            cur.executemany("""
                INSERT INTO asignaciones
                    (recurso_id, proyecto_id, codigo_proyecto,
                     porcentaje_dedicacion, fecha_inicio, fecha_fin, activa)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, lote)
            total += len(lote)
            print(f"    {len(lote)} asignaciones")

            cur.execute("SELECT COUNT(*) FROM carga WHERE sobreasignado")
            print(f"    {cur.fetchone()[0]} personas sobreasignadas (>100%)")
        con.commit()
    return total


def contar():
    gran_total = 0
    for schema in SCHEMAS.values():
        with psycopg.connect(dsn()) as con, con.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            print(f"  {schema}")
            sub = 0
            for t in ("recursos", "asignaciones"):
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                n = cur.fetchone()[0]
                print(f"    {t:<22} {n:>7}")
                sub += n
            cur.execute("SELECT COUNT(*) FROM carga WHERE sobreasignado")
            print(f"    {'(sobreasignados)':<22} {cur.fetchone()[0]:>7}")
            cur.execute("SELECT ROUND(AVG(ocupacion_pct),1) FROM carga WHERE activo")
            print(f"    {'(ocupacion promedio)':<22} {cur.fetchone()[0]:>6}%")
            print(f"    {'subtotal':<22} {sub:>7}\n")
            gran_total += sub
    print(f"  TOTAL {gran_total:>26}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--esquema", action="store_true")
    ap.add_argument("--contar", action="store_true")
    ap.add_argument("--tenant", choices=list(SCHEMAS))
    args = ap.parse_args()

    for v in ("DB_HOST", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(v):
            sys.exit(f"Falta la variable de entorno {v}")

    if args.esquema:
        return aplicar_esquema()
    if args.contar:
        return contar()

    random.seed(2026)
    objetivo = {args.tenant: PERFILES[args.tenant]} if args.tenant else PERFILES
    total = 0
    for tenant_id, perfil in objetivo.items():
        print(f"== {perfil['nombre']} ({tenant_id}) ==")
        n = generar(tenant_id, perfil)
        print(f"   {n} registros\n")
        total += n
    print(f"Listo: {total} registros. Verifica con --contar")


if __name__ == "__main__":
    main()
