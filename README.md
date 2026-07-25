# LibraEdge

Infraestructura edge opcional y premium de la familia Libra.

LibraEdge permite instalar una mini PC local —inicialmente Ubuntu Server LTS— para operar durante cortes de internet y sincronizar luego con el servidor central. Incluye persistencia de identidad de nodo/outbox (`libraedge.db`), worker de outbox, receptor idempotente, transporte HTTP y adaptador FastAPI.

El dominio de negocio pertenece a cada motor o producto: LibraCommerce (u otro) traduce sus propias operaciones confirmadas al `OutboxOperation` genérico y provee el `operation_handler` que sabe materializarlas; LibraEdge administra identidad de nodo, secuencias locales, persistencia del outbox, transporte, reintentos y recepción, sin conocer qué es una venta, un turno o cualquier otro dominio.

## Estado

Contratos propios desacoplados de LibraCommerce (`e0b69e0`) y verificados sin ninguna referencia cruzada (`tests/test_imports.py`). Persistencia SQLite propia (`libraedge.db.schema`/`libraedge.db.repository.SqliteNodeRepository`) para identidad de nodo y outbox — LibraCommerce ya no mantiene copia propia de estas tablas. Consumido como dependencia Git versionada por LibraCommerce (extra `offline`/`offline-server`), instalación en entorno limpio verificada.
