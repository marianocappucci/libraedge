# LibraEdge

Infraestructura edge opcional y premium de la familia Libra.

LibraEdge permite instalar una mini PC local —inicialmente Ubuntu Server LTS— para operar durante cortes de internet y sincronizar luego con el servidor central. Incluye worker de outbox, receptor idempotente, transporte HTTP y adaptador FastAPI.

El dominio de negocio pertenece a cada motor o producto: LibraCommerce publica operaciones comerciales; LibraEdge administra persistencia local, transporte, reintentos y recepción.

## Estado

Extracción inicial desde LibraCommerce. La API de contratos todavía debe desacoplarse completamente de los tipos de dominio de LibraCommerce y versionarse.
