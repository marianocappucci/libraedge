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
    [string]$Zona = "America/Argentina/Buenos_Aires",
    # 🔴 El collation tiene que parecerse al del central, y `C` no se parece a
    # nada: ordena por bytes, o sea todas las mayúsculas antes que cualquier
    # minúscula. Medido en producción el 2026-08-30: la base de
    # `restolibra-demo` es **`en_US.utf8`**. Con `C` en el nodo, la misma carta
    # sale ordenada distinto en el mostrador que en el central.
    #
    # Se usa el proveedor **ICU** y no el de la libc porque los nombres de
    # locale de la libc de Windows no son los de glibc
    # (`English_United States.1252` contra `en_US.utf8`): con ICU, `en-US`
    # significa lo mismo en los dos sistemas operativos.
    #
    # ⚠️ ICU y glibc **no son byte-idénticos** en los casos raros. Es todo lo
    # cerca que se puede estar sin correr la misma libc, y es muchísimo más
    # cerca que `C`. No asumir igualdad exacta de orden entre nodo y central.
    [string]$Locale = "en-US"
)

$ErrorActionPreference = "Stop"

$initdb = Join-Path $RaizPostgres "bin\initdb.exe"
$pgctl  = Join-Path $RaizPostgres "bin\pg_ctl.exe"
$psql   = Join-Path $RaizPostgres "bin\psql.exe"
foreach ($exe in @($initdb, $pgctl, $psql)) {
    if (-not (Test-Path $exe)) { throw "No está $exe. ¿La ruta de PostgreSQL es correcta?" }
}

