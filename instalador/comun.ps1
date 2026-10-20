# Funciones que comparten los scripts del instalador. Se carga con:
#     . (Join-Path $PSScriptRoot "comun.ps1")
#
# Regla de esta carpeta: los TEXTOS de los scripts van sin acentos ni emojis.
# Windows PowerShell 5.1 lee un .ps1 sin BOM como ANSI, y un caracter de varios
# bytes adentro de una cadena puede terminarla antes de tiempo (paso con un
# emoji en 2026-08-30). En los comentarios no importa.

# Los SIDs conocidos no dependen del idioma: "Administrators" no existe en un
# Windows en espanol (medido 2026-08-30, IdentityNotMappedException).
$script:SidsPermitidos = @("S-1-5-18", "S-1-5-32-544")   # SYSTEM, Administradores

function Proteger-Archivo {
    <#
    Crea el archivo si no existe (vacio) y deja su ACL en SYSTEM y
    Administradores, sin herencia. Si ya existe NO lo vacia: en una
    reinstalacion el nodo.env viejo tiene el SECRET_KEY que hay que conservar.
    La ACL se verifica despues de aplicarla: Set-Acl puede fallar a medias.
    #>
    param([Parameter(Mandatory = $true)][string]$Ruta)

    if (-not (Test-Path $Ruta)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $Ruta -Parent) | Out-Null
        New-Item -ItemType File -Path $Ruta | Out-Null
    }
    $acl = Get-Acl $Ruta
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($regla in @($acl.Access)) { [void]$acl.RemoveAccessRule($regla) }
    foreach ($sid in $script:SidsPermitidos) {
        $cuenta = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
            $cuenta, "FullControl", "Allow")))
    }
    Set-Acl -Path $Ruta -AclObject $acl
    Test-ArchivoProtegido -Ruta $Ruta -Lanzar | Out-Null
}

function Test-ArchivoProtegido {
    # Devuelve $true si la ACL es exactamente SYSTEM + Administradores.
    param([Parameter(Mandatory = $true)][string]$Ruta, [switch]$Lanzar)

    $quedaron = @((Get-Acl $Ruta).Access | ForEach-Object {
        $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value })
    $faltan = @($script:SidsPermitidos | Where-Object { $quedaron -notcontains $_ })
    $sobran = @($quedaron | Where-Object { $script:SidsPermitidos -notcontains $_ })
    $bien = ($faltan.Count -eq 0 -and $sobran.Count -eq 0)
    if (-not $bien -and $Lanzar) {
        throw "La ACL de $Ruta no quedo: faltan [$($faltan -join ', ')], sobran [$($sobran -join ', ')]."
    }
    return $bien
}

function Invocar {
    <#
    Corre un programa nativo y devuelve su codigo de salida, con su salida en
    pantalla (y por lo tanto en el Start-Transcript).

    Con $ErrorActionPreference = 'Stop', PowerShell convierte lo que un
    programa escribe en stderr en un error terminante, y `*> $null` no lo
    evita. Aca se baja a Continue solo durante la llamada (medido en la VM el
    2026-08-30: un guard que no podia hablar).

    No pasar secretos como argumentos: el instalador pasa rutas de archivos.
    #>
    param([Parameter(Mandatory = $true)][string]$Programa, [string[]]$Argumentos = @())

    $previa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Programa @Argumentos 2>&1 | ForEach-Object { Write-Host "    $_" }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previa
    }
}

function Leer-Salida {
    # Como Invocar, pero devuelve lo que el programa escribe en stdout (stderr va a pantalla).
    param([Parameter(Mandatory = $true)][string]$Programa, [string[]]$Argumentos = @())

    $previa = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $todo = @(& $Programa @Argumentos 2>&1)
        $codigo = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previa
    }
    $esError = { $_ -is [System.Management.Automation.ErrorRecord] }
    $todo | Where-Object $esError | ForEach-Object { Write-Host "    $_" }
    $salida = $todo | Where-Object { -not ($_ -is [System.Management.Automation.ErrorRecord]) }
    return [pscustomobject]@{ Codigo = $codigo; Texto = (($salida | Out-String).Trim()) }
}

function Leer-Entorno {
    # El nodo.env como tabla. No imprime valores: tiene secretos.
    param([Parameter(Mandatory = $true)][string]$Ruta)

    $tabla = [ordered]@{}
    foreach ($linea in (Get-Content -Path $Ruta -Encoding UTF8)) {
        $linea = $linea.Trim()
        if (-not $linea -or $linea.StartsWith("#")) { continue }
        $i = $linea.IndexOf("=")
        if ($i -lt 1) { continue }
        $tabla[$linea.Substring(0, $i).Trim()] = $linea.Substring($i + 1)
    }
    return $tabla
}

function Cargar-EntornoEnProceso {
    # Pone las variables del nodo.env en ESTE proceso (para migrar y sincronizar).
    param([Parameter(Mandatory = $true)][string]$Ruta)

    $tabla = Leer-Entorno -Ruta $Ruta
    foreach ($nombre in $tabla.Keys) {
        Set-Item -Path ("Env:{0}" -f $nombre) -Value $tabla[$nombre]
    }
    return $tabla.Count
}

function Leer-Plan {
    # La definicion del producto, validada por libraedge_instalador.py.
    param([Parameter(Mandatory = $true)][string]$Python, [Parameter(Mandatory = $true)][string]$Ayudante,
          [Parameter(Mandatory = $true)][string]$Definicion)

    $r = Leer-Salida -Programa $Python -Argumentos @($Ayudante, "plan", "--definicion", $Definicion)
    if ($r.Codigo -ne 0 -or -not $r.Texto) {
        throw "La definicion del producto ($Definicion) no es valida (codigo $($r.Codigo))."
    }
    return ($r.Texto | ConvertFrom-Json)
}

