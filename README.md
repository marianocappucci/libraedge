# LibraEdge

Infraestructura edge opcional y premium de la familia Libra.

LibraEdge permite instalar una mini PC local —inicialmente Ubuntu Server LTS— para operar durante cortes de internet y sincronizar luego con el servidor central. Incluye persistencia de identidad de nodo/outbox (`libraedge.db`), worker de outbox, receptor idempotente, transporte HTTP y adaptador FastAPI.

El dominio de negocio pertenece a cada motor o producto: LibraCommerce (u otro) traduce sus propias operaciones confirmadas al `OutboxOperation` genérico y provee el `operation_handler` que sabe materializarlas; LibraEdge administra identidad de nodo, secuencias locales, persistencia del outbox, transporte, reintentos y recepción, sin conocer qué es una venta, un turno o cualquier otro dominio.

## Estado

Contratos propios desacoplados de LibraCommerce (`e0b69e0`) y verificados sin ninguna referencia cruzada (`tests/test_imports.py`). Persistencia SQLite propia (`libraedge.db.schema`/`libraedge.db.repository.SqliteNodeRepository`) para identidad de nodo y outbox — LibraCommerce ya no mantiene copia propia de estas tablas. Consumido como dependencia Git versionada por LibraCommerce (extra `offline`/`offline-server`), instalación en entorno limpio verificada.

**Autenticación por nodo**: `POST /sync/v1/push` exige `Authorization: Bearer <secret>` — antes de esto, cualquiera que alcanzara el endpoint podía inyectar operaciones que el `operation_handler` central materializaría como datos de dominio reales (ej. una venta confirmada). `SqliteNodeRepository.register_node()` da de alta un nodo y devuelve su secreto una sola vez (solo se persiste el hash); `deactivate_node()` revoca uno (mini PC robada/dada de baja). `create_sync_app(receiver, node_repository)` ahora requiere el repositorio como segundo argumento — sin consumidores externos todavía, sin impacto real.
