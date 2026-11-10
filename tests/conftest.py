"""Pruebas locales de ms-recursos contra un PostgreSQL real.

No necesitan AWS: SNS se reemplaza por un falso que guarda lo publicado.

    # 1. Un Postgres local (Docker es lo mas rapido)
    docker run -d --name pg-recursos -p 5432:5432 -e POSTGRES_PASSWORD=x postgres:16
    docker exec pg-recursos psql -U postgres -c "CREATE DATABASE ppm_recursos"

    # 2. Esquema y datos
    export DB_HOST=localhost DB_USER=postgres DB_PASSWORD=x DB_NAME=ppm_recursos
    python seed/generar.py --esquema && python seed/generar.py

    # 3. Pruebas
    pip install pytest 'psycopg[binary]'
    pytest -q
"""
import json
import os
import sys
import types
import uuid

import pytest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("SERVICIO", "ms-recursos")
os.environ.setdefault("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:000000000000:falso")
os.environ.setdefault("TABLA_EVENTOS_PROCESADOS", "falsa")
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "ppm_recursos")
os.environ.setdefault("DB_USER", "postgres")
os.environ.setdefault("DB_PASSWORD", "x")


class _SnsFalso:
    def __init__(self):
        self.publicados = []

    def publish(self, **kw):
        self.publicados.append(kw)
        return {"MessageId": str(uuid.uuid4())}


SNS = _SnsFalso()

# boto3 falso: la Lambda lo trae en el runtime, en local puede no estar
_boto3 = types.ModuleType("boto3")
_boto3.client = lambda *a, **k: SNS
_boto3.resource = lambda *a, **k: types.SimpleNamespace(Table=lambda *_: None)
sys.modules.setdefault("boto3", _boto3)
import src.eventos  # noqa: E402

src.eventos._sns = SNS


@pytest.fixture
def sns():
    SNS.publicados.clear()
    return SNS


def eventos(sns):
    """Los eventos publicados, como dicts, en orden."""
    return [json.loads(p["Message"]) for p in sns.publicados]


def peticion(tenant="t-initech", rol="pm", body=None, path=None, query=None,
             usuario=None):
    """Arma el event que API Gateway le pasa a la Lambda tras el authorizer."""
    return {
        "requestContext": {"authorizer": {
            "tenant_id": tenant,
            "usuario_id": usuario or str(uuid.uuid4()),
            "rol": rol,
            "email": "prueba@ppm.pe",
        }},
        "pathParameters": path,
        "queryStringParameters": query,
        "body": body if isinstance(body, str) or body is None else json.dumps(body),
    }


def cuerpo(resp):
    return json.loads(resp["body"])