function Escribir-SinBom {
    param([Parameter(Mandatory = $true)][string]$Ruta, [Parameter(Mandatory = $true)][string]$Texto)
    [System.IO.File]::WriteAllText($Ruta, $Texto, (New-Object System.Text.UTF8Encoding($false)))
}

function Proteger-Carpeta {
    # Deja la carpeta (y lo que se cree adentro) solo para SYSTEM y Administradores.
    # Para DATA_DIR del producto y los respaldos: ahi caen volcados de la base,
    # que bajo ProgramData heredarian lectura para cualquier usuario de la PC.
    param([Parameter(Mandatory = $true)][string]$Ruta)

    New-Item -ItemType Directory -Force -Path $Ruta | Out-Null
    $codigo = Invocar -Programa "icacls.exe" -Argumentos @(
        $Ruta, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    if ($codigo -ne 0) { throw "No se pudo restringir el acceso a $Ruta (icacls $codigo)." }
}

# --- Los accesos directos del producto (ADR-013 y ADR-015) ---------------------------
# En esta PC se entra SIEMPRE por la copia local, aunque haya internet: el nodo
# sincroniza por detras. Los crea preparar_nodo.ps1 al configurar y instalar.ps1
# -SinVincular (para vincular despues desde la app); los borra el desinstalador
# ([UninstallDelete] del .iss, con los mismos nombres).
#
# - "<Producto>" (escritorio publico y menu Inicio comun): la app de escritorio,
#   {app}\escritorio\<Producto>.exe (ADR-015), si esta y la PC tiene WebView2.
#   Si no, Edge en modo aplicacion con http://localhost:<puerto>/ como antes
#   (ADR-013), y sin Edge la URL en el navegador por defecto.
# - "<Producto> (navegador)" (solo en el menu Inicio, y solo si el principal es la
#   app): el respaldo, Edge en modo aplicacion, por si la app no arranca.

function Buscar-Edge {
    # Edge viene con Windows 10 y 11. Primero el registro (App Paths), despues
    # las rutas conocidas.
    foreach ($clave in @("HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
                         "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe")) {
        $valor = (Get-ItemProperty -Path $clave -ErrorAction SilentlyContinue).'(default)'
        if ($valor -and (Test-Path $valor)) { return $valor }
    }
    foreach ($ruta in @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                        "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
        if ($ruta -and (Test-Path $ruta)) { return $ruta }
    }
    return $null
}

function Test-WebView2 {
    # El runtime de WebView2 (Evergreen) que usa la app: viene con Windows 11 y con
    # las actualizaciones de Edge en Windows 10. Instalado para la maquina (HKLM) o
    # para el usuario (HKCU); "pv" es la version, 0.0.0.0 si se desinstalo.
    $id = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    foreach ($clave in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$id",
                         "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id",
                         "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id")) {
        $pv = (Get-ItemProperty -Path $clave -ErrorAction SilentlyContinue).pv
        if ($pv -and $pv -ne "0.0.0.0") { return $true }
    }
    return $false
}

function Crear-AccesosDelProducto {
    param([Parameter(Mandatory = $true)][string]$RaizNodo, [Parameter(Mandatory = $true)]$Plan)

    $urlLocal = "http://localhost:$($Plan.puerto)/"
    $app = Join-Path $RaizNodo "escritorio\$($Plan.nombre).exe"
    $icono = Join-Path $RaizNodo "producto.ico"
    $edge = Buscar-Edge
    $conApp = (Test-Path $app) -and (Test-WebView2)
    if ((Test-Path $app) -and -not $conApp) {
        Write-Warning "Esta PC no tiene el runtime de WebView2: el acceso directo abre $urlLocal con Edge en lugar de la app."
    }
    $shell = New-Object -ComObject WScript.Shell
    function Guardar-Acceso([string]$Ruta, [bool]$ConLaApp) {
        $lnk = $shell.CreateShortcut($Ruta)
        if ($ConLaApp) {
            $lnk.TargetPath = $app
            $lnk.Arguments = ""
            $lnk.WorkingDirectory = Split-Path $app -Parent
        } elseif ($edge) {
            $lnk.TargetPath = $edge
            $lnk.Arguments = "--app=$urlLocal"
        } else {
            $lnk.TargetPath = Join-Path $env:SystemRoot "explorer.exe"
            $lnk.Arguments = $urlLocal
        }
        if (Test-Path $icono) { $lnk.IconLocation = "$icono,0" }
        $lnk.Description = "Abre $($Plan.nombre) desde la copia de este local"
        $lnk.Save()
        Write-Host "Acceso directo: $Ruta -> $($lnk.TargetPath) $($lnk.Arguments)"
    }
    foreach ($carpeta in @("CommonDesktopDirectory", "CommonPrograms")) {
        Guardar-Acceso -Ruta (Join-Path ([Environment]::GetFolderPath($carpeta)) "$($Plan.nombre).lnk") -ConLaApp $conApp
    }
    $respaldo = Join-Path ([Environment]::GetFolderPath("CommonPrograms")) "$($Plan.nombre) (navegador).lnk"
    if ($conApp) {
        Guardar-Acceso -Ruta $respaldo -ConLaApp $false
    } elseif (Test-Path $respaldo) {
        Remove-Item $respaldo -Force
    }
    if (-not $edge -and -not $conApp) {
        Write-Warning "No se encontro Edge: el acceso directo abre $urlLocal en el navegador por defecto."
    }
}
