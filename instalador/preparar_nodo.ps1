# Entorno, config.json del emisor, migraciones y los dos servicios del nodo.
#
# Lo corre `instalar.ps1` (como Administrador) despues de `preparar_postgres.ps1`.
# Lo propio del producto sale de `producto.json` (copia de
# `productos/<producto>.json`) por medio de `libraedge_instalador.py`, que es
# `libraedge/instalador.py` copiado tal cual: la lectura de la definicion, la
# validacion de la carga y el armado del entorno estan probados ahi (ADR-010).
#
# Historia de lo que se midio en una VM (2026-08-30/31): README de esta carpeta.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizNodo,   # donde quedo instalado el producto
    [Parameter(Mandatory = $true)][string]$Carga,      # carga-nodo.json (la emite el central)
    [string]$Datos = (Join-Path $env:ProgramData "LibraEdge"),
    [int]$IntervaloSegundos = 60
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "comun.ps1")

$python     = Join-Path $RaizNodo "python\python.exe"
$nssm       = Join-Path $RaizNodo "herramientas\nssm.exe"
$ayudante   = Join-Path $RaizNodo "instalador\libraedge_instalador.py"
$definicion = Join-Path $RaizNodo "instalador\producto.json"
foreach ($archivo in @($python, $nssm, $ayudante, $definicion)) {
    if (-not (Test-Path $archivo)) { throw "Falta $archivo. El paquete del instalador esta incompleto." }
}
if (-not (Test-Path $Carga)) { throw "No esta la carga del nodo: $Carga" }

$plan = Leer-Plan -Python $python -Ayudante $ayudante -Definicion $definicion
Write-Host "Producto: $($plan.nombre) (puerto $($plan.puerto), base del nodo $($plan.base_del_nodo))."

$archivoEnv  = Join-Path $RaizNodo "nodo.env"
$estado      = Join-Path $RaizNodo "estado.json"
$clavePg     = Join-Path $Datos "postgres.clave"
$dataDir     = Join-Path $Datos "app"

# --- El Python embebido tiene que ver sus paquetes ---------------------------
# El "Windows embeddable package" trae `import site` comentado en su `._pth`:
# sin eso `site-packages` no entra al path y todo lo de abajo muere con un
# "No module named ..." que apunta a otra cosa. `armar_paquete.sh` lo
# descomenta; esto lo verifica antes de seguir.
if ((Invocar -Programa $python -Argumentos @("-c", "import alembic, uvicorn, libraedge")) -ne 0) {
    throw ("El Python embebido no puede importar alembic, uvicorn o libraedge. Casi seguro es " +
           "el 'import site' comentado en python3XX._pth de $RaizNodo\python, o un paquete " +
           "armado sin el producto.")
}

# --- Parar lo que corre (reinstalacion) --------------------------------------
# Las migraciones no corren con el producto atendiendo, y en una actualizacion
# el producto viejo no tiene que escribir sobre un esquema nuevo.
foreach ($servicio in @("LibraEdgeNodo", "LibraEdgeProducto")) {
    if (Get-Service -Name $servicio -ErrorAction SilentlyContinue) {
        Write-Host "Se detiene $servicio para actualizar."
        & $nssm stop $servicio confirm | Out-Null
    }
}

# --- El archivo de entorno ---------------------------------------------------
# Contiene el secreto del nodo, que autoriza a escribir en el central. La ACL
# (SYSTEM y Administradores, por SID) se pone ANTES de escribir el contenido,
# y el ayudante solo escribe sobre un archivo que ya existe: crearlo con los
# permisos heredados y arreglarlos despues deja una ventana en la que cualquier
# usuario de la PC lo puede leer.
#
# Si ya existia (reinstalacion) NO se borra: el ayudante conserva SECRET_KEY y
# la clave del admin local. Un SECRET_KEY nuevo deja ilegible lo que libraauth
# cifro con el anterior.
#
# Lo que el ayudante imprime son NOMBRES de variables, nunca valores, y los
# secretos le llegan por archivo: ni la carga ni la clave de PostgreSQL pasan
# por la linea de comandos (que Inno Setup y Start-Transcript registran).
Proteger-Archivo -Ruta $archivoEnv
Proteger-Carpeta -Ruta $dataDir
# Despues de desinstalar, nodo.env ya no esta (tenia el secreto del nodo), pero
# el desinstalador guardo SECRET_KEY y la clave del admin local en
# conservado.env, al lado de los datos (igual que Linux, ADR-012).
$conservado = Join-Path $Datos "conservado.env"
$argumentos = @($ayudante, "entorno", "--definicion", $definicion, "--carga", $Carga, "--clave-pg", $clavePg,
    "--estado", $estado, "--data-dir", $dataDir, "--salida", $archivoEnv)
