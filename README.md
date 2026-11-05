# ms-recursos

Personas, capacidad y asignaciones a proyectos. Responde *¿quién está
sobrecargado y quién está libre?* y **avisa cuando alguien pasa del 100%**.

- **Responsable:** @sebastianperez72
- **Base de datos:** RDS PostgreSQL · database `ppm_recursos` · un schema por tenant
- **Runtime:** Python 3.12 · Serverless Framework · us-east-1

## Endpoints (9, todos protegidos)

| Método | Ruta | Rol | Qué hace |
|---|---|---|---|
| GET | `/recursos` | todos | personas con su ocupación · `?rol=`, `?sobreasignados=true`, `?disponibles=true` |
| GET | `/recursos/{id}` | todos | detalle con sus asignaciones |
| POST | `/recursos` | admin, pmo | registra capacidad y costo-hora |
| PUT | `/recursos/{id}` | admin, pmo | actualiza |
| GET | `/carga` | todos | **la vista de Workload** con el resumen del equipo |
| GET | `/asignaciones` | todos | `?proyecto_id=`, `?recurso_id=` |
| POST | `/asignaciones` | pm, pmo, admin | asigna · **publica eventos** |
| DELETE | `/asignaciones/{id}` | pm, pmo, admin | libera capacidad (baja lógica) |
| GET | `/health` | todos | verifica el servicio |

## El tercer flujo de eventos de la demo

```
   POST /asignaciones  { recurso_id: 42, dedicacion: 60 }
                    |
       FOR UPDATE + ocupacion antes (70%) / despues (130%)
                    |
     publica recurso.asignado   +   recurso.sobreasignado
              |                              |
      cola-presupuestos                cola-alertas
              |                              |
   proyecta costo de horas-hombre      avisa al PM
```

En la demo: asignas a alguien que ya estaba al 70% y **salta la alerta**. La
respuesta del endpoint ya trae `ocupacion_anterior_pct`, `ocupacion_pct` y
`exceso_pct` para mostrarlo en pantalla.

## Dos cosas que vale la pena entender

**1. La sobreasignación no se bloquea.** A veces es una decisión consciente del
PM. El sistema la registra y avisa — eso es lo que pide el producto.

**2. La vista `carga` solo cuenta asignaciones vigentes hoy.** Una que ya
terminó no ocupa capacidad. Sin ese filtro todo el equipo aparecería
sobreasignado por su historial y la vista sería inútil.

Este microservicio **solo publica eventos, no consume**: no tiene worker ni cola.

## Desplegar

```bash
npm install            # instala Serverless v3 y el plugin de Python
npx serverless deploy
```

Necesita **Docker corriendo** si este microservicio usa RDS: el plugin compila
`psycopg` contra Amazon Linux dentro de un contenedor.

## Cargar datos

```bash
export DB_HOST=<endpoint del RDS> DB_USER=ppm_recursos DB_PASSWORD=<de SSM>
export DB_NAME=ppm_recursos
python seed/generar.py --esquema
python seed/generar.py            # 30,000 registros, 15% sobreasignados
python seed/generar.py --contar
```

## Documentación

- [Modelo de datos](docs/modelo.md) — ER, la vista `carga`, detección de sobreasignación
- [Arquitectura general](https://github.com/ppm-cloud-utec/infra-compartida/blob/main/docs/arquitectura.md)
