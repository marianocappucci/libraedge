# Migraciones, archivo de entorno y los dos servicios del nodo.
#
# 🔴 SIN PROBAR. Ver el README de esta carpeta.
#
# Corre después de `preparar_postgres.ps1`, como Administrador.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizNodo,      # donde quedó instalado el producto
    [Parameter(Mandatory = $true)][string]$UrlBase,       # postgresql://... del PG local
    [Parameter(Mandatory = $true)][string]$UrlCentral,    # https://cliente.restolibra.com.ar
    [Parameter(Mandatory = $true)][string]$NodeId,
    [Parameter(Mandatory = $true)][string]$NodeSecret,    # lo emite el central, UNA sola vez
    [Parameter(Mandatory = $true)][string]$TablasEspejo,  # lo imprime `nodo_offline registrar`
    [int]$PuertoProducto = 8000,
    [int]$IntervaloSegundos = 60,
    # El prefijo del producto, que es lo que `libracore-migrar` usa para
    # encontrar la base del motor. Es un parámetro y no una constante porque el
    # nodo va a existir para más de un producto de la familia.
    [string]$Prefijo = "restolibra",
    # La clave del superusuario de PostgreSQL. Va SUELTA y no se saca de
    # `$UrlBase` a propósito: parsear una URL para recuperar una contraseña
    # falla en cuanto la contraseña tiene un `@`, un `/` o un `:`, y falla
    # devolviendo una cadena que parece válida. La necesita la tarea de
    # respaldo, que se conecta sin pasar por la URL.
    [string]$ClavePostgres = ""
)

$ErrorActionPreference = "Stop"

$python = Join-Path $RaizNodo "python\python.exe"
$nssm   = Join-Path $RaizNodo "herramientas\nssm.exe"
if (-not (Test-Path $python)) { throw "No está el Python embebido en $python" }
if (-not (Test-Path $nssm)) { throw "No está nssm.exe en $nssm" }

$archivoEnv = Join-Path $RaizNodo "nodo.env"
$estado     = Join-Path $RaizNodo "estado.json"

# --- El archivo de entorno ------------------------------------------------
# 🔴 Contiene el secreto del nodo, que autoriza a escribir en el central. Se
# escribe con ACL restringida ANTES de poner el contenido: crearlo con los
# permisos heredados y arreglarlos después deja una ventana en la que cualquier
# usuario de la máquina puede leerlo.
#
# 🔴 **Las cuentas van por SID y no por nombre.** `"Administrators"` no existe
# en un Windows en español: ahí el grupo se llama `Administradores`, y
# `FileSystemAccessRule("Administrators", ...)` tira `IdentityNotMappedException`.
# Medido el 2026-08-30 sobre un Windows 11 Pro es-AR: `SYSTEM` resuelve,
# `Administrators` **no**, `Administradores` sí.
#
# Las PCs de los clientes son justamente Windows en español, así que el nombre
# literal habría hecho fallar el instalador en el 100% de las instalaciones
# reales — y en el 0% de cualquier prueba corrida en un Windows en inglés.
#
# Los SIDs conocidos no dependen del idioma: `S-1-5-18` es SYSTEM y
# `S-1-5-32-544` el grupo de administradores locales.
if (Test-Path $archivoEnv) { Remove-Item $archivoEnv -Force }
New-Item -ItemType File -Path $archivoEnv | Out-Null
$acl = Get-Acl $archivoEnv
$acl.SetAccessRuleProtection($true, $false)   # corta la herencia
foreach ($sid in @("S-1-5-18", "S-1-5-32-544")) {
    $cuenta = New-Object System.Security.Principal.SecurityIdentifier($sid)
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $cuenta, "FullControl", "Allow")))
}
Set-Acl -Path $archivoEnv -AclObject $acl

# Que la ACL haya quedado NO se asume: `Set-Acl` puede fallar parcialmente y el
# archivo con el secreto seguiría legible por cualquier usuario de la máquina.
$quedaron = (Get-Acl $archivoEnv).Access |
    ForEach-Object { $_.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]).Value }
foreach ($esperado in @("S-1-5-18", "S-1-5-32-544")) {
    if ($quedaron -notcontains $esperado) { throw "La ACL de $archivoEnv no quedó: falta $esperado" }
}
$deMas = $quedaron | Where-Object { $_ -notin @("S-1-5-18", "S-1-5-32-544") }
if ($deMas) { throw "La ACL de $archivoEnv dejó cuentas de más: $($deMas -join ', ')" }