$hayConservado = (Test-Path $conservado) -and ((Get-Item $conservado).Length -gt 0)
if ($hayConservado) { $argumentos += @("--previo", $conservado) }
$codigo = Invocar -Programa $python -Argumentos $argumentos
if ($codigo -ne 0) { throw "No se pudo escribir $archivoEnv (codigo $codigo). El motivo esta arriba." }
Test-ArchivoProtegido -Ruta $archivoEnv -Lanzar | Out-Null
if ($hayConservado) {
    Write-Host "Se recuperaron SECRET_KEY y la clave del admin local de la instalacion anterior."
    Remove-Item $conservado -Force
}

# --- El config.json del emisor ------------------------------------------------
# CUIT, razon social, condicion de IVA y el formato del ticket: la facturacion
# del nodo los lee del `config.json` de DATA_DIR y el espejo no los trae (no es
# una tabla). Vienen en la carga, que los copio del `config.json` del central al
# registrar el nodo. Se fusionan: lo demas del archivo no se toca.
$codigo = Invocar -Programa $python -Argumentos @(
    $ayudante, "config-emisor", "--definicion", $definicion, "--carga", $Carga,
    "--config", (Join-Path $dataDir "config.json"))
if ($codigo -ne 0) { throw "No se pudo escribir el config.json del emisor (codigo $codigo)." }

# --- Las migraciones ----------------------------------------------------------
# Las MISMAS que el central y en el mismo orden (`migraciones` de la definicion
# = `configure(migraciones=...)` de `scripts/panel_admin.py` del producto): de
# eso vive que el espejo pueda escribir fila por fila sin traducir nada. Desde
# 2026-10 son cuatro cadenas (motor, auth, libracommerce, producto); el
# instalador de RestoLibra corria tres y le faltaba la de libracommerce.
#
# Se corren por el ayudante (`ejecutar`), que busca el console script en los
# entry points del Python embebido: el paquete se arma desde Linux y no trae
# los `.exe` de `Scripts`.
$cantidad = Cargar-EntornoEnProceso -Ruta $archivoEnv
Write-Host "Entorno del nodo cargado en este proceso ($cantidad variables)."
Push-Location $RaizNodo
try {
    foreach ($comando in $plan.migraciones) {
        $partes = @($comando)
        Write-Host "Migraciones: $($partes -join ' ')"
        $codigo = Invocar -Programa $python -Argumentos (@($ayudante, "ejecutar") + $partes)
        if ($codigo -ne 0) { throw "Fallo '$($partes -join ' ')' con codigo $codigo." }
    }

    # --- La primera bajada, ANTES de arrancar el producto ---------------------
    # El producto crea un admin local al arrancar si la tabla de usuarios esta
    # vacia. Si el espejo ya bajo los usuarios del central, no lo crea. Sin
    # conexion (o con una carga de prueba) no se puede: se avisa y se sigue, el
    # servicio del nodo sincroniza solo cuando haya central.
    Write-Host "Primera sincronizacion con el central..."
    $codigo = Invocar -Programa $python -Argumentos @("-m", "libraedge", "sincronizar")
    if ($codigo -ne 0) {
        Write-Warning ("La primera sincronizacion no termino bien (codigo $codigo). El nodo queda " +
                       "instalado y reintenta solo; hasta que baje la referencia, el producto arranca " +
                       "con un admin local que no se usa.")
    }
} finally {
    Pop-Location
}

