# ms-recursos

Personas, capacidad semanal y asignaciones a proyectos. Detecta sobreasignacion.

Parte del proyecto final de Cloud Computing (CS2032), Grupo 4. Todas las rutas cuelgan de la API compartida `ppm-api-dev` bajo `/recursos` y, salvo el login, piden el token JWT.

Base de datos: RDS PostgreSQL, base ppm_recursos, un schema por organizacion.

## Endpoints

- GET /recursos/health
- GET /recursos/recursos
- GET /recursos/recursos/{id}
- POST /recursos/recursos
- PUT /recursos/recursos/{id}
- GET /recursos/carga
- GET /recursos/asignaciones
- POST /recursos/asignaciones
- DELETE /recursos/asignaciones/{id}

## Eventos

Publica recurso.asignado y recurso.sobreasignado.

## Despliegue

Requiere que `infra-compartida` ya este desplegado.

```bash
npm install
npx serverless deploy
```

Los servicios con RDS necesitan Docker para compilar psycopg.

## Datos de prueba

```bash
python seed/generar.py
python seed/generar.py --contar
```