# Sin BOM. `Set-Content -Encoding utf8` en Windows PowerShell 5.1 --la que trae
# Windows 11 de fábrica-- escribe `EF BB BF` adelante. Acá abajo el archivo lo
# vuelve a leer `Get-Content`, que **sí** saca el BOM, así que por ese camino no
# se rompe nada: eso está medido. Se escribe sin BOM igual porque el archivo es
# un `.env` y cualquier lector que no sea PowerShell --un `python-dotenv`, por
# ejemplo-- se comería el BOM como parte del nombre de la primera variable, y
# `LIBRAEDGE_NODE_ID` desaparecería sin que nada avise.

# 🔴 **El `SECRET_KEY` del producto, que faltaba.** Sin él la app no levanta:
# `libraauth.session_auth` aborta con *"SECRET_KEY no está seteado. No se
# levanta la app sin un secreto propio"*. Medido en la VM el 2026-08-30: el
# servicio quedaba en bucle de reinicio, Windows lo mostraba **Running**, y no
# había nada escuchando en el puerto.
#
# Se genera acá, uno por nodo, y no se pide al que instala: es un secreto de
# esta máquina y nadie lo tiene que ver ni transcribir. Va al mismo archivo con
# la misma ACL que el secreto del nodo.
#
# 🔴 Y **no** se usa `Get-Random`: es un generador reproducible, no
# criptográfico. Para un secreto de sesión eso es exactamente lo que no sirve.
$bytes = New-Object byte[] 48
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$claveSesion = [Convert]::ToBase64String($bytes)

$contenido = @"
LIBRAEDGE_NODE_ID=$NodeId
LIBRAEDGE_NODE_SECRET=$NodeSecret
LIBRAEDGE_CENTRAL_URL=$UrlCentral
LIBRAEDGE_DATABASE_URL=$UrlBase
LIBRAEDGE_TABLAS_ESPEJO=$TablasEspejo
LIBRAEDGE_ESTADO=$estado
RESTOLIBRA_DATABASE_URL=$UrlBase
SECRET_KEY=$claveSesion
TZ=America/Argentina/Buenos_Aires
"@
[System.IO.File]::WriteAllText(
    $archivoEnv, $contenido, (New-Object System.Text.UTF8Encoding($false)))

# --- Las migraciones ------------------------------------------------------
# El nodo corre LAS MISMAS que el central: es lo que hace que el espejo pueda
# escribir fila por fila sin traducir nada. Si divergen, el aplicador del nodo
# falla por una columna que no existe.
#
# 🔴 Antes de migrar, chequear que el Python embebido pueda VER sus paquetes.
# El "Windows embeddable package" de python.org viene con un `python3XX._pth`
# donde `import site` está comentado: con eso apagado `site-packages` no entra
# al path y `-m alembic` muere con "No module named alembic" aunque alembic esté
# instalado ahí mismo. El error apunta a una dependencia faltante y el problema
# es un archivo de configuración de dos líneas.
#
# 🔴 **Y el chequeo se hace con `$ErrorActionPreference` bajado a mano.**
# Con `Stop` --que es como arranca este script-- PowerShell convierte lo que un
# programa nativo escribe en stderr en un error terminante, y `*> $null` NO lo
# evita: redirige la salida, no la conversión. O sea que el `python -c` que
# falla **lanza en esa misma línea** y las cinco de abajo no se ejecutan nunca.
#
# Medido en la VM el 2026-08-30: el instalador escupió un traceback de Python
# crudo y salió con 1, sin decir una palabra del `import site`. El guard estaba
# escrito, probado a ojo, y **no podía hablar**.
$preferenciaPrevia = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $python -c "import alembic" 2>&1 | Out-Null
$puedeImportar = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $preferenciaPrevia
if (-not $puedeImportar) {
    throw ("El Python embebido no puede importar alembic. Casi seguro es el " +
           "'import site' comentado en el archivo python3XX._pth de " +
           "$RaizNodo\python: descomentarlo y volver a correr. Si ya está " +
           "descomentado, entonces sí falta instalar el producto en esa carga.")
}