# --- Los dos servicios ---------------------------------------------------------
# NSSM y no `sc.exe` a secas: un proceso de consola registrado con `sc` no
# maneja bien el apagado y Windows lo mata a los 30 segundos. NSSM ademas
# reinicia el proceso si se cae.
function Registrar-Servicio {
    param([string]$Nombre, [string]$Ejecutable, [string]$Argumentos, [string]$Descripcion)

    if (Get-Service -Name $Nombre -ErrorAction SilentlyContinue) {
        & $nssm stop $Nombre confirm | Out-Null
        & $nssm remove $Nombre confirm | Out-Null
    }
    & $nssm install $Nombre $Ejecutable $Argumentos
    if ($LASTEXITCODE -ne 0) { throw "nssm install $Nombre fallo" }
    & $nssm set $Nombre AppDirectory $RaizNodo | Out-Null
    & $nssm set $Nombre Description $Descripcion | Out-Null
    & $nssm set $Nombre Start SERVICE_AUTO_START | Out-Null
    & $nssm set $Nombre AppStdout (Join-Path $RaizNodo "logs\$Nombre.log") | Out-Null
    & $nssm set $Nombre AppStderr (Join-Path $RaizNodo "logs\$Nombre.log") | Out-Null
    # El freno del reinicio: NSSM reintenta a los 1500 ms y un arranque roto
    # giraba a dos por segundo (24 logs en dos minutos, medido 2026-08-30).
    & $nssm set $Nombre AppThrottle 15000 | Out-Null
    & $nssm set $Nombre AppRotateFiles 1 | Out-Null
    & $nssm set $Nombre AppRotateOnline 1 | Out-Null
    & $nssm set $Nombre AppRotateBytes 10485760 | Out-Null
}

New-Item -ItemType Directory -Force -Path (Join-Path $RaizNodo "logs") | Out-Null

Registrar-Servicio -Nombre "LibraEdgeProducto" -Ejecutable $python `
    -Argumentos "-m uvicorn $($plan.asgi) --host 0.0.0.0 --port $($plan.puerto)" `
    -Descripcion "$($plan.nombre), la instancia local de esta sucursal (LibraEdge)"

Registrar-Servicio -Nombre "LibraEdgeNodo" -Ejecutable $python `
    -Argumentos "-m libraedge correr --intervalo $IntervaloSegundos" `
    -Descripcion "Sincronizacion del nodo offline con el central (LibraEdge)"

# El entorno va a NSSM desde el archivo, para que el secreto viva en un solo
# lugar. NSSM lo guarda en el registro del servicio (HKLM, solo administradores).
$pares = @(Get-Content $archivoEnv -Encoding UTF8 | Where-Object { $_ -match "=" })
foreach ($servicio in @("LibraEdgeProducto", "LibraEdgeNodo")) {
    & $nssm set $servicio AppEnvironmentExtra $pares | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "No se pudo cargar el entorno de $servicio en NSSM." }
    Start-Service -Name $servicio
}

# --- El descubrimiento (ADR-017) ------------------------------------------------
# Las otras cajas del local encuentran esta PC preguntando por UDP 47823. Contesta
# libraedge_descubrimiento.py (solo biblioteca estandar) como LocalService, sin
# privilegios: lee SOLO descubrimiento.json, que no tiene secretos (producto,
# nombre, puerto, nodo, sucursal y negocio), y no puede leer nodo.env (su ACL es
# SYSTEM y Administradores). Sin logs propios: LocalService no escribe en {app}.
$descubrimiento = Join-Path $Datos "descubrimiento.json"
$codigo = Invocar -Programa $python -Argumentos @(
    $ayudante, "descubrimiento", "--definicion", $definicion, "--carga", $Carga, "--salida", $descubrimiento)
