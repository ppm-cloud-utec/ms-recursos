"""Los 9 endpoints de punta a punta contra PostgreSQL (SNS falso)."""
import uuid
from datetime import date, timedelta

import pytest

from conftest import cuerpo, eventos, peticion
from src.handlers import asignaciones, health, recursos

HOY = date.today()


def _nuevo_recurso(tenant="t-initech", capacidad=40, costo=50):
    r = recursos.crear(peticion(tenant, "admin", body={
        "usuario_id": str(uuid.uuid4()), "nombre": "Prueba Pytest",
        "rol_tecnico": "QA", "costo_hora": costo,
        "capacidad_horas_semana": capacidad,
    }), None)
    assert r["statusCode"] == 201, r["body"]
    return cuerpo(r)["datos"]


def _asignar(rid, pct, inicio=None, fin=None, tenant="t-initech", rol="pm"):
    body = {"recurso_id": rid, "proyecto_id": 7, "codigo_proyecto": "PRY-T",
            "porcentaje_dedicacion": pct,
            "fecha_inicio": (inicio or HOY).isoformat()}
    if fin:
        body["fecha_fin"] = fin.isoformat()
    return asignaciones.crear(peticion(tenant, rol, body=body), None)


# ------------------------------------------------------------------ lecturas
def test_health_devuelve_el_tenant_del_token():
    r = health.manejar(peticion("t-globex"), None)
    assert r["statusCode"] == 200
    assert cuerpo(r)["datos"]["tenant"] == "t-globex"


def test_listar_recursos_serializa_fechas_y_uuid():
    r = recursos.listar(peticion(query={"limite": "5"}), None)
    assert r["statusCode"] == 200, r["body"]
    c = cuerpo(r)
    assert len(c["datos"]) == 5 and c["meta"]["total"] > 0
    uuid.UUID(c["datos"][0]["usuario_id"])          # sale como cadena UUID
    assert c["datos"][0]["ocupacion_pct"] >= c["datos"][-1]["ocupacion_pct"]


def test_filtro_sobreasignados():
    c = cuerpo(recursos.listar(peticion(query={"sobreasignados": "true"}), None))
    assert c["meta"]["total"] > 0
    assert all(p["sobreasignado"] and p["ocupacion_pct"] > 100 for p in c["datos"])


def test_dos_tenants_ven_datos_distintos():
    a = cuerpo(recursos.carga(peticion("t-acme"), None))
    g = cuerpo(recursos.carga(peticion("t-globex"), None))
    roles_a = {p["rol_tecnico"] for p in a["datos"]}
    roles_g = {p["rol_tecnico"] for p in g["datos"]}
    assert "Topografo" in roles_a and "Topografo" not in roles_g
    assert a["meta"]["personas"] > 0 and g["meta"]["personas"] > 0


def test_obtener_recurso_con_asignaciones():
    rid = cuerpo(recursos.listar(peticion(query={"limite": "1"}), None))["datos"][0]["recurso_id"]
    r = recursos.obtener(peticion(path={"id": str(rid)}), None)
    assert r["statusCode"] == 200, r["body"]
    d = cuerpo(r)["datos"]
    assert d["asignaciones"] and "vigente" in d["asignaciones"][0]
    date.fromisoformat(d["asignaciones"][0]["fecha_inicio"])


def test_listar_asignaciones_por_recurso():
    rid = cuerpo(recursos.listar(peticion(query={"limite": "1"}), None))["datos"][0]["recurso_id"]
    r = asignaciones.listar(peticion(query={"recurso_id": str(rid)}), None)
    assert r["statusCode"] == 200, r["body"]
    assert all(a["recurso_id"] == rid for a in cuerpo(r)["datos"])


# ------------------------------------------------------------------- errores
@pytest.mark.parametrize("handler,ev", [
    (recursos.obtener, peticion(path={"id": "abc"})),
    (recursos.listar, peticion(query={"limite": "muchos"})),
    (asignaciones.listar, peticion(query={"proyecto_id": "x"})),
    (asignaciones.liberar, peticion(path={"id": "1;DROP"})),
    (recursos.crear, peticion(rol="admin", body="{no es json")),
    (recursos.crear, peticion(rol="admin", body={"usuario_id": "no-uuid",
                                                 "nombre": "A", "rol_tecnico": "QA"})),
    (recursos.crear, peticion(rol="admin", body={"usuario_id": str(uuid.uuid4()),
                                                 "nombre": "A", "rol_tecnico": "QA",
                                                 "costo_hora": -5})),
    (recursos.actualizar, peticion(rol="admin", path={"id": "1"},
                                   body={"capacidad_horas_semana": 0})),
])
def test_entradas_invalidas_son_400(handler, ev):
    r = handler(ev, None)
    assert r["statusCode"] == 400, r["body"]
    assert cuerpo(r)["ok"] is False


