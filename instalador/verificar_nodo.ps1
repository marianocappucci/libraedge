# Verifica un nodo instalado. Corre como Administrador (lee nodo.env y la clave
# de PostgreSQL, que solo leen SYSTEM y Administradores) y NO imprime ningun
# valor secreto: de nodo.env solo nombra variables.
#
#     powershell -ExecutionPolicy Bypass -File "C:\Program Files\LibraEdge\instalador\verificar_nodo.ps1"
#
# Sale con la cantidad de chequeos que fallaron (0 = todo bien). Con
# -SinCentral, que el nodo no vea al central es un AVISO y no una falla: es lo
# esperable con una carga de prueba (la VM) o con el local sin internet.

[CmdletBinding()]
param(
    [string]$RaizNodo = (Join-Path $env:ProgramFiles "LibraEdge"),
    [string]$Datos = (Join-Path $env:ProgramData "LibraEdge"),
    [int]$PuertoPostgres = 55432,
    [int]$IntervaloSegundos = 60,
    [switch]$SinCentral
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "comun.ps1")

$script:fallas = 0
function Bien([string]$Que)  { Write-Host "OK     $Que" }
function Aviso([string]$Que) { Write-Host "AVISO  $Que" }
function Falla([string]$Que) { Write-Host "FALLA  $Que"; $script:fallas++ }

$python     = Join-Path $RaizNodo "python\python.exe"
$ayudante   = Join-Path $RaizNodo "instalador\libraedge_instalador.py"
$definicion = Join-Path $RaizNodo "instalador\producto.json"
$archivoEnv = Join-Path $RaizNodo "nodo.env"
$clavePg    = Join-Path $Datos "postgres.clave"
$psql       = Join-Path $RaizNodo "postgres\bin\psql.exe"

foreach ($archivo in @($python, $ayudante, $definicion, $archivoEnv, $clavePg, $psql)) {
    if (-not (Test-Path $archivo)) { Falla "No esta $archivo"; exit $script:fallas }
}
$plan = Leer-Plan -Python $python -Ayudante $ayudante -Definicion $definicion
Write-Host "Nodo LibraEdge de $($plan.nombre) en $RaizNodo"
Write-Host ""

# --- 1. Los servicios ---------------------------------------------------------
foreach ($nombre in @("LibraEdgePostgres", "LibraEdgeProducto", "LibraEdgeNodo")) {
    $s = Get-Service -Name $nombre -ErrorAction SilentlyContinue
    if (-not $s) { Falla "servicio ${nombre}: no existe"; continue }
    $inicio = (Get-CimInstance Win32_Service -Filter "Name='$nombre'").StartMode
    if ($s.Status -eq "Running" -and $inicio -eq "Auto") { Bien "servicio ${nombre}: corriendo, inicio automatico" }
    else { Falla "servicio ${nombre}: estado $($s.Status), inicio $inicio" }
}

# --- 2. El archivo de entorno -----------------------------------------------------
if (Test-ArchivoProtegido -Ruta $archivoEnv) { Bien "nodo.env: solo SYSTEM y Administradores" }
else { Falla "nodo.env: la ACL deja leer el secreto a otras cuentas" }
$bytes = [System.IO.File]::ReadAllBytes($archivoEnv)
if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
    Falla "nodo.env: tiene BOM"
}
$r = Leer-Salida -Programa $python -Argumentos @($ayudante, "revisar-entorno", "--definicion", $definicion, "--entorno", $archivoEnv)
if ($r.Codigo -eq 0) { Bien "nodo.env: estan todas las variables del producto" }
else { Falla "nodo.env: faltan variables -> $($r.Texto -replace "`r?`n", '; ')" }
Cargar-EntornoEnProceso -Ruta $archivoEnv | Out-Null

