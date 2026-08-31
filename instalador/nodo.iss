; Instalador del nodo LibraEdge para Windows (Inno Setup 6).
;
; 🔴 SIN COMPILAR NI PROBAR. Se escribió en un entorno sin Inno Setup. Ver el
; README de esta carpeta para la lista de lo que hay que verificar en una
; máquina real antes de dárselo a un cliente.
;
; Compilar con:  ISCC.exe nodo.iss
;
; Espera encontrar, al lado de este archivo:
;   carga\python\        Python embebido (python.org, "Windows embeddable package")
;   carga\postgres\      PostgreSQL 16 (el ZIP binario, NO el instalador de EDB)
;   carga\producto\      El producto ya instalado en el Python embebido
;   carga\herramientas\  nssm.exe
;
; 🔴 Al armar `carga\python\`: el paquete embebido trae un `python3XX._pth` con
; la línea `import site` COMENTADA. Con eso apagado `site-packages` no entra al
; path, y `python -m alembic` falla con "No module named alembic" aunque esté
; instalado justo al lado. Hay que descomentarla al preparar la carga;
; `preparar_nodo.ps1` lo verifica antes de correr las migraciones.

#define Producto      "Restolibra"
#define NombreNodo    "Nodo LibraEdge"
; ⚠️ Mover esto con el tag de LibraEdge. Estuvo en 0.4.1 mientras el paquete iba
; por v0.6.0: un número que nadie mantiene es peor que no tenerlo, porque el
; cliente termina reportando una versión que no es la que tiene.
#define Version       "0.6.0"
#define Editor        "Mariano Cappucci"

[Setup]
; GUID real y fijo: es lo que hace que una reinstalación actualice en vez de
; instalar una segunda copia al lado. No cambiarlo entre versiones.
AppId={{F4887308-2B5A-4D5A-B694-1F85F48CBA4B}
AppName={#NombreNodo} para {#Producto}
AppVersion={#Version}
AppPublisher={#Editor}
DefaultDirName={autopf}\LibraEdge
DefaultGroupName=LibraEdge
DisableProgramGroupPage=yes
OutputBaseFilename=libraedge-nodo-{#Version}
Compression=lzma2/max
SolidCompression=yes
; Registra servicios de Windows y escribe en Archivos de programa.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "carga\python\*";       DestDir: "{app}\python";       Flags: ignoreversion recursesubdirs
Source: "carga\postgres\*";     DestDir: "{app}\postgres";     Flags: ignoreversion recursesubdirs
Source: "carga\producto\*";     DestDir: "{app}";              Flags: ignoreversion recursesubdirs
Source: "carga\herramientas\*"; DestDir: "{app}\herramientas"; Flags: ignoreversion
Source: "preparar_postgres.ps1"; DestDir: "{app}\instalador";  Flags: ignoreversion
Source: "preparar_nodo.ps1";     DestDir: "{app}\instalador";  Flags: ignoreversion
; 🔴 El runtime de Visual C++. NO es opcional y no es una dependencia teorica:
; el ZIP binario de PostgreSQL no lo trae y un Windows limpio tampoco. Sin el,
; initdb/psql/pg_ctl/postgres mueren con 0xC0000135 sin escribir una linea.
; Verificado en una VM con Windows 11 LTSC recien instalado, el 2026-08-30.
; Bajar de https://aka.ms/vs/17/release/vc_redist.x64.exe al armar la carga.
Source: "carga\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Dirs]
; Los datos NO van bajo {app}: la desinstalación no los tiene que tocar. Un
; outbox con operaciones sin sincronizar no existe en ningún otro lado.
Name: "{commonappdata}\LibraEdge\datos"; Permissions: system-full admins-full
Name: "{app}\logs"

[Run]
; PRIMERO el runtime de Visual C++: sin el, el paso siguiente no puede ni
; preguntarle la version a initdb. Los codigos de salida que NO son un fallo:
; 0 = instalado, 1638 = ya hay una version mas nueva, 3010 = pide reinicio
; --el reinicio no hace falta ahora: las DLLs ya quedaron en System32--.
Filename: "{tmp}\vc_redist.x64.exe"; \
  Parameters: "/install /quiet /norestart"; \
  StatusMsg: "Instalando el runtime de Visual C++ (lo necesita PostgreSQL)..."; \
  Flags: waituntilterminated; \
  Check: HaceFaltaVCRuntime

Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\instalador\preparar_postgres.ps1"" -RaizPostgres ""{app}\postgres"" -DirectorioDatos ""{commonappdata}\LibraEdge\datos"" -Password ""{code:ClavePostgres}"""; \
  StatusMsg: "Preparando la base de datos local..."; \
  Flags: runhidden waituntilterminated

Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\instalador\preparar_nodo.ps1"" -RaizNodo ""{app}"" -UrlBase ""{code:UrlBase}"" -UrlCentral ""{code:UrlCentral}"" -NodeId ""{code:NodeId}"" -NodeSecret ""{code:NodeSecret}"" -TablasEspejo ""{code:TablasEspejo}"""; \
  StatusMsg: "Configurando el nodo y sus servicios..."; \
  Flags: runhidden waituntilterminated

[UninstallRun]
; Se detienen y se quitan los servicios, en orden inverso al de creación.
Filename: "{app}\herramientas\nssm.exe"; Parameters: "stop LibraEdgeNodo confirm";     Flags: runhidden; RunOnceId: "PararNodo"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "remove LibraEdgeNodo confirm";   Flags: runhidden; RunOnceId: "QuitarNodo"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "stop LibraEdgeProducto confirm"; Flags: runhidden; RunOnceId: "PararProducto"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "remove LibraEdgeProducto confirm"; Flags: runhidden; RunOnceId: "QuitarProducto"
Filename: "{app}\postgres\bin\pg_ctl.exe"; Parameters: "unregister -N LibraEdgePostgres"; Flags: runhidden; RunOnceId: "QuitarPG"

[Code]
{ Los datos del nodo se piden en una página propia: el secreto lo emite el
  central y se muestra UNA sola vez, así que quien instala lo trae anotado.
  No se puede pedir por API — el instalador no tiene credenciales para eso, y
  dárselas sería poner una llave del central en cada PC de cada cliente. }

var
  PaginaNodo: TInputQueryWizardPage;
  PaginaBase: TInputQueryWizardPage;

{ Corre el vc_redist solo si hace falta. Se mira si la DLL esta en el sistema
  y no el registro de programas instalados: lo que a PostgreSQL le importa es
  poder CARGAR la DLL, no que figure una entrada de desinstalacion. Y son dos
  archivos: vcruntime140_1.dll es la parte de C++ y en algunas maquinas viejas
  esta una y falta la otra.

  Si el chequeo se equivoca y corre de mas, no pasa nada: el instalador de
  Microsoft detecta que ya esta y sale con 1638. Equivocarse para el otro lado
  --saltearlo cuando hacia falta-- deja los binarios de PostgreSQL sin arrancar,
  asi que ante la duda se corre. }
function HaceFaltaVCRuntime: Boolean;
begin
  Result := (not FileExists(ExpandConstant('{sys}\vcruntime140.dll')))
         or (not FileExists(ExpandConstant('{sys}\vcruntime140_1.dll')))
         or (not FileExists(ExpandConstant('{sys}\msvcp140.dll')));
end;

procedure InitializeWizard;
begin
  PaginaNodo := CreateInputQueryPage(wpSelectDir,
    'Identidad del nodo',
    'Los datos que emitió el central al registrar esta sucursal',
    'Correr en el central:  python -m scripts.nodo_offline registrar <id> --sucursal <nombre>' + #13#10 +
    'y copiar acá lo que imprime. El secreto se muestra una sola vez.');
  PaginaNodo.Add('URL del central (ej. https://cliente.restolibra.com.ar):', False);
  PaginaNodo.Add('LIBRAEDGE_NODE_ID:', False);
  PaginaNodo.Add('LIBRAEDGE_NODE_SECRET:', True);
  PaginaNodo.Add('LIBRAEDGE_TABLAS_ESPEJO:', False);

  PaginaBase := CreateInputQueryPage(PaginaNodo.ID,
    'Base de datos local',
    'Una contraseña para el PostgreSQL de esta máquina',
    'Se usa sólo en esta PC: la base escucha únicamente en 127.0.0.1.');
  PaginaBase.Add('Contraseña:', True);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PaginaNodo.ID then
  begin
    { Falla temprano: un nodo a medio configurar sincroniza contra el lugar
      equivocado, o contra ninguno, y se ve igual que uno sano. }
    if (Trim(PaginaNodo.Values[0]) = '') or (Trim(PaginaNodo.Values[1]) = '') or
       (Trim(PaginaNodo.Values[2]) = '') or (Trim(PaginaNodo.Values[3]) = '') then
    begin
      MsgBox('Faltan datos del nodo. Los cuatro salen del comando `registrar` ' +
             'que se corre en el central.', mbError, MB_OK);
      Result := False;
    end;
  end;
  if CurPageID = PaginaBase.ID then
  begin
    if Length(Trim(PaginaBase.Values[0])) < 12 then
    begin
      MsgBox('La contraseña tiene que tener al menos 12 caracteres.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function UrlCentral(Param: String): String;    begin Result := Trim(PaginaNodo.Values[0]); end;
function NodeId(Param: String): String;        begin Result := Trim(PaginaNodo.Values[1]); end;
function NodeSecret(Param: String): String;    begin Result := Trim(PaginaNodo.Values[2]); end;
function TablasEspejo(Param: String): String;  begin Result := Trim(PaginaNodo.Values[3]); end;
function ClavePostgres(Param: String): String; begin Result := PaginaBase.Values[0]; end;

function UrlBase(Param: String): String;
begin
  { El puerto no es el 5432 por defecto: la PC del cliente puede tener ya un
    PostgreSQL de otra cosa, y pisarlo sería la peor forma de empezar. }
  Result := 'postgresql://postgres:' + ClavePostgres('') + '@127.0.0.1:55432/restolibra';
end;
