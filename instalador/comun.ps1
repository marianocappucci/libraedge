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