# --- 3. PostgreSQL embebido -----------------------------------------------------------
$env:PGPASSWORD = ([System.IO.File]::ReadAllText($clavePg)).Trim()
function Consultar([string]$Base, [string]$Sql) {
    $r = Leer-Salida -Programa $psql -Argumentos @("-h", "127.0.0.1", "-p", "$PuertoPostgres", "-U", "postgres",
                                                    "-d", $Base, "-tAc", $Sql)
    return $r
}
$r = Consultar "postgres" "SELECT now()"
if ($r.Codigo -ne 0) {
    Falla "PostgreSQL no acepta conexiones en 127.0.0.1:$PuertoPostgres"
} else {
    if ($r.Texto -match "-03$|-03:00$") { Bien "PostgreSQL arriba; now() = $($r.Texto) (zona -03)" }
    else { Falla "PostgreSQL arriba pero now() = $($r.Texto): la zona no es -03" }
    $c = Consultar "postgres" "SHOW data_checksums"
    if ($c.Texto -eq "on") { Bien "PostgreSQL: data_checksums on" } else { Aviso "PostgreSQL: data_checksums $($c.Texto)" }

    # --- 4. Migraciones ------------------------------------------------------------------
    # Cada cadena de Alembic deja su tabla `alembic_version*` con una fila. Se
    # cuentan las de todas las bases contra los comandos de la definicion.
    $cadenas = 0
    foreach ($base in @($plan.bases | ForEach-Object { $_.nombre })) {
        $t = Consultar $base ("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()" +
                              " AND table_name LIKE 'alembic%' ORDER BY 1")
        foreach ($tabla in @($t.Texto -split "`r?`n" | Where-Object { $_ -like "alembic_version*" })) {
            $v = Consultar $base "SELECT string_agg(version_num, ',') FROM $tabla"
            if ($v.Texto) { $cadenas++; Write-Host "       $base.$tabla = $($v.Texto)" }
            else { Falla "migraciones: $base.$tabla esta vacia" }
        }
    }
    $esperadas = @($plan.migraciones).Count
    if ($cadenas -ge $esperadas) { Bien "migraciones: $cadenas cadenas aplicadas (la definicion corre $esperadas)" }
    else { Falla "migraciones: $cadenas cadenas aplicadas y la definicion corre $esperadas" }
    $o = Consultar $plan.base_del_nodo "SELECT count(1) FROM sync_outbox"
    if ($o.Codigo -eq 0) { Bien "migraciones: sync_outbox existe en $($plan.base_del_nodo) ($($o.Texto) operaciones)" }
    else { Falla "migraciones: no hay sync_outbox en $($plan.base_del_nodo)" }
}
$env:PGPASSWORD = $null

# --- 5. libraedge-nodo estado ------------------------------------------------------------
$r = Leer-Salida -Programa $python -Argumentos @("-m", "libraedge", "estado")
if ($r.Codigo -ne 0) {
    Falla "libraedge-nodo estado: sin estado ($($r.Texto))"
} else {
    $estado = $r.Texto | ConvertFrom-Json
    Bien "libraedge-nodo estado: contesta"
    if ($estado.en_linea) { Bien "nodo en linea con el central" }
    elseif ($SinCentral) { Aviso "nodo sin conexion con el central (esperado con -SinCentral): $($estado.ultimo_error)" }
    else { Falla "nodo sin conexion con el central: $($estado.ultimo_error)" }
}

# --- 6. El archivo de estado ---------------------------------------------------------------
$archivoEstado = $env:LIBRAEDGE_ESTADO
if (-not $archivoEstado -or -not (Test-Path $archivoEstado)) {
    Falla "estado.json: no existe ($archivoEstado)"
} else {
    $edad = ((Get-Date) - (Get-Item $archivoEstado).LastWriteTime).TotalSeconds
    $limite = 3 * $IntervaloSegundos + 30
    if ($edad -le $limite) { Bien ("estado.json: escrito hace {0:N0} s" -f $edad) }
    else { Falla ("estado.json: escrito hace {0:N0} s (mas de {1} s: el servicio del nodo no cicla)" -f $edad, $limite) }
}

# --- 7. El producto respondiendo -------------------------------------------------------------
# La salud tiene que ser un objeto JSON: el catch-all de la SPA contesta 200
# con el index.html a cualquier ruta, y eso no prueba nada.
$url = "http://127.0.0.1:$($plan.puerto)$($plan.salud)"
try {
    $respuesta = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
    $cuerpo = $respuesta.Content | ConvertFrom-Json
    if ($respuesta.StatusCode -eq 200 -and $cuerpo -is [pscustomobject]) { Bien "$($plan.nombre) responde en $url" }
    else { Falla "$($plan.nombre) en ${url}: $($respuesta.StatusCode) sin un objeto JSON" }
} catch {
    Falla "$($plan.nombre) no responde en ${url}: $($_.Exception.Message)"
}

# --- 8. El emisor y lo demas --------------------------------------------------------------------
$config = Join-Path $env:DATA_DIR "config.json"
if (Test-Path $config) {
    $cfg = Get-Content $config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfg.empresa_cuit -and $cfg.empresa_iva_condition) {
        Bien "config.json: emisor con CUIT y condicion de IVA ($($cfg.empresa_iva_condition))"
    } else { Aviso "config.json: falta CUIT o condicion de IVA del emisor; el nodo no va a poder facturar" }
} else {
    Aviso "config.json: no existe en $($env:DATA_DIR); el nodo no va a poder facturar"
}
if (Get-ScheduledTask -TaskName "LibraEdgeRespaldo" -ErrorAction SilentlyContinue) { Bien "tarea de respaldo programada" }
else { Falla "no esta la tarea de respaldo LibraEdgeRespaldo" }

Write-Host ""
if ($script:fallas -eq 0) { Write-Host "Nodo verificado: todo bien." }
else { Write-Host "Nodo con $($script:fallas) fallas." }
exit $script:fallas