def test_recurso_inexistente_404():
    assert recursos.obtener(peticion(path={"id": "999999999"}), None)["statusCode"] == 404


def test_miembro_no_puede_asignar():
    r = _asignar(1, 10, rol="miembro")
    assert r["statusCode"] == 403


def test_tenant_desconocido_403():
    assert recursos.carga(peticion("t-hackers"), None)["statusCode"] == 403


# -------------------------------------------------------------- escrituras
def test_crear_duplicado_409():
    uid = str(uuid.uuid4())
    body = {"usuario_id": uid, "nombre": "X", "rol_tecnico": "QA"}
    assert recursos.crear(peticion(rol="pmo", body=body), None)["statusCode"] == 201
    assert recursos.crear(peticion(rol="pmo", body=body), None)["statusCode"] == 409


def test_actualizar_recurso():
    rec = _nuevo_recurso()
    r = recursos.actualizar(peticion(rol="pmo", path={"id": str(rec["id"])},
                                     body={"costo_hora": 99.5, "activo": False}), None)
    assert r["statusCode"] == 200, r["body"]
    d = cuerpo(r)["datos"]
    assert d["costo_hora"] == 99.5 and d["activo"] is False


def test_flujo_de_la_demo_70_a_130_dispara_la_alerta(sns):
    rec = _nuevo_recurso(capacidad=40, costo=50)
    assert _asignar(rec["id"], 70)["statusCode"] == 201
    assert [e["tipo"] for e in eventos(sns)] == ["recurso.asignado"]

    sns.publicados.clear()
    r = _asignar(rec["id"], 60, fin=HOY + timedelta(days=27))
    assert r["statusCode"] == 201, r["body"]
    d = cuerpo(r)["datos"]["recurso"]
    assert (d["ocupacion_anterior_pct"], d["ocupacion_pct"], d["exceso_pct"]) == (70, 130, 30)
    assert d["alerta_publicada"] is True

    asignado, sobre = eventos(sns)
    assert asignado["tipo"] == "recurso.asignado"
    assert asignado["datos"]["horas_semana"] == 24.0          # 40h x 60%
    assert asignado["datos"]["semanas"] == 4                  # 28 dias
    assert isinstance(asignado["datos"]["proyecto_id"], int)
    uuid.UUID(asignado["datos"]["usuario_id"])
    assert sobre["tipo"] == "recurso.sobreasignado"
    assert sobre["datos"]["ocupacion_pct"] == 130
    atributos = sns.publicados[1]["MessageAttributes"]
    assert atributos["tipo"]["StringValue"] == "recurso.sobreasignado"
    assert atributos["tenant_id"]["StringValue"] == "t-initech"


def test_no_vuelve_a_alertar_si_ya_estaba_sobreasignado(sns):
    rec = _nuevo_recurso()
    _asignar(rec["id"], 100)
    _asignar(rec["id"], 30)                     # cruza: 100 -> 130
    sns.publicados.clear()
    _asignar(rec["id"], 20)                     # 130 -> 150: sin alerta nueva
    assert [e["tipo"] for e in eventos(sns)] == ["recurso.asignado"]


def test_asignacion_futura_tambien_alerta(sns):
    """Antes solo se media HOY: una asignacion que empieza la proxima semana
    nunca disparaba la alerta."""
    rec = _nuevo_recurso()
    _asignar(rec["id"], 80)
    sns.publicados.clear()
    r = _asignar(rec["id"], 50, inicio=HOY + timedelta(days=7))
    d = cuerpo(r)["datos"]["recurso"]
    assert d["ocupacion_pct"] == 130 and d["alerta_publicada"]
    assert "recurso.sobreasignado" in [e["tipo"] for e in eventos(sns)]


def test_validaciones_de_asignacion():
    rec = _nuevo_recurso()
    assert _asignar(rec["id"], 0)["statusCode"] == 400
    assert _asignar(rec["id"], 101)["statusCode"] == 400
    assert _asignar(rec["id"], 50, inicio=HOY,
                    fin=HOY - timedelta(days=1))["statusCode"] == 400
    r = asignaciones.crear(peticion(body={
        "recurso_id": rec["id"], "proyecto_id": 1, "porcentaje_dedicacion": 10,
        "fecha_inicio": "09/11/2026"}), None)
    assert r["statusCode"] == 400
    assert _asignar(999999999, 10)["statusCode"] == 404


def test_liberar_devuelve_la_capacidad():
    rec = _nuevo_recurso()
    aid = cuerpo(_asignar(rec["id"], 60))["datos"]["asignacion"]["id"]
    r = asignaciones.liberar(peticion(path={"id": str(aid)}), None)
    assert r["statusCode"] == 200, r["body"]
    assert cuerpo(r)["datos"]["ocupacion_pct"] == 0
    assert asignaciones.liberar(peticion(path={"id": str(aid)}), None)["statusCode"] == 404
