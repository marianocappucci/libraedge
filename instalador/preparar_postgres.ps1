# Prepara el PostgreSQL local del nodo: initdb, servicio y base.
#
# 🔴 SIN PROBAR. Escrito en un entorno sin PostgreSQL para Windows. Ver el
# README de esta carpeta.
#
# Se ejecuta como Administrador, una sola vez, desde el instalador.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizPostgres,   # donde estan los binarios (bin\initdb.exe)
    [Parameter(Mandatory = $true)][string]$DirectorioDatos, # el cluster
    [Parameter(Mandatory = $true)][string]$Password,        # del superusuario local
    [string]$NombreServicio = "LibraEdgePostgres",
    [int]$Puerto = 55432,
    [string]$Base = "restolibra",
    [string]$Zona = "America/Argentina/Buenos_Aires"
)

$ErrorActionPreference = "Stop"

$initdb = Join-Path $RaizPostgres "bin\initdb.exe"
$pgctl  = Join-Path $RaizPostgres "bin\pg_ctl.exe"
$psql   = Join-Path $RaizPostgres "bin\psql.exe"
foreach ($exe in @($initdb, $pgctl, $psql)) {
    if (-not (Test-Path $exe)) { throw "No está $exe. ¿La ruta de PostgreSQL es correcta?" }
}

# El puerto NO es el 5432 por defecto, a propósito: la PC del cliente puede tener
# ya un PostgreSQL de otra cosa, y pisarlo sería la peor forma de empezar.
if (Get-Service -Name $NombreServicio -ErrorAction SilentlyContinue) {
    Write-Host "El servicio $NombreServicio ya existe: no se toca el cluster."
    exit 0
}

if (Test-Path (Join-Path $DirectorioDatos "PG_VERSION")) {
    # ${...} y no $...: PowerShell lee `$Variable:` como una variable con ámbito
    # --la forma de $env:RUTA-- y el parser lo rechaza.
    Write-Host "Ya hay un cluster en ${DirectorioDatos}: no se re-inicializa."
} else {
    New-Item -ItemType Directory -Force -Path $DirectorioDatos | Out-Null

    # 🔴 La zona horaria se fija ACA y no después. La imagen/binario escribe
    # `timezone` en postgresql.conf una sola vez, en el initdb: sobre un cluster
    # que ya existe, cambiar la variable de entorno mueve el reloj del PROCESO y
    # no el del SERVIDOR, y `now()` --que estampa los DEFAULT de la base-- sigue
    # en la zona vieja. Se verifica con `select now()`, nunca con la hora del
    # sistema operativo.
    $env:TZ = $Zona
    $archivoPass = New-TemporaryFile
    try {
        Set-Content -Path $archivoPass -Value $Password -NoNewline -Encoding utf8
        & $initdb --pgdata="$DirectorioDatos" --username=postgres `
            --pwfile="$archivoPass" --encoding=UTF8 --locale=C `
            --auth-local=scram-sha-256 --auth-host=scram-sha-256
        if ($LASTEXITCODE -ne 0) { throw "initdb falló con código $LASTEXITCODE" }
    } finally {
        # El archivo tiene la contraseña del superusuario en texto plano.
        Remove-Item $archivoPass -Force -ErrorAction SilentlyContinue
    }

    $conf = Join-Path $DirectorioDatos "postgresql.conf"
    Add-Content -Path $conf -Encoding utf8 -Value @"

# --- LibraEdge ---
port = $Puerto
timezone = '$Zona'
log_timezone = '$Zona'
# Sólo local: el nodo y el producto corren en esta misma máquina, y exponer la
# base a la red del local sería una superficie que nadie va a vigilar.
listen_addresses = '127.0.0.1'
"@
}

& $pgctl register -N $NombreServicio -D "$DirectorioDatos" -S auto
if ($LASTEXITCODE -ne 0) { throw "pg_ctl register falló con código $LASTEXITCODE" }
Start-Service -Name $NombreServicio

# Esperar a que acepte conexiones. El servicio "Running" NO quiere decir que la
# base esté lista: el proceso arrancó, la recuperación puede seguir corriendo.
$env:PGPASSWORD = $Password
$listo = $false
foreach ($intento in 1..30) {
    & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -c "SELECT 1" *> $null
    if ($LASTEXITCODE -eq 0) { $listo = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $listo) { throw "PostgreSQL arrancó pero no acepta conexiones en el puerto $Puerto." }

$existe = & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname = '$Base'"
if ($existe -ne "1") {
    & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -c "CREATE DATABASE $Base"
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear la base $Base." }
}

# La verificación que importa: qué hora cree el SERVIDOR que es.
$ahora = & $psql -h 127.0.0.1 -p $Puerto -U postgres -d $Base -tAc "SELECT now()"
Write-Host "PostgreSQL listo en el puerto $Puerto. now() del servidor: $ahora"
Write-Host "Si ese offset no es -03, el initdb no tomó la zona y hay que rehacer el cluster."
$env:PGPASSWORD = $null