#
# 🔴 **SON DOS CADENAS, no una, y el orden importa.**
#
# Hasta el 2026-08-30 acá sólo corría `alembic upgrade head`, o sea la cadena
# PROPIA del producto. Falta la del motor, que es la que crea `facturas`,
# `cajas`, `usuarios` y todo el schema de LibraCore. El central corre las dos
# --ver `scripts/panel_admin.py` y `scripts/nuevo_cliente.py` del producto, que
# tienen exactamente esta tupla-- y el nodo tiene que correr LAS MISMAS: de eso
# vive que el espejo pueda escribir fila por fila sin traducir nada.
#
# Medido en la VM: con una sola cadena, la migración muere en el `0001` con
# *relation "facturas" does not exist* y la base del nodo queda con 21 tablas y
# sin `cajas`, `usuarios`, `sync_outbox` ni `edge_nodes`.
#
# `libracore-migrar` es el console script del motor, igual que en el central, y
# resuelve la base por `<PREFIJO>_DATABASE_URL` cuando no hay una del core
# aparte. Se invoca el ejecutable y no `python -m`: es la misma forma que usa el
# central, y las migraciones del motor viajan dentro del wheel.
$migrar = Join-Path $RaizNodo "python\Scripts\libracore-migrar.exe"
if (-not (Test-Path $migrar)) {
    throw ("No está $migrar. La carga del producto se armó sin instalar " +
           "libracore[migrations] en el Python embebido.")
}

Push-Location $RaizNodo
try {
    Set-Item -Path ("Env:{0}_DATABASE_URL" -f $Prefijo.ToUpper()) -Value $UrlBase
    $env:RESTOLIBRA_DATABASE_URL = $UrlBase

    & $migrar upgrade --prefijo $Prefijo
    if ($LASTEXITCODE -ne 0) { throw "Las migraciones del motor fallaron con código $LASTEXITCODE" }

    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Las migraciones del producto fallaron con código $LASTEXITCODE" }
} finally {
    Pop-Location
}

# --- Los dos servicios ----------------------------------------------------
# NSSM y no `sc.exe` a secas: un proceso de consola registrado con `sc` no
# maneja bien el apagado y Windows lo mata a los 30 segundos. NSSM además
# reinicia el proceso si se cae, que en la PC de un cliente es la diferencia
# entre un corte de un minuto y uno hasta que alguien lo note.

function Registrar-Servicio {
    param([string]$Nombre, [string]$Ejecutable, [string]$Argumentos, [string]$Descripcion)

    if (Get-Service -Name $Nombre -ErrorAction SilentlyContinue) {
        Write-Host "El servicio $Nombre ya existe: se detiene para reconfigurarlo."
        & $nssm stop $Nombre confirm | Out-Null
        & $nssm remove $Nombre confirm | Out-Null
    }
    & $nssm install $Nombre $Ejecutable $Argumentos
    if ($LASTEXITCODE -ne 0) { throw "nssm install $Nombre falló" }
    & $nssm set $Nombre AppDirectory $RaizNodo
    # El entorno se carga después, en el bucle de abajo: viene del archivo, para
    # que el secreto del nodo viva en un solo lugar.
    & $nssm set $Nombre Description $Descripcion
    & $nssm set $Nombre Start SERVICE_AUTO_START
    & $nssm set $Nombre AppStdout (Join-Path $RaizNodo "logs\$Nombre.log")
    & $nssm set $Nombre AppStderr (Join-Path $RaizNodo "logs\$Nombre.log")
    # 🔴 El freno del reinicio. NSSM reintenta a los 1500 ms por defecto, y un
    # proceso que muere al arrancar --por una variable que falta, digamos-- gira
    # a dos por segundo. Medido en la VM el 2026-08-30: **24 archivos de log en
    # dos minutos**, y el bucle escondido detrás de un servicio que Windows
    # muestra `Running`. Con 15 s el servicio sigue recuperándose solo de una
    # caída real, pero un arranque roto no quema disco ni tapa el log.
    & $nssm set $Nombre AppThrottle 15000
    & $nssm set $Nombre AppRotateFiles 1
    # Y que rote por TAMAÑO, no en cada arranque: con rotación por arranque, un
    # bucle deja un archivo por intento y el log deja de servir para leerlo.
    & $nssm set $Nombre AppRotateOnline 1
    & $nssm set $Nombre AppRotateBytes 10485760
}

New-Item -ItemType Directory -Force -Path (Join-Path $RaizNodo "logs") | Out-Null

# NSSM toma el entorno como pares NUL-separados; se arma desde el archivo para
# no repetir los valores acá (y para que el secreto viva en un solo lugar).
$pares = Get-Content $archivoEnv | Where-Object { $_ -match "=" }

Registrar-Servicio -Nombre "LibraEdgeProducto" -Ejecutable $python `
    -Argumentos "-m uvicorn app.asgi:app --host 0.0.0.0 --port $PuertoProducto" `
    -Descripcion "Restolibra, la instancia local de esta sucursal"

Registrar-Servicio -Nombre "LibraEdgeNodo" -Ejecutable $python `
    -Argumentos "-m libraedge correr --intervalo $IntervaloSegundos" `
    -Descripcion "Sincronizacion del nodo offline con el central"