if ($codigo -ne 0) {
    Write-Warning "No se pudo escribir $descubrimiento (codigo $codigo): las otras cajas no van a encontrar esta PC solas."
} else {
    $servicioDesc = "LibraEdgeDescubrimiento"
    if (Get-Service -Name $servicioDesc -ErrorAction SilentlyContinue) {
        & $nssm stop $servicioDesc confirm | Out-Null
        & $nssm remove $servicioDesc confirm | Out-Null
    }
    $scriptDesc = Join-Path $RaizNodo "instalador\libraedge_descubrimiento.py"
    & $nssm install $servicioDesc $python "-I -B `"$scriptDesc`" responder --datos `"$descubrimiento`""
    if ($LASTEXITCODE -ne 0) { throw "nssm install $servicioDesc fallo" }
    & $nssm set $servicioDesc AppDirectory (Join-Path $RaizNodo "instalador") | Out-Null
    & $nssm set $servicioDesc Description "Contesta a las cajas del local que buscan esta PC (LibraEdge)" | Out-Null
    & $nssm set $servicioDesc Start SERVICE_AUTO_START | Out-Null
    & $nssm set $servicioDesc AppThrottle 15000 | Out-Null
    # Una cuenta del sistema sin clave. Sin el "" de la clave: Windows PowerShell 5.1 descarta los argumentos vacios
    # a un programa nativo. Si NSSM no la acepta asi, sc.exe (que no pide clave para LocalService).
    & $nssm set $servicioDesc ObjectName "NT AUTHORITY\LocalService" | Out-Null
    if ($LASTEXITCODE -ne 0) { & "$env:SystemRoot\System32\sc.exe" config $servicioDesc obj= "NT AUTHORITY\LocalService" | Out-Null }
    $cuenta = (Get-CimInstance Win32_Service -Filter "Name='$servicioDesc'").StartName
    if ($cuenta -ne "NT AUTHORITY\LocalService") {
        # Como SYSTEM no: es un proceso que escucha en la red. Sin el, las cajas escriben la IP a mano.
        & $nssm remove $servicioDesc confirm | Out-Null
        Write-Warning "No se pudo poner $servicioDesc como LocalService (quedo '$cuenta'): no se instala; las otras cajas no van a encontrar esta PC solas."
    } else {
        # Que no arranque no frena la instalacion del nodo: las cajas pueden escribir la IP. Se deja el motivo.
        try {
            Start-Service -Name $servicioDesc -ErrorAction Stop
            Write-Host "Descubrimiento: $((Get-Service $servicioDesc).Status) (UDP 47823, como LocalService)."
        } catch {
            Write-Warning "No arranco $servicioDesc ($($_.Exception.Message)): las otras cajas no van a encontrar esta PC solas."
            Get-WinEvent -FilterHashtable @{ LogName = "Application"; ProviderName = "nssm" } -MaxEvents 5 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host "    nssm: $($_.Message)" }
            Get-WinEvent -FilterHashtable @{ LogName = "System"; ProviderName = "Service Control Manager" } -MaxEvents 3 -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host "    scm: $($_.Message)" }
            Write-Host "    acl de ${descubrimiento}: $((Get-Acl $descubrimiento).AccessToString -replace "`r?`n", '; ')"
        }
    }
}

# --- El firewall ---------------------------------------------------------------
# Las cajas del local son navegadores que entran a esta PC por la red interna:
# sin la regla, Windows bloquea el puerto y solo anda desde esta misma PC. Solo
# en los perfiles Privado y Dominio: en una red marcada Publica no se abre.
$regla = "LibraEdge producto"
Get-NetFirewallRule -DisplayName $regla -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $regla -Direction Inbound -Protocol TCP -LocalPort $plan.puerto `
    -Action Allow -Profile Private, Domain | Out-Null
# El descubrimiento (ADR-017): UDP 47823, igual, solo en redes privadas y de dominio.
$reglaDesc = "LibraEdge descubrimiento"
Get-NetFirewallRule -DisplayName $reglaDesc -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $reglaDesc -Direction Inbound -Protocol UDP -LocalPort 47823 `
    -Action Allow -Profile Private, Domain | Out-Null
