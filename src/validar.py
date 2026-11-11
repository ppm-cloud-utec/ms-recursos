"""Validacion de entradas y traduccion de errores a respuestas HTTP.

Sin esto, un `/recursos/abc` o una fecha mal escrita llegan a PostgreSQL,
revientan dentro de la Lambda y API Gateway devuelve un 502 sin cuerpo: el
frontend no sabe que paso. Aqui se convierten en 400/404/409 con la forma de
error del contrato comun.
"""
import functools
import json
import logging
import math
import traceback
import uuid
from datetime import date

import psycopg.errors

from src import db
from src.respuesta import error

log = logging.getLogger()
log.setLevel(logging.INFO)


class EntradaInvalida(Exception):
    """Error de validacion: termina en un 400 con este codigo y mensaje."""

    def __init__(self, codigo, mensaje):
        super().__init__(mensaje)
        self.codigo = codigo
        self.mensaje = mensaje


def cuerpo(event):
    """El body como dict. Un JSON roto o que no sea objeto es un 400."""
    try:
        c = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        raise EntradaInvalida("JSON_INVALIDO", "El cuerpo no es un JSON valido")
    if not isinstance(c, dict):
        raise EntradaInvalida("JSON_INVALIDO", "El cuerpo debe ser un objeto JSON")
    return c


def entero(valor, campo, minimo=1):
    if isinstance(valor, bool):
        raise EntradaInvalida("ID_INVALIDO", f"{campo} debe ser un entero")
    try:
        n = int(str(valor).strip())
    except (TypeError, ValueError):
        raise EntradaInvalida("ID_INVALIDO", f"{campo} debe ser un entero")
    if n < minimo:
        raise EntradaInvalida("ID_INVALIDO", f"{campo} debe ser >= {minimo}")
    return n


def numero(valor, campo, minimo=None, maximo=None, incluye_minimo=True):
    if isinstance(valor, bool):
        raise EntradaInvalida("NUMERO_INVALIDO", f"{campo} debe ser numerico")
    try:
        x = float(valor)
    except (TypeError, ValueError):
        raise EntradaInvalida("NUMERO_INVALIDO", f"{campo} debe ser numerico")
    if math.isnan(x) or math.isinf(x):
        raise EntradaInvalida("NUMERO_INVALIDO", f"{campo} debe ser numerico")
    if minimo is not None and (x < minimo or (x == minimo and not incluye_minimo)):
        signo = ">=" if incluye_minimo else ">"
        raise EntradaInvalida("NUMERO_INVALIDO", f"{campo} debe ser {signo} {minimo}")
    if maximo is not None and x > maximo:
        raise EntradaInvalida("NUMERO_INVALIDO", f"{campo} debe ser <= {maximo}")
    return x


def fecha(valor, campo):
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor).strip())
    except (TypeError, ValueError):
        raise EntradaInvalida("FECHA_INVALIDA", f"{campo} debe tener formato AAAA-MM-DD")


def uuid_(valor, campo):
    try:
        return str(uuid.UUID(str(valor).strip()))
    except (TypeError, ValueError, AttributeError):
        raise EntradaInvalida("UUID_INVALIDO", f"{campo} debe ser un UUID")


def texto(valor, campo):
    if not isinstance(valor, str) or not valor.strip():
        raise EntradaInvalida("DATOS_INCOMPLETOS", f"{campo} no puede estar vacio")
    return valor.strip()


def booleano(valor, campo):
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str) and valor.lower() in ("true", "false"):
        return valor.lower() == "true"
    raise EntradaInvalida("BOOLEANO_INVALIDO", f"{campo} debe ser true o false")


def protegido(handler):
    """Envuelve un handler HTTP: validacion -> 400, BD -> 400/409, resto -> 500.

    El 500 sigue la forma comun {"ok": false, "error": {...}} y el detalle
    queda en CloudWatch, no en la respuesta.
    """

    @functools.wraps(handler)
    def envoltura(event, context):
        try:
            return handler(event, context)
        except EntradaInvalida as e:
            return error(e.codigo, e.mensaje, 400)
        except db.TenantDesconocido as e:
            return error("TENANT_DESCONOCIDO", str(e), 403)
        except psycopg.errors.UniqueViolation:
            return error("DUPLICADO", "Ya existe un registro con esos datos", 409)
        except psycopg.errors.CheckViolation as e:
            return error("RESTRICCION", _detalle(e), 400)
        except (psycopg.errors.DataError, psycopg.errors.NotNullViolation) as e:
            return error("DATOS_INVALIDOS", _detalle(e), 400)
        except Exception:
            log.error("Error no controlado en %s\n%s",
                      handler.__name__, traceback.format_exc())
            return error("ERROR_INTERNO", "Error interno del servicio", 500)

    return envoltura


def _detalle(e):
    diag = getattr(e, "diag", None)
    return (getattr(diag, "message_primary", None) or str(e)).strip()
