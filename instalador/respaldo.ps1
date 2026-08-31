# Respaldo de la base del nodo, con verificación.
#
# 🔴 POR QUÉ EXISTE. El `sync_outbox` del nodo es **el único lugar del mundo**
# donde vive una venta cobrada sin internet: todavía no llegó al central, y en
# el central no está. Si ese disco muere entre el cobro y la sincronización, esa
# plata no está en ningún lado. Hasta el 2026-08-31 el instalador no contemplaba
# ningún respaldo — cero menciones en toda la carpeta.
#
# 🔴 QUÉ NO RESUELVE, y hay que decirlo. Un respaldo en el MISMO disco protege
# de que la base se corrompa, de un DROP por accidente y de una actualización
# que salga mal. **No protege de que el disco se rompa.** Para eso hay que
# pasarle `-DestinoSecundario` apuntando a un pendrive, una carpeta de red o lo
# que haya en el local. El instalador no lo puede adivinar.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizPostgres,
    [Parameter(Mandatory = $true)][string]$Password,
    [string]$Base = "restolibra",
    [int]$Puerto = 55432,
    [string]$Destino = (Join-Path $env:ProgramData "LibraEdge\respaldos"),
    # Segunda copia, en otro dispositivo. Vacío = no se hace, y se avisa.
    [string]$DestinoSecundario = "",
    # Cuántos se conservan. A un respaldo por hora, 48 son dos días.
    [int]$Conservar = 48
)

$ErrorActionPreference = "Stop"

$pgdump    = Join-Path $RaizPostgres "bin\pg_dump.exe"
$pgrestore = Join-Path $RaizPostgres "bin\pg_restore.exe"
$psql      = Join-Path $RaizPostgres "bin\psql.exe"
foreach ($exe in @($pgdump, $pgrestore, $psql)) {
    if (-not (Test-Path $exe)) { throw "No está $exe." }
}

New-Item -ItemType Directory -Force -Path $Destino | Out-Null
$env:PGPASSWORD = $Password
$sello   = Get-Date -Format "yyyyMMdd-HHmmss"
$archivo = Join-Path $Destino "nodo-$sello.dump"

# El `$ErrorActionPreference` baja para todo lo que hable con los binarios de
# PostgreSQL: con `Stop`, lo que un programa nativo escribe en stderr se
# convierte en error terminante, y `pg_dump` escribe avisos ahí. Es la misma
# trampa que ya se comió el bucle de espera de `preparar_postgres.ps1`.
$preferenciaPrevia = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

function Consultar($sql, $base = $Base) {
    (& $psql -h 127.0.0.1 -p $Puerto -U postgres -d $base -tAc $sql 2>&1 | Out-String).Trim()
}

