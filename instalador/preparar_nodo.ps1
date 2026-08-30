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
    [int]$IntervaloSegundos = 60
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
if (Test-Path $archivoEnv) { Remove-Item $archivoEnv -Force }
New-Item -ItemType File -Path $archivoEnv | Out-Null
$acl = Get-Acl $archivoEnv
$acl.SetAccessRuleProtection($true, $false)   # corta la herencia
foreach ($cuenta in @("SYSTEM", "Administrators")) {
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $cuenta, "FullControl", "Allow")))
}
Set-Acl -Path $archivoEnv -AclObject $acl

@"
LIBRAEDGE_NODE_ID=$NodeId
LIBRAEDGE_NODE_SECRET=$NodeSecret
LIBRAEDGE_CENTRAL_URL=$UrlCentral
LIBRAEDGE_DATABASE_URL=$UrlBase
LIBRAEDGE_TABLAS_ESPEJO=$TablasEspejo
LIBRAEDGE_ESTADO=$estado
RESTOLIBRA_DATABASE_URL=$UrlBase
TZ=America/Argentina/Buenos_Aires
"@ | Set-Content -Path $archivoEnv -Encoding utf8

# --- Las migraciones ------------------------------------------------------
# El nodo corre LAS MISMAS que el central: es lo que hace que el espejo pueda
# escribir fila por fila sin traducir nada. Si divergen, el aplicador del nodo
# falla por una columna que no existe.
Push-Location $RaizNodo
try {
    $env:RESTOLIBRA_DATABASE_URL = $UrlBase
    & $python -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Las migraciones fallaron con código $LASTEXITCODE" }
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
    & $nssm set $Nombre AppRotateFiles 1
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

Write-Host ""
Write-Host "Instalado. El producto escucha en http://<esta-pc>:$PuertoProducto"
Write-Host "Estado del nodo:  $python -m libraedge estado"
Write-Host ""
Write-Host "ATENCION: Verificar A MANO, porque el instalador no lo puede probar solo:"
Write-Host "   1. Reiniciar la PC y confirmar que los dos servicios levantan."
Write-Host '   2. select now() en la base: el offset tiene que ser -03.'
Write-Host "   3. Desconectar internet, cobrar, reconectar, y ver que la venta suba."
