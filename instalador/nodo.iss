; Instalador del nodo LibraEdge para Windows (Inno Setup 6.3 o superior).
;
; UNO para todos los productos (ADR-010). Se compila por producto:
;
;     ISCC.exe /DProducto=ventalibra nodo.iss
;     ISCC.exe /DProducto=restolibra /DVersion=0.10.0 nodo.iss
;
; Lo propio del producto esta en productos\<producto>.json (bases, variables,
; modulo ASGI, migraciones, puerto) y lo lee instalar.ps1 al instalar. Aca solo
; hace falta el nombre visible y el AppId, en la tabla de abajo: un test
; (tests/test_instalador.py) verifica que coincida con los .json.
;
; Espera, al lado de este archivo (lo arma armar_paquete.sh, ver README):
;   paquete\<producto>\python\     Python embebido con el producto y sus dependencias
;   paquete\<producto>\producto\   app\, migrations\, alembic.ini y frontend\dist
;   paquete\postgres\              PostgreSQL 16 (el ZIP binario, NO el instalador de EDB)
;   paquete\herramientas\          nssm.exe y vc_redist.x64.exe
;
; Instalacion desatendida (la carga la emite el central, ver README):
;     libraedge-nodo-ventalibra-0.10.0.exe /VERYSILENT /SUPPRESSMSGBOXES /CARGA=C:\ruta\carga-nodo.json /LOG=C:\ruta\inno.log
;
; Codigos de salida: 0 bien; 1 no arranco (falta /CARGA, u otro producto ya
; instalado); 20 los archivos se copiaron pero la configuracion del nodo fallo
; (el detalle esta en {app}\logs\instalacion.log). Los demas son los de Inno.
;
; Los textos van SIN acentos: sin BOM, ISCC lee este archivo con la pagina de
; codigos del sistema.

#ifndef Producto
  #error Falta el producto: ISCC /DProducto=restolibra|ventalibra|contalibra nodo.iss
#endif

#if Producto == "restolibra"
  #define NombreProducto "RestoLibra"
  #define AppIdProducto  "F4887308-2B5A-4D5A-B694-1F85F48CBA4B"
#elif Producto == "ventalibra"
  #define NombreProducto "VentaLibra"
  #define AppIdProducto  "ED40FA78-DE81-4F77-8006-2930D4ADBCAB"
#elif Producto == "contalibra"
  #define NombreProducto "ContaLibra"
  #define AppIdProducto  "4BE6AB1D-4F2C-4255-9D16-C119C22D1DFC"
#else
  #error Producto desconocido: agregar productos\<producto>.json y su fila en esta tabla
#endif

; La version del instalador es la del tag de LibraEdge que lo construye.
#ifndef Version
  #define Version "0.10.0"
#endif
#define NombreNodo "Nodo LibraEdge"
#define Editor     "Mariano Cappucci"