try {
    # --- Lo que hay ANTES, para poder comparar contra el respaldo -----------
    # Se miden las dos tablas que importan: la que tiene lo que el central
    # todavía no sabe, y la de las ventas.
    $pendientesVivo = Consultar "SELECT count(1) FROM sync_outbox WHERE status <> 'acknowledged'"
    $ventasVivo     = Consultar "SELECT count(1) FROM sales"
    Write-Host "En la base: $pendientesVivo operaciones sin confirmar, $ventasVivo ventas."

    # --- El respaldo --------------------------------------------------------
    & $pgdump -h 127.0.0.1 -p $Puerto -U postgres -d $Base -Fc -f $archivo
    if ($LASTEXITCODE -ne 0) { throw "pg_dump falló con código $LASTEXITCODE" }
    $tam = (Get-Item $archivo).Length
    Write-Host ("Escrito: {0} ({1:N1} MB)" -f (Split-Path $archivo -Leaf), ($tam / 1MB))
    if ($tam -lt 1024) { throw "El respaldo pesa $tam bytes: no puede estar bien." }

    # --- 🔴 LA VERIFICACIÓN: se ABRE el respaldo, no se confía en el [OK] ----
    #
    # `pg_dump` puede salir con 0 y dejar un archivo que no restaura. Y mirar
    # que el archivo exista o que pese algo no distingue un respaldo bueno de
    # uno truncado. Lo único que lo distingue es RESTAURARLO y contar.
    #
    # Se restaura en una base descartable de la misma instancia --el nodo no
    # tiene otra a mano-- y se comparan los conteos contra los de arriba.
    $verificacion = "libraedge_verificar_respaldo"
    Consultar "DROP DATABASE IF EXISTS $verificacion" "postgres" | Out-Null
    Consultar "CREATE DATABASE $verificacion" "postgres" | Out-Null
    try {
        & $pgrestore -h 127.0.0.1 -p $Puerto -U postgres -d $verificacion --no-owner --no-privileges $archivo 2>&1 |
            Out-Null
        # pg_restore avisa por cosas inofensivas (owners que no existen), así
        # que su código de salida no alcanza: lo que decide son los conteos.
        $pendientesCopia = Consultar "SELECT count(1) FROM sync_outbox WHERE status <> 'acknowledged'" $verificacion
        $ventasCopia     = Consultar "SELECT count(1) FROM sales" $verificacion
        Write-Host "En el respaldo: $pendientesCopia sin confirmar, $ventasCopia ventas."

        if ($pendientesCopia -ne $pendientesVivo -or $ventasCopia -ne $ventasVivo) {
            throw ("El respaldo NO coincide con la base: sin confirmar " +
                   "$pendientesCopia contra $pendientesVivo, ventas " +
                   "$ventasCopia contra $ventasVivo. Se conserva el archivo " +
                   "para mirarlo, pero no cuenta como respaldo.")
        }
        Write-Host "Verificado: el respaldo restaura y los conteos coinciden."
    } finally {
        Consultar "DROP DATABASE IF EXISTS $verificacion" "postgres" | Out-Null
    }

    # --- La segunda copia ---------------------------------------------------
    if ($DestinoSecundario) {
        if (Test-Path $DestinoSecundario) {
            Copy-Item $archivo -Destination $DestinoSecundario -Force
            $copia = Join-Path $DestinoSecundario (Split-Path $archivo -Leaf)
            # Se compara el hash: una copia a un pendrive que se desconectó a
            # mitad deja un archivo del tamaño correcto y el contenido cortado.
            $a = (Get-FileHash $archivo -Algorithm SHA256).Hash
            $b = (Get-FileHash $copia   -Algorithm SHA256).Hash
            if ($a -ne $b) { throw "La copia en $DestinoSecundario no coincide con el original." }
            Write-Host "Segunda copia en $DestinoSecundario, con el mismo SHA256."
        } else {
            # ${...} y no $...: PowerShell lee `$Variable:` como una variable con
            # ámbito --la forma de $env:RUTA-- y el parser lo rechaza.
            Write-Warning ("No existe ${DestinoSecundario}: la segunda copia NO se hizo. " +
                           "Si es un pendrive, está desconectado.")
        }
    } else {
        Write-Warning ("Sin -DestinoSecundario: el respaldo queda en el MISMO disco " +
                       "que la base. Protege de una base corrupta, no de un disco roto.")
    }

    # --- Rotación -----------------------------------------------------------
    # Se borran DESPUÉS de verificar el nuevo: si el de hoy no sirviera, no se
    # tiraron los que sí.
    $viejos = Get-ChildItem $Destino -Filter "nodo-*.dump" |
              Sort-Object LastWriteTime -Descending | Select-Object -Skip $Conservar
    foreach ($v in $viejos) { Remove-Item $v.FullName -Force }
    if ($viejos) { Write-Host ("Rotación: {0} respaldos viejos borrados." -f $viejos.Count) }
    Write-Host ("Quedan {0} respaldos en $Destino." -f
        (Get-ChildItem $Destino -Filter "nodo-*.dump").Count)
} finally {
    $ErrorActionPreference = $preferenciaPrevia
    $env:PGPASSWORD = $null
}
