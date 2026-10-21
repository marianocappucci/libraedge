# Configura el nodo despues de que Inno Setup copio los archivos. Es lo unico
# que el .iss ejecuta (ADR-010): el instalador revisa su codigo de salida y lo
# devuelve como el suyo, asi que una instalacion desatendida que falla no
# termina con 0.
#
# Se puede volver a correr a mano para reparar (todo es idempotente):
#     $env:LIBRAEDGE_CODIGO = "K7Q4-M2XP"
#     powershell -ExecutionPolicy Bypass -File "C:\Program Files\LibraEdge\instalador\instalar.ps1" `
#         -RaizNodo "C:\Program Files\LibraEdge" -Direccion sistema.contalibra.com.ar
# o, con el archivo de carga (soporte):
#     ... instalar.ps1 -RaizNodo "C:\Program Files\LibraEdge" -Carga C:\ruta\carga-nodo.json
#
# Desde la app de escritorio (ADR-015, "Vincular con el sistema"): la app se
# relanza elevada (UAC) y corre esto con -Direccion y -ArchivoCodigo, un archivo
# del perfil del usuario con el codigo (UAC no pasa el entorno). Se lee con el
# ayudante (--archivo-codigo) y se borra apenas se canjea.
#
# -SinVincular (el instalador con /SINVINCULAR, o sin codigo ni archivo en el
# asistente): solo deja los accesos directos, para vincular despues desde la app.
#
# El codigo de vinculacion (ADR-014) llega en la variable LIBRAEDGE_CODIGO del
# proceso (el .iss la pone antes de llamar a este script) y NUNCA como
# argumento: el transcript anota la linea de comandos. Se canjea por la carga
# antes de tocar nada; la carga queda en <Datos>\carga-nodo.json (solo SYSTEM y
# Administradores) y se borra al terminar bien.
#
# El detalle queda en <RaizNodo>\logs\instalacion.log. Nada de lo que se
# registra ahi es un secreto: los scripts pasan RUTAS de archivos, nunca
# valores, porque el transcript anota la linea de comandos.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizNodo,
    [string]$Carga = "",
    [string]$Direccion = "",
    [string]$ArchivoCodigo = "",
    [switch]$SinVincular,
    [string]$Datos = (Join-Path $env:ProgramData "LibraEdge"),
    [int]$IntervaloSegundos = 60
)

$ErrorActionPreference = "Stop"
# Lanzado desde PowerShell 7 (una consola pwsh, o el runner de GitHub), Windows
# PowerShell 5.1 hereda su PSModulePath y carga los modulos de la 7: chocan y
# Get-Acl no carga ("The member AuditToString is already present", CI
# 2026-10-19). Se usa el de la maquina, que es el de la 5.1.
if ($PSVersionTable.PSEdition -eq "Desktop") {
    $env:PSModulePath = [Environment]::GetEnvironmentVariable("PSModulePath", "Machine")
}
$logs = Join-Path $RaizNodo "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Start-Transcript -Path (Join-Path $logs "instalacion.log") -Append | Out-Null