foreach ($servicio in @("LibraEdgeProducto", "LibraEdgeNodo")) {
    & $nssm set $servicio AppEnvironmentExtra $pares
    Start-Service -Name $servicio
}

# --- El respaldo del nodo -------------------------------------------------
#
# 🔴 El `sync_outbox` es **el único lugar del mundo** donde vive una venta
# cobrada sin internet: todavía no llegó al central, y en el central no está. Si
# el disco muere entre el cobro y la sincronización, esa plata no está en ningún
# lado. Hasta el 2026-08-31 el instalador no contemplaba ningún respaldo.
#
# Cada hora, y no una vez por día, porque lo que se pierde es exactamente lo
# cobrado desde el último: con el enlace sano el outbox se vacía en un minuto,
# pero un local que estuvo el día entero sin internet acumula el día entero.
#
# Va como tarea programada de SISTEMA y no como servicio: corre un rato cada
# hora, no permanentemente, y el planificador ya sabe recuperarse de un apagón
# --si la PC estaba apagada a la hora en punto, corre al encender--.
$tareaRespaldo = "LibraEdgeRespaldo"
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$RaizNodo\instalador\respaldo.ps1`"" +
               " -RaizPostgres `"$RaizNodo\postgres`" -Password `"$ClavePostgres`"")
$disparador = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Unregister-ScheduledTask -TaskName $tareaRespaldo -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $tareaRespaldo -Action $accion -Trigger $disparador `
    -Settings $opciones -User "SYSTEM" -RunLevel Highest | Out-Null
Write-Host "Respaldo programado cada hora en $((Get-ScheduledTask -TaskName $tareaRespaldo).State)."

# --- La bandeja -------------------------------------------------------------
#
# 🔴 **No puede ser un servicio.** Windows no le deja dibujar nada a un servicio
# desde la sesión 0, así que el ícono al lado del reloj tiene que arrancar en la
# sesión del operador. Va como acceso directo en el inicio COMÚN: cualquiera que
# entre a esa PC lo ve, y se apaga sacándolo de ahí, sin tocar el instalador.
#
# La ruta del estado va como ARGUMENTO y no por entorno: un acceso directo del
# inicio no hereda el entorno que NSSM le da a los servicios, y una variable de
# máquina ensuciaría el sistema entero por un dato de una sola aplicación.
$inicio = [Environment]::GetFolderPath("CommonStartup")
$acceso = Join-Path $inicio "LibraEdge - estado del nodo.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($acceso)
# `pythonw.exe` y no `python.exe`: con el segundo queda una consola negra
# abierta toda la jornada al lado del ícono.
$pythonw = Join-Path $RaizNodo "python\pythonw.exe"
$lnk.TargetPath = $(if (Test-Path $pythonw) { $pythonw } else { $python })
$lnk.Arguments = "-m libraedge bandeja --estado `"$estado`" --intervalo $IntervaloSegundos"
$lnk.WorkingDirectory = $RaizNodo
$lnk.Description = "Muestra si el nodo esta sincronizando"
$lnk.Save()
Write-Host "Bandeja: acceso directo en el inicio -> $acceso"
if (-not (Test-Path $pythonw)) {
    Write-Warning ("No esta pythonw.exe: la bandeja va a abrir una consola. " +
                   "El paquete embebido de python.org lo trae; revisar la carga.")
}
# Que pystray este NO se puede dar por hecho: viaja en el extra `bandeja`, que
# no es una dependencia del producto. Sin el, el acceso directo no hace nada y
# nadie se entera hasta que alguien pregunta por que no aparece el icono.
$preferenciaPystray = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $python -c "import pystray" 2>&1 | Out-Null
$hayPystray = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $preferenciaPystray
if (-not $hayPystray) {
    Write-Warning ("Falta pystray: el acceso directo de la bandeja no va a mostrar " +
                   "nada. Se instala con `pip install libraedge[bandeja]` al armar " +
                   "la carga, no en la PC del cliente.")
}

Write-Host ""
Write-Host "Instalado. El producto escucha en http://<esta-pc>:$PuertoProducto"
Write-Host "Estado del nodo:  $python -m libraedge estado"
Write-Host ""
Write-Host "ATENCION: Verificar A MANO, porque el instalador no lo puede probar solo:"
Write-Host "   1. Reiniciar la PC y confirmar que los dos servicios levantan."
Write-Host '   2. select now() en la base: el offset tiene que ser -03.'
Write-Host "   3. Desconectar internet, cobrar, reconectar, y ver que la venta suba."