[Setup]
; El AppId es por producto y fijo: es lo que hace que una reinstalacion
; actualice en vez de instalar una segunda copia. El de RestoLibra es el de
; siempre (F4887308-...).
AppId={{{#AppIdProducto}}
AppName={#NombreNodo} para {#NombreProducto}
AppVersion={#Version}
AppPublisher={#Editor}
DefaultDirName={autopf}\LibraEdge
DefaultGroupName=LibraEdge
DisableProgramGroupPage=yes
OutputDir=salida
OutputBaseFilename=libraedge-nodo-{#Producto}-{#Version}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; Lo que siga usando archivos del nodo despues de parar los servicios (la
; bandeja, un python suelto) se cierra: en silencio, no poder cerrarlo es Abort.
CloseApplications=force

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "paquete\{#Producto}\python\*";   DestDir: "{app}\python";       Flags: ignoreversion recursesubdirs createallsubdirs
Source: "paquete\{#Producto}\producto\*"; DestDir: "{app}";              Flags: ignoreversion recursesubdirs createallsubdirs
Source: "paquete\postgres\*";             DestDir: "{app}\postgres";     Flags: ignoreversion recursesubdirs createallsubdirs
Source: "paquete\herramientas\nssm.exe";  DestDir: "{app}\herramientas"; Flags: ignoreversion
; El runtime de Visual C++: el ZIP de PostgreSQL no lo trae y un Windows limpio
; tampoco (0xC0000135, medido 2026-08-30). Lo instala instalar.ps1 si falta;
; queda en {app} para poder reparar sin el instalador.
Source: "paquete\herramientas\vc_redist.x64.exe"; DestDir: "{app}\herramientas"; Flags: ignoreversion
Source: "productos\{#Producto}.json";     DestDir: "{app}\instalador"; DestName: "producto.json"; Flags: ignoreversion
; El ayudante en Python: libraedge/instalador.py tal cual, solo biblioteca estandar.
Source: "..\libraedge\instalador.py";     DestDir: "{app}\instalador"; DestName: "libraedge_instalador.py"; Flags: ignoreversion
Source: "comun.ps1";             DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "instalar.ps1";          DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "preparar_postgres.ps1"; DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "preparar_nodo.ps1";     DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "respaldo.ps1";          DestDir: "{app}\instalador"; Flags: ignoreversion
Source: "verificar_nodo.ps1";    DestDir: "{app}\instalador"; Flags: ignoreversion

[Dirs]
; Los datos NO van bajo {app}: la desinstalacion no los tiene que tocar. Un
; outbox con operaciones sin sincronizar no existe en ningun otro lado.
Name: "{commonappdata}\LibraEdge\datos"; Permissions: system-full admins-full; Flags: uninsneveruninstall
Name: "{app}\logs"

[UninstallRun]
; Se detienen y se quitan los servicios, en orden inverso al de creacion.
Filename: "{app}\herramientas\nssm.exe"; Parameters: "stop LibraEdgeNodo confirm";       Flags: runhidden; RunOnceId: "PararNodo"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "remove LibraEdgeNodo confirm";     Flags: runhidden; RunOnceId: "QuitarNodo"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "stop LibraEdgeProducto confirm";   Flags: runhidden; RunOnceId: "PararProducto"
Filename: "{app}\herramientas\nssm.exe"; Parameters: "remove LibraEdgeProducto confirm"; Flags: runhidden; RunOnceId: "QuitarProducto"
; PostgreSQL se DETIENE antes de desregistrarlo: sin el stop, pg_ctl unregister
; falla con 1072 (ERROR_SERVICE_MARKED_FOR_DELETE) y el servicio queda
; registrado apuntando a binarios borrados (medido 2026-08-31). `net stop` y no
; `sc stop`: net espera a que termine de parar.
Filename: "{sys}\net.exe"; Parameters: "stop LibraEdgePostgres /y"; Flags: runhidden; RunOnceId: "PararPG"
Filename: "{app}\postgres\bin\pg_ctl.exe"; Parameters: "unregister -N LibraEdgePostgres"; Flags: runhidden; RunOnceId: "QuitarPG"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN LibraEdgeRespaldo /F"; Flags: runhidden; RunOnceId: "QuitarRespaldo"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LibraEdge producto"""; Flags: runhidden; RunOnceId: "QuitarFirewall"

[UninstallDelete]
; El secreto del nodo NO puede quedar en el disco de una PC que se da de baja:
; nodo.env lo escribe instalar.ps1, asi que el desinstalador no sabe de el.
Type: files; Name: "{app}\nodo.env"
Type: files; Name: "{app}\estado.json"
Type: files; Name: "{commonstartup}\LibraEdge - estado del nodo.lnk"
Type: filesandordirs; Name: "{app}\logs"
; Los __pycache__ los genera Python al importar: sin esto la carpeta queda.
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\migrations"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\frontend"
Type: filesandordirs; Name: "{app}\instalador"
Type: filesandordirs; Name: "{app}\python"
; Quedan a proposito, en {commonappdata}\LibraEdge: datos\ (el cluster),
; postgres.clave (sin ella esos datos no se abren), app\ (config.json),
; respaldos\ y producto.txt (un nodo por PC).

[Code]
{ La carga del nodo es un ARCHIVO (carga-nodo.json) que emite el central con
  libraedge-central nodo registrar ... --carga. Se elige en el asistente o se
  pasa con /CARGA= en una instalacion desatendida.

  Nunca se pasan secretos por la linea de comandos: Inno Setup escribe su linea
  de comandos en el log (/LOG), y los scripts de PowerShell la suya en el
  transcript. Viaja la RUTA de la carga; el secreto lo lee el ayudante.

  (Ojo con las llaves en estos comentarios: en Pascal Script la llave ES el
  comentario y una de mas lo cierra antes de tiempo.) }
var
  PaginaCarga: TInputFileWizardPage;
  vCarga: String;
  CodigoDeSalida: Integer;

function ArchivoDeProducto: String;
begin
  Result := ExpandConstant('{commonappdata}\LibraEdge\producto.txt');
end;

function InitializeSetup: Boolean;
var
  Previo: AnsiString;
  Instalado: String;
begin
  Result := True;
  CodigoDeSalida := 0;
  vCarga := Trim(ExpandConstant('{param:CARGA|}'));
  if vCarga <> '' then
    vCarga := ExpandFileName(vCarga);

  { Un nodo por PC: los servicios, el puerto de PostgreSQL y la carpeta de
    datos son unos solos. }
  if LoadStringFromFile(ArchivoDeProducto, Previo) then
  begin
    Instalado := Trim(String(Previo));
    if (Instalado <> '') and (CompareText(Instalado, '{#Producto}') <> 0) then
    begin
      Log('Esta PC ya tiene el nodo de ' + Instalado + '.');
      SuppressibleMsgBox('Esta PC ya tiene un nodo LibraEdge de ' + Instalado +
        '. Va uno solo por PC: desinstalarlo y retirar la carpeta de datos antes de instalar {#NombreProducto}.',
        mbError, MB_OK, IDOK);
      Result := False;
      Exit;
    end;
  end;

  { En silencio no hay a quien preguntarle: sin carga se aborta antes de tocar
    el disco. }
  if WizardSilent and ((vCarga = '') or (not FileExists(vCarga))) then
  begin
    Log('Instalacion desatendida sin /CARGA o con un archivo que no existe.');
    SuppressibleMsgBox('Instalacion desatendida: falta /CARGA=<ruta de carga-nodo.json> o el archivo no existe.',
      mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  PaginaCarga := CreateInputFilePage(wpSelectDir,
    'Carga del nodo',
    'El archivo que emitio el central al registrar este nodo',
    'En el central: libraedge-central nodo registrar <node_id> --sucursal <sucursal> ' +
    '--carga carga-nodo.json --central-url https://... ' + #13#10 +
    'Elegir ese archivo. Tiene el secreto del nodo: borrarlo despues de instalar.');
  PaginaCarga.Add('Archivo de carga:', 'Carga del nodo (*.json)|*.json|Todos los archivos|*.*', '.json');
  PaginaCarga.Values[0] := vCarga;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = PaginaCarga.ID then
  begin
    vCarga := Trim(PaginaCarga.Values[0]);
    if (vCarga = '') or (not FileExists(vCarga)) then
    begin
      SuppressibleMsgBox('Elegir el archivo de carga del nodo (carga-nodo.json).', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

{ En una actualizacion, los servicios tienen abiertos los archivos del Python
  embebido: se paran antes de copiar. Si no existen, net stop falla y no pasa
  nada. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Codigo: Integer;
begin
  Result := '';
  Exec(ExpandConstant('{sys}\net.exe'), 'stop LibraEdgeNodo /y', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  Exec(ExpandConstant('{sys}\net.exe'), 'stop LibraEdgeProducto /y', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  { PostgreSQL tambien: sus binarios se reemplazan y el RestartManager no puede
    cerrar un servicio. En silencio eso es un Abort y la actualizacion se
    deshace (medido en la VM, 2026-10-19). preparar_postgres.ps1 lo vuelve a
    arrancar, e instalar.ps1 si falla antes de tocar nada. }
  Exec(ExpandConstant('{sys}\net.exe'), 'stop LibraEdgePostgres /y', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
end;

{ Todo lo que no es copiar archivos lo hace instalar.ps1, y su codigo de salida
  pasa a ser el del instalador (GetCustomSetupExitCode). Antes eran entradas de
  la seccion Run, cuyo codigo de salida Inno no mira: un paso fallido terminaba
  igual con 0. (Ninguna linea de [Code] puede empezar con corchete: Inno la toma
  como una seccion nueva.) }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Parametros, Registro: String;
  Codigo: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    Registro := ExpandConstant('{app}\logs\instalacion.log');
    WizardForm.StatusLabel.Caption := 'Preparando la base local y el nodo (puede tardar unos minutos)...';
    Parametros := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\instalador\instalar.ps1') +
      '" -RaizNodo "' + ExpandConstant('{app}') + '" -Carga "' + vCarga +
      '" -Datos "' + ExpandConstant('{commonappdata}\LibraEdge') + '"';
    Log('Configurando el nodo con instalar.ps1; el detalle queda en ' + Registro);
    if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Parametros,
                ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Codigo) then
    begin
      Log('No se pudo ejecutar PowerShell: ' + SysErrorMessage(Codigo));
      CodigoDeSalida := 20;
    end
    else if Codigo <> 0 then
    begin
      Log('instalar.ps1 termino con codigo ' + IntToStr(Codigo) + '.');
      CodigoDeSalida := 20;
    end
    else
      Log('instalar.ps1 termino bien.');
    if CodigoDeSalida <> 0 then
      SuppressibleMsgBox('La configuracion del nodo fallo. El detalle esta en ' + Registro +
        '. Se puede reintentar corriendo instalar.ps1 a mano (ver el README).', mbError, MB_OK, IDOK);
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := CodigoDeSalida;
end;