# --- Sin vincular (ADR-015): los accesos directos y nada mas ----------------------
# El nodo se vincula despues desde la app ("Vincular con el sistema"), que vuelve a
# correr este script elevado con -Direccion y -ArchivoCodigo. No toca servicios.
if ($SinVincular) {
    try {
        if ($Carga -or $Direccion) { throw "-SinVincular va sin codigo ni carga." }
        . (Join-Path $PSScriptRoot "comun.ps1")
        $plan = Leer-Plan -Python (Join-Path $RaizNodo "python\python.exe") `
            -Ayudante (Join-Path $RaizNodo "instalador\libraedge_instalador.py") `
            -Definicion (Join-Path $RaizNodo "instalador\producto.json")
        Write-Host "== Nodo LibraEdge de $($plan.nombre), sin vincular"
        # Si es una actualizacion de un nodo ya vinculado, el nodo.env no se vuelve a armar: se le agregan las
        # variables fijas nuevas (LIBRAAUTH_COOKIE_SECURE, ADR-016) y las "variables" de la definicion del producto
        # (LIBRAEDGE_CARGA_INICIAL, ADR-023) sin tocar lo demas ni su ACL.
        $archivoEnv = Join-Path $RaizNodo "nodo.env"
        if (Test-Path $archivoEnv) {
            $codigo = Invocar -Programa (Join-Path $RaizNodo "python\python.exe") -Argumentos @(
                (Join-Path $RaizNodo "instalador\libraedge_instalador.py"), "completar-entorno", "--entorno", $archivoEnv,
                "--definicion", (Join-Path $RaizNodo "instalador\producto.json"))
            if ($codigo -ne 0) { throw "No se pudo completar $archivoEnv (codigo $codigo)." }
            # Y el .iss paro los servicios para copiar los archivos nuevos (PrepareToInstall): sin esto el local
            # quedaba sin sistema hasta reiniciar la PC. Se migra (el producto nuevo puede traer migraciones) y se
            # vuelven a arrancar, como en una actualizacion con la carga.
            Actualizar-NodoVinculado -RaizNodo $RaizNodo -Plan $plan -ArchivoEnv $archivoEnv
        }
        Crear-AccesosDelProducto -RaizNodo $RaizNodo -Plan $plan
        $texto = "Falta vincular esta PC con el sistema: abrir el acceso directo `"$($plan.nombre)`" y escribir " +
            "la direccion y el codigo que muestra Sucursal sin internet."
        Escribir-SinBom -Ruta (Join-Path $RaizNodo "instalador\acceso.txt") -Texto $texto
        Write-Host $texto
        Write-Host "== Listo (sin vincular)."
        $salida = 0
    } catch {
        Write-Host "ERROR: $($_.Exception.Message)"
        $salida = 1
    } finally {
        Stop-Transcript | Out-Null
    }
    exit $salida
}

# Hasta crear o tocar la base, un error deja la PC como estaba: el .iss paro los
# servicios para copiar archivos y aca se vuelven a arrancar (una reinstalacion
# con una carga equivocada no deja el local sin sistema).
$tocado = $false
$canjeada = ""
try {
    . (Join-Path $PSScriptRoot "comun.ps1")
    $python     = Join-Path $RaizNodo "python\python.exe"
    $ayudante   = Join-Path $RaizNodo "instalador\libraedge_instalador.py"
    $definicion = Join-Path $RaizNodo "instalador\producto.json"

    $plan = Leer-Plan -Python $python -Ayudante $ayudante -Definicion $definicion
    Write-Host "== Nodo LibraEdge de $($plan.nombre)"

    # --- Un nodo por PC (antes de canjear: un codigo gastado en la PC equivocada no vuelve) ---
    # Los servicios, el puerto de PostgreSQL y la carpeta de datos son unos solos.
    # El .iss ya lo controla al arrancar; esto cubre la reparacion a mano.
    $marca = Join-Path $Datos "producto.txt"
    if (Test-Path $marca) {
        $previo = ([System.IO.File]::ReadAllText($marca)).Trim()
        if ($previo -and $previo -ne $plan.producto) {
            throw "Esta PC ya tiene el nodo de $previo. Un nodo por PC."
        }
    }

    # --- 0. El codigo de vinculacion, si vino (ADR-014) ---------------------------
    # La carga canjeada nace con la ACL de SYSTEM y Administradores ANTES de que
    # el ayudante escriba el secreto adentro (como el nodo.env). El codigo lo lee
    # el ayudante de LIBRAEDGE_CODIGO; despues se saca del entorno.
    if ($Direccion) {
        if ($Carga) { throw "O el codigo o el archivo de carga, no los dos." }
        if ($ArchivoCodigo) {
            if (-not (Test-Path $ArchivoCodigo -PathType Leaf)) { throw "No esta el archivo del codigo." }
        } elseif (-not $env:LIBRAEDGE_CODIGO) {
            throw "Falta el codigo de vinculacion (LIBRAEDGE_CODIGO)."
        }
        $canjeada = Join-Path $Datos "carga-nodo.json"
        Proteger-Archivo -Ruta $canjeada
        Write-Host "Canjeando el codigo de vinculacion en $Direccion..."
        $argsCanje = @($ayudante, "canjear", "--definicion", $definicion, "--direccion", $Direccion, "--salida", $canjeada)
        if ($ArchivoCodigo) { $argsCanje += @("--archivo-codigo", $ArchivoCodigo) }
        $codigoCanje = Invocar -Programa $python -Argumentos $argsCanje
        Remove-Item Env:LIBRAEDGE_CODIGO -ErrorAction SilentlyContinue
        if ($ArchivoCodigo) { Remove-Item $ArchivoCodigo -Force -ErrorAction SilentlyContinue }
        if ($codigoCanje -ne 0) {
            Remove-Item $canjeada -Force -ErrorAction SilentlyContinue
            $canjeada = ""
            throw "No se pudo canjear el codigo (el motivo esta arriba). Pedir otro en Sucursal sin internet."
        }
        Test-ArchivoProtegido -Ruta $canjeada -Lanzar | Out-Null
        $Carga = $canjeada
    }
    if (-not $Carga) { throw "Falta la carga del nodo: -Direccion con LIBRAEDGE_CODIGO, o -Carga <archivo>." }
    $Carga = (Resolve-Path $Carga).Path

    # --- 1. La carga, antes de tocar nada --------------------------------------
    # Una carga de otro producto, incompleta o con una URL sin https se rechaza
    # ACA, antes de crear el cluster. El resumen no incluye el secreto.
    $r = Leer-Salida -Programa $python -Argumentos @($ayudante, "validar", "--definicion", $definicion, "--carga", $Carga)
    if ($r.Codigo -ne 0) { throw "La carga del nodo no sirve: el motivo esta arriba." }
    $resumen = $r.Texto | ConvertFrom-Json
    Write-Host ("Carga: nodo {0}, prefijo {1}, central {2}, {3} tablas espejo." -f
        $resumen.node_id, $resumen.prefijo, $resumen.central, $resumen.tablas_espejo)
    foreach ($aviso in @($resumen.avisos)) { if ($aviso) { Write-Warning $aviso } }

    # --- 3. El runtime de Visual C++ ---------------------------------------------
    # El ZIP de PostgreSQL no lo trae y un Windows limpio tampoco: sin el,
    # initdb/psql/pg_ctl/postgres mueren con 0xC0000135 sin escribir una linea
    # (medido 2026-08-30). Se mira la DLL y no el registro de programas.
    $sistema = [Environment]::SystemDirectory
    $faltaRuntime = @("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll") |
        Where-Object { -not (Test-Path (Join-Path $sistema $_)) }
    if ($faltaRuntime) {
        $vc = Join-Path $RaizNodo "herramientas\vc_redist.x64.exe"
        if (-not (Test-Path $vc)) { throw "Falta el runtime de Visual C++ y no esta $vc." }
        Write-Host "Instalando el runtime de Visual C++..."
        $codigo = Invocar -Programa $vc -Argumentos @("/install", "/quiet", "/norestart")
        # 0 instalado, 1638 ya hay uno mas nuevo, 3010 pide reinicio (las DLL ya quedaron).
        if (@(0, 1638, 3010) -notcontains $codigo) { throw "vc_redist termino con codigo $codigo." }
    }

    # --- 4. PostgreSQL ----------------------------------------------------------------
    $tocado = $true
    $bases = @($plan.bases | ForEach-Object { $_.nombre }) -join ","
    & (Join-Path $PSScriptRoot "preparar_postgres.ps1") -RaizPostgres (Join-Path $RaizNodo "postgres") `
        -DirectorioDatos (Join-Path $Datos "datos") -ArchivoClave (Join-Path $Datos "postgres.clave") `
        -Bases $bases

    # --- 5. El nodo -----------------------------------------------------------------
    & (Join-Path $PSScriptRoot "preparar_nodo.ps1") -RaizNodo $RaizNodo -Carga $Carga -Datos $Datos `
        -IntervaloSegundos $IntervaloSegundos

    Escribir-SinBom -Ruta $marca -Texto $plan.producto
    Write-Host ""
    if ($canjeada) {
        # El secreto ya esta en el nodo.env: la carga canjeada no tiene por que quedar.
        Remove-Item $canjeada -Force -ErrorAction SilentlyContinue
        $canjeada = ""
    } else {
        Write-Host "La carga ($Carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
    }
    Write-Host "== Listo."
    $salida = 0
} catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)"
    Write-Host "En: $($_.InvocationInfo.ScriptName):$($_.InvocationInfo.ScriptLineNumber)"
    if ($canjeada -and (Test-Path $canjeada)) {
        Write-Host "El codigo ya se uso: la carga quedo en $canjeada (solo SYSTEM y Administradores)."
        Write-Host "Reintentar con: instalar.ps1 -RaizNodo `"$RaizNodo`" -Carga `"$canjeada`""
    }
    $salida = 1
    if (-not $tocado) {
        foreach ($servicio in @("LibraEdgePostgres", "LibraEdgeProducto", "LibraEdgeNodo", "LibraEdgeDescubrimiento")) {
            $s = Get-Service -Name $servicio -ErrorAction SilentlyContinue
            if ($s -and $s.Status -ne "Running") {
                Write-Host "Se vuelve a arrancar $servicio (no se toco nada)."
                Start-Service -Name $servicio -ErrorAction SilentlyContinue
            }
        }
    }
} finally {
    Stop-Transcript | Out-Null
}
exit $salida