Write-Host "Firewall: puerto $($plan.puerto) (y UDP 47823) abierto en redes privadas y de dominio."

# --- El respaldo del nodo --------------------------------------------------------
# El `sync_outbox` es el unico lugar donde vive una venta cobrada sin internet.
# Cada hora, como tarea programada de SISTEMA. La clave de PostgreSQL la lee
# `respaldo.ps1` de su archivo: antes iba como argumento de la tarea, o sea en
# texto plano en la definicion de la tarea.
$bases = @($plan.bases | ForEach-Object { $_.nombre }) -join ","
$tareaRespaldo = "LibraEdgeRespaldo"
$accion = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$RaizNodo\instalador\respaldo.ps1`"" +
               " -RaizPostgres `"$RaizNodo\postgres`" -ArchivoClave `"$clavePg`"" +
               " -Bases `"$bases`" -BaseNodo `"$($plan.base_del_nodo)`"")
$disparador = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Unregister-ScheduledTask -TaskName $tareaRespaldo -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $tareaRespaldo -Action $accion -Trigger $disparador `
    -Settings $opciones -User "SYSTEM" -RunLevel Highest | Out-Null
Write-Host "Respaldo programado cada hora: $((Get-ScheduledTask -TaskName $tareaRespaldo).State)."

# --- La bandeja ------------------------------------------------------------------
# No puede ser un servicio (sesion 0): va como acceso directo en el inicio
# COMUN. La ruta del estado va como argumento: un acceso directo no hereda el
# entorno de NSSM.
$inicio = [Environment]::GetFolderPath("CommonStartup")
$acceso = Join-Path $inicio "LibraEdge - estado del nodo.lnk"
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($acceso)
$pythonw = Join-Path $RaizNodo "python\pythonw.exe"
$lnk.TargetPath = $(if (Test-Path $pythonw) { $pythonw } else { $python })
$lnk.Arguments = "-m libraedge bandeja --estado `"$estado`" --intervalo $IntervaloSegundos"
$lnk.WorkingDirectory = $RaizNodo
$lnk.Description = "Muestra si el nodo esta sincronizando"
$lnk.Save()
Write-Host "Bandeja: acceso directo en el inicio -> $acceso"
if ((Invocar -Programa $python -Argumentos @("-c", "import pystray")) -ne 0) {
    Write-Warning "Falta pystray en el paquete: el icono de la bandeja no va a aparecer."
}

# --- Los accesos directos del producto (ADR-013 y ADR-015) -------------------------
# "<Producto>" en el escritorio publico y en el menu Inicio: la app de escritorio
# (o Edge en modo aplicacion si la app no esta o falta WebView2), y
# "<Producto> (navegador)" en el menu Inicio como respaldo. Ver comun.ps1.
Crear-AccesosDelProducto -RaizNodo $RaizNodo -Plan $plan
$urlLocal = "http://localhost:$($plan.puerto)/"

# --- Como se entra, con la IP de esta PC (ADR-013) ----------------------------------
# Lo muestra la pagina final del asistente (nodo.iss lee acceso.txt) y queda en
# el log. La IP la elige el ayudante: la privada de la ruta por defecto.
$textoAcceso = Leer-Salida -Programa $python -Argumentos @($ayudante, "acceso", "--definicion", $definicion)
Write-Host ""
Write-Host "Instalado."
if ($textoAcceso.Codigo -eq 0 -and $textoAcceso.Texto) {
    Escribir-SinBom -Ruta (Join-Path $RaizNodo "instalador\acceso.txt") -Texto $textoAcceso.Texto
    Write-Host $textoAcceso.Texto
} else {
    Write-Host "En esta PC: el acceso directo `"$($plan.nombre)`" ($urlLocal); desde las otras cajas: http://<IP de esta PC>:$($plan.puerto)"
}
Write-Host "Verificar con: powershell -ExecutionPolicy Bypass -File `"$RaizNodo\instalador\verificar_nodo.ps1`""
