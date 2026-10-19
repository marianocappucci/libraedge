# Configura el nodo despues de que Inno Setup copio los archivos. Es lo unico
# que el .iss ejecuta (ADR-010): el instalador revisa su codigo de salida y lo
# devuelve como el suyo, asi que una instalacion desatendida que falla no
# termina con 0.
#
# Se puede volver a correr a mano para reparar (todo es idempotente):
#     powershell -ExecutionPolicy Bypass -File "C:\Program Files\LibraEdge\instalador\instalar.ps1" `
#         -RaizNodo "C:\Program Files\LibraEdge" -Carga C:\ruta\carga-nodo.json
#
# El detalle queda en <RaizNodo>\logs\instalacion.log. Nada de lo que se
# registra ahi es un secreto: los scripts pasan RUTAS de archivos, nunca
# valores, porque el transcript anota la linea de comandos.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RaizNodo,
    [Parameter(Mandatory = $true)][string]$Carga,
    [string]$Datos = (Join-Path $env:ProgramData "LibraEdge"),
    [int]$IntervaloSegundos = 60
)

$ErrorActionPreference = "Stop"
$logs = Join-Path $RaizNodo "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Start-Transcript -Path (Join-Path $logs "instalacion.log") -Append | Out-Null

# Hasta crear o tocar la base, un error deja la PC como estaba: el .iss paro los
# servicios para copiar archivos y aca se vuelven a arrancar (una reinstalacion
# con una carga equivocada no deja el local sin sistema).
$tocado = $false
try {
    . (Join-Path $PSScriptRoot "comun.ps1")
    $python     = Join-Path $RaizNodo "python\python.exe"
    $ayudante   = Join-Path $RaizNodo "instalador\libraedge_instalador.py"
    $definicion = Join-Path $RaizNodo "instalador\producto.json"
    $Carga      = (Resolve-Path $Carga).Path

    $plan = Leer-Plan -Python $python -Ayudante $ayudante -Definicion $definicion
    Write-Host "== Nodo LibraEdge de $($plan.nombre)"

    # --- 1. La carga, antes de tocar nada --------------------------------------
    # Una carga de otro producto, incompleta o con una URL sin https se rechaza
    # ACA, antes de crear el cluster. El resumen no incluye el secreto.
    $r = Leer-Salida -Programa $python -Argumentos @($ayudante, "validar", "--definicion", $definicion, "--carga", $Carga)
    if ($r.Codigo -ne 0) { throw "La carga del nodo no sirve: el motivo esta arriba." }
    $resumen = $r.Texto | ConvertFrom-Json
    Write-Host ("Carga: nodo {0}, prefijo {1}, central {2}, {3} tablas espejo." -f
        $resumen.node_id, $resumen.prefijo, $resumen.central, $resumen.tablas_espejo)
    foreach ($aviso in @($resumen.avisos)) { if ($aviso) { Write-Warning $aviso } }

    # --- 2. Un nodo por PC ---------------------------------------------------------
    # Los servicios, el puerto de PostgreSQL y la carpeta de datos son unos solos.
    # El .iss ya lo controla al arrancar; esto cubre la reparacion a mano.
    $marca = Join-Path $Datos "producto.txt"
    if (Test-Path $marca) {
        $previo = ([System.IO.File]::ReadAllText($marca)).Trim()
        if ($previo -and $previo -ne $plan.producto) {
            throw "Esta PC ya tiene el nodo de $previo. Un nodo por PC."
        }
    }

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
    Write-Host "La carga ($Carga) tiene el secreto del nodo: borrarla de esta PC y de donde se haya copiado."
    Write-Host "== Listo."
    $salida = 0
} catch {
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)"
    Write-Host "En: $($_.InvocationInfo.ScriptName):$($_.InvocationInfo.ScriptLineNumber)"
    $salida = 1
    if (-not $tocado) {
        foreach ($servicio in @("LibraEdgeProducto", "LibraEdgeNodo")) {
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
