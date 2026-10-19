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
    # La clave del superusuario se lee de su archivo (SYSTEM y Administradores):
    # antes iba como -Password, o sea en texto plano en la definicion de la tarea.
    [Parameter(Mandatory = $true)][string]$ArchivoClave,
    # Las bases del producto (texto con comas: `-File` no pasa arreglos) y la del
    # outbox, que es la que se cuenta para verificar.
    [Parameter(Mandatory = $true)][string]$Bases,
    [Parameter(Mandatory = $true)][string]$BaseNodo,
    [int]$Puerto = 55432,
    [string]$Destino = (Join-Path $env:ProgramData "LibraEdge\respaldos"),
    # Segunda copia, en otro dispositivo. Vacío = no se hace, y se avisa.
    [string]$DestinoSecundario = "",
    # Cuántos se conservan POR BASE. A un respaldo por hora, 48 son dos días.
    [int]$Conservar = 48
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "comun.ps1")

$pgdump    = Join-Path $RaizPostgres "bin\pg_dump.exe"
$pgrestore = Join-Path $RaizPostgres "bin\pg_restore.exe"
$psql      = Join-Path $RaizPostgres "bin\psql.exe"
foreach ($exe in @($pgdump, $pgrestore, $psql)) {
    if (-not (Test-Path $exe)) { throw "No esta $exe." }
}
if (-not (Test-Path $ArchivoClave)) { throw "No esta la clave de PostgreSQL en $ArchivoClave." }
$listaBases = @($Bases -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($listaBases -notcontains $BaseNodo) { throw "La base del nodo ($BaseNodo) no esta entre las bases." }

# Los volcados tienen la base entera (usuarios con sus hashes incluidos).
Proteger-Carpeta -Ruta $Destino
$env:PGPASSWORD = ([System.IO.File]::ReadAllText($ArchivoClave)).Trim()
$sello = Get-Date -Format "yyyyMMdd-HHmmss"

# El `$ErrorActionPreference` baja para todo lo que hable con los binarios de
# PostgreSQL: con `Stop`, lo que un programa nativo escribe en stderr se
# convierte en error terminante, y `pg_dump` escribe avisos ahí. Es la misma
# trampa que ya se comió el bucle de espera de `preparar_postgres.ps1`.
$preferenciaPrevia = $ErrorActionPreference
$ErrorActionPreference = 'Continue'

function Consultar($sql, $base) {
    (& $psql -h 127.0.0.1 -p $Puerto -U postgres -d $base -tAc $sql 2>&1 | Out-String).Trim()
}

# Lo que se compara entre la base viva y el respaldo restaurado. En la base del
# nodo, lo que importa: lo que el central todavia no sabe (`sync_outbox` sin
# confirmar) y las ventas, si la tabla existe. En todas, cuantas tablas hay.
function Medir($base, $esNodo) {
    $medida = [ordered]@{
        tablas = Consultar "SELECT count(1) FROM information_schema.tables WHERE table_schema = 'public'" $base
    }
    if ($esNodo) {
        $medida.sin_confirmar = Consultar "SELECT count(1) FROM sync_outbox WHERE status <> 'acknowledged'" $base
        if ((Consultar "SELECT to_regclass('public.sales') IS NOT NULL" $base) -eq "t") {
            $medida.ventas = Consultar "SELECT count(1) FROM sales" $base
        }
    }
    return $medida
}

function Texto($medida) { ($medida.Keys | ForEach-Object { "$_=$($medida[$_])" }) -join ", " }

$fallos = 0
try {
    foreach ($base in $listaBases) {
        $esNodo  = ($base -eq $BaseNodo)
        $archivo = Join-Path $Destino "nodo-$base-$sello.dump"
        try {
            $vivo = Medir $base $esNodo
            Write-Host "[$base] en la base: $(Texto $vivo)."

            & $pgdump -h 127.0.0.1 -p $Puerto -U postgres -d $base -Fc -f $archivo
            if ($LASTEXITCODE -ne 0) { throw "pg_dump fallo con codigo $LASTEXITCODE" }
            $tam = (Get-Item $archivo).Length
            Write-Host ("[{0}] escrito: {1} ({2:N1} MB)" -f $base, (Split-Path $archivo -Leaf), ($tam / 1MB))
            if ($tam -lt 1024) { throw "El respaldo pesa $tam bytes: no puede estar bien." }

            # --- LA VERIFICACION: se ABRE el respaldo, no se confia en el [OK] --
            # `pg_dump` puede salir con 0 y dejar un archivo que no restaura. Lo
            # unico que lo distingue de uno bueno es RESTAURARLO y contar. Se
            # restaura en una base descartable de la misma instancia.
            $verificacion = "libraedge_verificar_respaldo"
            Consultar "DROP DATABASE IF EXISTS $verificacion" "postgres" | Out-Null
            Consultar "CREATE DATABASE $verificacion" "postgres" | Out-Null
            try {
                & $pgrestore -h 127.0.0.1 -p $Puerto -U postgres -d $verificacion --no-owner --no-privileges $archivo 2>&1 |
                    Out-Null
                # pg_restore avisa por cosas inofensivas (owners que no existen), asi
                # que su codigo de salida no alcanza: lo que decide son las medidas.
                $copia = Medir $verificacion $esNodo
                Write-Host "[$base] en el respaldo: $(Texto $copia)."
                if ((Texto $copia) -ne (Texto $vivo)) {
                    throw ("El respaldo NO coincide con la base ($(Texto $copia) contra " +
                           "$(Texto $vivo)). Se conserva el archivo para mirarlo, pero no cuenta.")
                }
                Write-Host "[$base] verificado: el respaldo restaura y las medidas coinciden."
            } finally {
                Consultar "DROP DATABASE IF EXISTS $verificacion" "postgres" | Out-Null
            }

            # --- La segunda copia ---------------------------------------------
            if ($DestinoSecundario) {
                if (Test-Path $DestinoSecundario) {
                    Copy-Item $archivo -Destination $DestinoSecundario -Force
                    $copiaSecundaria = Join-Path $DestinoSecundario (Split-Path $archivo -Leaf)
                    # Una copia a un pendrive que se desconecto a mitad deja un
                    # archivo del tamano correcto y el contenido cortado.
                    $a = (Get-FileHash $archivo -Algorithm SHA256).Hash
                    $b = (Get-FileHash $copiaSecundaria -Algorithm SHA256).Hash
                    if ($a -ne $b) { throw "La copia en $DestinoSecundario no coincide con el original." }
                    Write-Host "[$base] segunda copia en $DestinoSecundario, con el mismo SHA256."
                } else {
                    Write-Warning ("No existe ${DestinoSecundario}: la segunda copia NO se hizo. " +
                                   "Si es un pendrive, esta desconectado.")
                }
            }

            # --- Rotacion: DESPUES de verificar el nuevo --------------------------
            $viejos = Get-ChildItem $Destino -Filter "nodo-$base-*.dump" |
                      Sort-Object LastWriteTime -Descending | Select-Object -Skip $Conservar
            foreach ($v in $viejos) { Remove-Item $v.FullName -Force }
            Write-Host ("[{0}] quedan {1} respaldos en {2}." -f $base,
                @(Get-ChildItem $Destino -Filter "nodo-$base-*.dump").Count, $Destino)
        } catch {
            # Una base que falla no frena a las otras, pero el script sale con 1.
            Write-Warning "[$base] $($_.Exception.Message)"
            $fallos++
        }
    }
    if (-not $DestinoSecundario) {
        Write-Warning ("Sin -DestinoSecundario: el respaldo queda en el MISMO disco " +
                       "que la base. Protege de una base corrupta, no de un disco roto.")
    }
} finally {
    $ErrorActionPreference = $preferenciaPrevia
    $env:PGPASSWORD = $null
}
if ($fallos) { exit 1 }