# 🔴 **Que el archivo esté NO quiere decir que el programa corra**, y esto
# voltea la instalación entera en una máquina recién formateada.
#
# El ZIP binario de PostgreSQL --el que este instalador usa a propósito, en vez
# del instalador de EDB-- **no trae el runtime de Visual C++**, y un Windows 11
# limpio tampoco lo tiene. Sin `vcruntime140.dll` y compañía, los cuatro
# ejecutables (`initdb`, `psql`, `pg_ctl`, `postgres`) mueren con
# `0xC0000135` / `-1073741515` = STATUS_DLL_NOT_FOUND, **sin escribir una sola
# línea de error**: ni siquiera `--version` contesta.
#
# Medido en una VM con Windows 11 Enterprise LTSC recién instalado el
# 2026-08-30: ninguna de las cuatro DLLs del runtime estaba en System32, ninguna
# venía en el ZIP, y los cuatro binarios fallaban igual. Instalando
# `vc_redist.x64.exe` los mismos binarios arrancaron sin tocar nada más.
#
# El instalador lo instala antes de llegar acá (ver `nodo.iss`). Esta guarda
# está para el caso en que no lo haya hecho: sin ella el síntoma es un código
# de salida negativo y nada más, que no lleva a ningún lado.
& $initdb --version *> $null
if ($LASTEXITCODE -eq -1073741515) {
    throw ("Los binarios de PostgreSQL no arrancan: falta el runtime de " +
           "Visual C++ (STATUS_DLL_NOT_FOUND). El ZIP de PostgreSQL no lo trae " +
           "y un Windows limpio no lo tiene. Instalar vc_redist.x64.exe " +
           "(https://aka.ms/vs/17/release/vc_redist.x64.exe) con " +
           "/install /quiet /norestart y volver a correr.")
}
if ($LASTEXITCODE -ne 0) {
    throw "initdb no pudo ejecutarse (código $LASTEXITCODE) antes de empezar."
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
        # 🔴 **Sin BOM, y por eso NO se usa `Set-Content -Encoding utf8`.**
        # En Windows PowerShell 5.1 --la que trae Windows 11 de fábrica, y la
        # que va a correr esto en la PC del cliente-- `-Encoding utf8` escribe
        # `EF BB BF` al principio. Medido el 2026-08-30 en 5.1.26100.
        #
        # `Get-Content` lo saca al leer, así que del lado de PowerShell no se
        # nota. Pero `initdb` es un programa en C: **se come el BOM como parte
        # de la contraseña**. El superusuario queda con una clave que este
        # mismo script no sabe, y la falla aparece tres pasos después, como
        # "arrancó pero no acepta conexiones" — que se lee como un problema de
        # arranque y no de credencial.
        [System.IO.File]::WriteAllText(
            $archivoPass, $Password, (New-Object System.Text.UTF8Encoding($false)))
        & $initdb --pgdata="$DirectorioDatos" --username=postgres `
            --pwfile="$archivoPass" --encoding=UTF8 `
            --locale-provider=icu --icu-locale=$Locale --locale=C `
            --auth-local=scram-sha-256 --auth-host=scram-sha-256
        if ($LASTEXITCODE -ne 0) {
            # No se cae a `--locale=C` en silencio: ese es justamente el estado
            # que este parámetro existe para evitar, y un nodo que ordena por
            # bytes no avisa de nada, sólo muestra la carta en otro orden.
            # Sin backticks en el mensaje: adentro de comillas dobles PowerShell
            # los lee como carácter de escape y se los come.
            throw ("initdb falló con código $LASTEXITCODE. Si el motivo es que " +
                   "este build no tiene ICU, hay que decidir el collation a mano " +
                   "y dejarlo escrito. Caer al collation C NO es una opción " +
                   "silenciosa: ordena por bytes y nada avisa.")
        }
    } finally {
        # El archivo tiene la contraseña del superusuario en texto plano.
        Remove-Item $archivoPass -Force -ErrorAction SilentlyContinue
    }

}

# 🔴 **Este bloque va FUERA del `else`, y es el arreglo de un defecto real.**
# Estaba adentro, o sea que sólo se escribía cuando el clúster era nuevo. En el
# camino de reparación --clúster que ya existe pero servicio que no, que es
# exactamente donde cae una instalación interrumpida-- el script seguía de
# largo, registraba el servicio, y después sondeaba el puerto $Puerto durante
# treinta segundos contra un PostgreSQL que estaba escuchando en el 5432. El
# error final decía "arrancó pero no acepta conexiones", que manda a mirar el
# arranque en vez de la configuración.
#
# Se aplica siempre y es idempotente: si la marca ya está, no se duplica.
$conf = Join-Path $DirectorioDatos "postgresql.conf"
$marca = "# --- LibraEdge ---"
if ((Get-Content $conf -Raw -ErrorAction SilentlyContinue) -notmatch [regex]::Escape($marca)) {
    Add-Content -Path $conf -Encoding utf8 -Value @"

$marca
port = $Puerto
timezone = '$Zona'
log_timezone = '$Zona'
# Sólo local: el nodo y el producto corren en esta misma máquina, y exponer la
# base a la red del local sería una superficie que nadie va a vigilar.
listen_addresses = '127.0.0.1'
"@
} else {
    Write-Host "postgresql.conf ya tiene el bloque de LibraEdge: no se duplica."
}

& $pgctl register -N $NombreServicio -D "$DirectorioDatos" -S auto
if ($LASTEXITCODE -ne 0) { throw "pg_ctl register falló con código $LASTEXITCODE" }
Start-Service -Name $NombreServicio

# Esperar a que acepte conexiones. El servicio "Running" NO quiere decir que la
# base esté lista: el proceso arrancó, la recuperación puede seguir corriendo.
#
# 🔴 **`$ErrorActionPreference` baja a `Continue` para todo lo que hable con
# `psql`, y sin eso el bucle de reintentos no existe.** Con `Stop`, PowerShell
# convierte lo que un programa nativo escribe en stderr en un error terminante,
# y `*> $null` no lo evita: redirige la salida, no la conversión. El primer
# intento fallido --que es el caso NORMAL mientras la base todavía arranca--
# abortaba el script en vez de reintentar, y el mensaje era el de psql, no el de
# acá.
#
# Acá no se notó porque en la VM la base aceptó conexiones en el primer intento.
# En la PC de un restaurante, más lenta, el bucle es justamente lo que hace
# falta. Se descubrió por el mismo defecto en `preparar_nodo.ps1`, donde sí se
# vio (2026-08-30).
$preferenciaPrevia = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$env:PGPASSWORD = $Password
$listo = $false
foreach ($intento in 1..30) {
    & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -c "SELECT 1" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $listo = $true; break }
    Start-Sleep -Seconds 1
}
$ErrorActionPreference = $preferenciaPrevia
if (-not $listo) { throw "PostgreSQL arrancó pero no acepta conexiones en el puerto $Puerto." }

$existe = & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname = '$Base'"
if ($existe -ne "1") {
    & $psql -h 127.0.0.1 -p $Puerto -U postgres -d postgres -c "CREATE DATABASE $Base"
    if ($LASTEXITCODE -ne 0) { throw "No se pudo crear la base $Base." }
}

# --- Las dos verificaciones que no se pueden posponer -----------------------
# Las dos miran el estado REAL del servidor, no los flags que se le pasaron:
# initdb puede haber ignorado cualquiera de los dos y salir con código 0.
# Y las dos son irreversibles sin rehacer el clúster, así que si están mal,
# están mal AHORA y no dentro de seis meses con datos adentro.

# 1. Qué hora cree el SERVIDOR que es. No la del sistema operativo.
$ahora = & $psql -h 127.0.0.1 -p $Puerto -U postgres -d $Base -tAc "SELECT now()"
Write-Host "PostgreSQL listo en el puerto $Puerto. now() del servidor: $ahora"
if ($ahora -notmatch "-03") {
    Write-Warning ("La zona NO quedó en -03 (now() = $ahora). El initdb no tomó " +
                   "'$Zona' y hay que rehacer el cluster ANTES de cargar datos.")
}

# 2. Con qué criterio ordena. `C` ordena por bytes y el central no.
#
# 🔴 **No se concatena en SQL, y por algo.** La primera versión de esto hacía
# `datlocprovider || '/' || coalesce(...)` y moría con
# *operator is not unique: "char" || unknown*: `datlocprovider` es del tipo
# `"char"` y PostgreSQL no sabe qué `||` elegir.
#
# Lo grave no fue el error sino cómo falló. Medido en la VM el 2026-08-30:
# `psql` devolvió **vacío**, este bloque imprimió "Collation del nodo:" sin
# valor, el `-match` sobre la cadena vacía dio falso, y el script siguió con
# código 0. La verificación que se agregó justamente para que el collation no
# pasara desapercibido **no podía fallar**.
#
# Se piden las dos columnas por separado y las junta `psql` con su separador
# (`-tA` usa `|`): sin concatenación no hay operador que resolver.
$orden = & $psql -h 127.0.0.1 -p $Puerto -U postgres -d $Base -tAc `
    "SELECT datlocprovider, coalesce(daticulocale, datcollate) FROM pg_database WHERE datname = current_database()"
$orden = ($orden | Out-String).Trim()
if (-not $orden) {
    # Y si vuelve vacío, se grita. Un chequeo mudo es peor que no tenerlo:
    # ocupa el lugar del que sí habría avisado.
    Write-Warning ("No se pudo leer el collation del cluster (la consulta no " +
                   "devolvió nada). Verificar a mano con: " +
                   "select datlocprovider, daticulocale, datcollate from pg_database.")
} else {
    Write-Host "Collation del nodo: $orden   (el central es c|en_US.utf8)"
    # `i|...` es ICU, que es lo buscado. `c|C` es libc con collation C: bytes.
    if ($orden -match "\|C$") {
        Write-Warning ("El cluster quedó en collation C: ordena por BYTES, con todas " +
                       "las mayúsculas antes que las minúsculas. La carta va a salir " +
                       "en otro orden que en el central. Rehacer el cluster.")
    }
}

$env:PGPASSWORD = $null
