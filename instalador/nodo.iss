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
; Instalacion desatendida con el codigo de vinculacion (ADR-014; la direccion y
; el codigo los muestra el sistema en "Sucursal sin internet"):
;     set LIBRAEDGE_CODIGO=K7Q4-M2XP
;     libraedge-nodo-ventalibra-0.14.0.exe /VERYSILENT /SUPPRESSMSGBOXES /DIRECCION=sistema.ventalibra.com.ar /LOG=C:\ruta\inno.log
; Tambien /CODIGO=K7Q4-M2XP, pero Inno Setup escribe su linea de comandos en el
; /LOG: el codigo quedaria en ese archivo. Sirve una sola vez y vence a los 15
; minutos (y el instalador lo canjea apenas arranca), asi que el riesgo es
; chico; con la variable LIBRAEDGE_CODIGO no queda en ningun lado.
; Con el archivo de carga (soporte):
;     libraedge-nodo-ventalibra-0.14.0.exe /VERYSILENT /SUPPRESSMSGBOXES /CARGA=C:\ruta\carga-nodo.json /LOG=C:\ruta\inno.log
; Sin vincular (ADR-015): copia todo y deja el acceso directo de la app, que
; muestra "Vincular con el sistema" y configura el nodo con permiso de
; administrador (UAC). En el asistente, dejando vacios el codigo y el archivo.
;     libraedge-nodo-ventalibra-0.14.0.exe /VERYSILENT /SUPPRESSMSGBOXES /SINVINCULAR
;
; La app de escritorio (ADR-015) va en {app}\escritorio\<Producto>.exe: la
; compila escritorio/armar.py y la deja el CI en paquete\escritorio\<producto>.exe
; (armar_paquete.sh no la arma). Sin ella el instalador se compila igual y el
; acceso directo abre Edge en modo aplicacion, como antes.
;
; Firma de codigo (todavia sin certificado): ISCC /DFirmar "/Sfirma=signtool sign ... $f"
; firma el instalador y el desinstalador (la app la firma escritorio/armar.py con
; LIBRAEDGE_FIRMA_CMD). Ver el README, seccion "Firma de codigo".
;
; Codigos de salida: 0 bien; 1 no arranco (falta el codigo o la carga, u otro
; producto ya instalado); 20 los archivos se copiaron pero la configuracion del
; nodo fallo, por ejemplo un codigo vencido (el detalle esta en
; {app}\logs\instalacion.log). Los demas son los de Inno.
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
; La marca de LibraEdge (libra-ui, ADR-078): el icono del .exe del instalador
; y el de "Agregar o quitar programas", que Windows lee del archivo instalado.
SetupIconFile=libraedge.ico
UninstallDisplayIcon={app}\libraedge.ico
#ifdef Firmar
SignTool=firma
SignedUninstaller=yes
#endif

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
Source: "libraedge.ico";                 DestDir: "{app}";              Flags: ignoreversion
; La marca del producto, para su acceso directo (ADR-013; lo crea preparar_nodo.ps1).
Source: "iconos\{#Producto}.ico";        DestDir: "{app}"; DestName: "producto.ico"; Flags: ignoreversion
Source: "productos\{#Producto}.json";     DestDir: "{app}\instalador"; DestName: "producto.json"; Flags: ignoreversion
; La version de escritorio del producto (ADR-015): el acceso directo "<Producto>" la abre.
Source: "paquete\escritorio\{#Producto}.exe"; DestDir: "{app}\escritorio"; DestName: "{#NombreProducto}.exe"; Flags: ignoreversion skipifsourcedoesntexist
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

[Run]
; Al terminar el asistente, abrir la app (con el nodo sin vincular, muestra "Vincular con el sistema").
Filename: "{app}\escritorio\{#NombreProducto}.exe"; Description: "Abrir {#NombreProducto}"; Flags: postinstall nowait skipifsilent skipifdoesntexist runasoriginaluser

[UninstallRun]
; La app abierta en cualquier sesion tiene tomado su .exe: se cierra antes de borrar.
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM ""{#NombreProducto}.exe"""; Flags: runhidden; RunOnceId: "CerrarApp"
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
; Los accesos directos del producto (ADR-013), que tambien crea preparar_nodo.ps1:
; el del escritorio publico, el del menu Inicio y, si se activo a mano la opcion
; de abrirlo al iniciar sesion (README), su copia en el inicio comun.
Type: files; Name: "{commondesktop}\{#NombreProducto}.lnk"
Type: files; Name: "{commonprograms}\{#NombreProducto}.lnk"
Type: files; Name: "{commonstartup}\{#NombreProducto}.lnk"
; El respaldo del navegador (ADR-015), en el menu Inicio.
Type: files; Name: "{commonprograms}\{#NombreProducto} (navegador).lnk"
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
; respaldos\ y producto.txt (un nodo por PC). Y conservado.env: SECRET_KEY y la
; clave del admin local, que guarda CurUninstallStepChanged antes de borrar
; nodo.env (sin el SECRET_KEY, lo que libraauth cifro en esos datos no se lee al
; reinstalar). El secreto del nodo no se conserva.

[Code]
{ El nodo se vincula con la DIRECCION del sistema y un CODIGO de vinculacion
  (ADR-014), que muestra el central en "Sucursal sin internet" al dar de alta el
  nodo: se escriben en el asistente, o van en /DIRECCION= y LIBRAEDGE_CODIGO
  (o /CODIGO=) en una instalacion desatendida. instalar.ps1 canjea el codigo por
  la carga antes de tocar nada. La alternativa para soporte es el ARCHIVO de
  carga (carga-nodo.json): se elige en el asistente o va en /CARGA=.

  Nunca se pasan secretos por la linea de comandos: Inno Setup escribe su linea
  de comandos en el log (/LOG), y los scripts de PowerShell la suya en el
  transcript. Viaja la RUTA de la carga; el secreto lo lee el ayudante. El
  codigo viaja en la variable LIBRAEDGE_CODIGO de este proceso, que heredan
  PowerShell y el ayudante.

  (Ojo con las llaves en estos comentarios: en Pascal Script la llave ES el
  comentario y una de mas lo cierra antes de tiempo.) }
var
  PaginaVinculo: TInputQueryWizardPage;
  PaginaCarga: TInputFileWizardPage;
  vCarga, vDireccion, vCodigo: String;
  vSinVincular: Boolean;
  CodigoDeSalida: Integer;
  ServiciosParados: Boolean;

function SetEnvironmentVariable(lpName, lpValue: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

{ El codigo sin espacios ni guiones y en mayusculas: 8 de
  23456789ABCDEFGHJKMNPQRSTUVWXYZ (ADR-014). Vacio si no tiene esa forma. }
function CodigoNormalizado(const Texto: String): String;
var
  I: Integer;
  C: String;
begin
  Result := '';
  for I := 1 to Length(Texto) do
  begin
    C := Uppercase(Copy(Texto, I, 1));
    if (C = ' ') or (C = '-') or (C = #9) then
      Continue;
    if Pos(C, '23456789ABCDEFGHJKMNPQRSTUVWXYZ') = 0 then
    begin
      Result := '';
      Exit;
    end;
    Result := Result + C;
  end;
  if Length(Result) <> 8 then
    Result := '';
end;

{ La direccion va como argumento de PowerShell: solo letras, numeros y . : / - _ }
function DireccionValida(const Texto: String): Boolean;
var
  I: Integer;
  C: String;
begin
  Result := Texto <> '';
  for I := 1 to Length(Texto) do
  begin
    C := Copy(Texto, I, 1);
    if Pos(Lowercase(C), 'abcdefghijklmnopqrstuvwxyz0123456789.:/-_') = 0 then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

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
  vDireccion := Trim(ExpandConstant('{param:DIRECCION|}'));
  if vDireccion = '' then
    vDireccion := Trim(GetEnv('LIBRAEDGE_DIRECCION'));
  vCodigo := Trim(ExpandConstant('{param:CODIGO|}'));
  if vCodigo = '' then
    vCodigo := Trim(GetEnv('LIBRAEDGE_CODIGO'));
  vSinVincular := WizardSilent and (Pos('/SINVINCULAR', Uppercase(GetCmdTail)) > 0);

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

  { En silencio no hay a quien preguntarle: sin codigo ni carga se aborta antes
    de tocar el disco. El codigo no se loguea. }
  if WizardSilent then
  begin
    if vSinVincular then
    begin
      if (vCodigo <> '') or (vDireccion <> '') or (vCarga <> '') then
      begin
        Log('/SINVINCULAR va sin codigo, direccion ni carga.');
        SuppressibleMsgBox('Instalacion desatendida: /SINVINCULAR va sin codigo, direccion ni carga.',
          mbError, MB_OK, IDOK);
        Result := False;
      end;
    end
    else if (vCodigo <> '') or (vDireccion <> '') then
    begin
      if (CodigoNormalizado(vCodigo) = '') or (not DireccionValida(vDireccion)) or (vCarga <> '') then
      begin
        Log('Instalacion desatendida con un codigo o una direccion que no sirven (o con codigo y carga a la vez).');
        SuppressibleMsgBox('Instalacion desatendida: el codigo de vinculacion va con /DIRECCION= (la direccion ' +
          'del sistema), tiene 8 letras y numeros (como K7Q4-M2XP) y no va junto con /CARGA=.',
          mbError, MB_OK, IDOK);
        Result := False;
      end;
    end
    else if (vCarga = '') or (not FileExists(vCarga)) then
    begin
      Log('Instalacion desatendida sin codigo ni carga, o con un archivo de carga que no existe.');
      SuppressibleMsgBox('Instalacion desatendida: falta /DIRECCION= con LIBRAEDGE_CODIGO (o /CODIGO=), ' +
        '/CARGA=<ruta de carga-nodo.json> o /SINVINCULAR.', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

procedure InitializeWizard;
begin
  PaginaVinculo := CreateInputQueryPage(wpSelectDir,
    'Vincular con el sistema',
    'La direccion y el codigo que muestra el sistema',
    'En el sistema, entre a Configuracion > Sucursal sin internet y de de alta el nodo de esta sucursal ' +
    '(o pida otro codigo): ahi aparecen la direccion del sistema y un codigo de vinculacion. ' +
    'El codigo vale 15 minutos y una sola vez.' + #13#10#13#10 +
    'Si soporte le dio un archivo de carga, deje los dos campos vacios y eligalo en la pagina siguiente. ' +
    'Sin codigo ni archivo, se instala sin vincular y se vincula despues desde {#NombreProducto}.');
  PaginaVinculo.Add('Direccion del sistema (por ejemplo sistema.contalibra.com.ar):', False);
  PaginaVinculo.Add('Codigo de vinculacion (por ejemplo K7Q4-M2XP):', False);
  PaginaVinculo.Values[0] := vDireccion;
  PaginaVinculo.Values[1] := vCodigo;

  PaginaCarga := CreateInputFilePage(PaginaVinculo.ID,
    'Archivo de carga (soporte)',
    'Solo si soporte le dio el archivo de carga del nodo',
    'El archivo carga-nodo.json que se descarga en Sucursal sin internet (Archivo de carga). ' +
    'Tiene el secreto del nodo: borrarlo despues de instalar.');
  PaginaCarga.Add('Archivo de carga:', 'Carga del nodo (*.json)|*.json|Todos los archivos|*.*', '.json');
  PaginaCarga.Values[0] := vCarga;
end;

{ Con el codigo no hace falta el archivo. }
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = PaginaCarga.ID) and (vCodigo <> '');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  { En silencio el asistente igual pasa por las paginas: /SINVINCULAR ya se valido en InitializeSetup. }
  if WizardSilent and vSinVincular then
    Exit;
  if CurPageID = PaginaVinculo.ID then
  begin
    vDireccion := Trim(PaginaVinculo.Values[0]);
    vCodigo := Trim(PaginaVinculo.Values[1]);
    if (vDireccion = '') and (vCodigo = '') then
      Exit;
    if not DireccionValida(vDireccion) then
    begin
      SuppressibleMsgBox('Escribir la direccion del sistema tal como la muestra Sucursal sin internet ' +
        '(por ejemplo sistema.contalibra.com.ar).', mbError, MB_OK, IDOK);
      Result := False;
    end
    else if CodigoNormalizado(vCodigo) = '' then
    begin
      SuppressibleMsgBox('El codigo de vinculacion tiene 8 letras y numeros, como K7Q4-M2XP. ' +
        'Si vencio, pedir otro en Sucursal sin internet.', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end
  else if CurPageID = PaginaCarga.ID then
  begin
    vCarga := Trim(PaginaCarga.Values[0]);
    vSinVincular := False;
    if vCarga = '' then
    begin
      { Sin codigo ni archivo: se instala sin vincular y se vincula desde la app (ADR-015). }
      if SuppressibleMsgBox('No se escribio un codigo ni se eligio un archivo de carga. Instalar sin vincular? ' +
        'Despues, al abrir {#NombreProducto}, se piden la direccion y el codigo (y la clave de un administrador).',
        mbConfirmation, MB_YESNO, IDNO) = IDYES then
        vSinVincular := True
      else
        Result := False;
    end
    else if not FileExists(vCarga) then
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
  ServiciosParados := True;
  { Y todo proceso que corra desde el Python del nodo, de cualquier sesion: la
    bandeja corre en la sesion del usuario conectado, y el RestartManager no
    cierra procesos de otra sesion (Session Mismatch). Tenia tomado
    libffi-8.dll y la actualizacion se abortaba (VM, 2026-10-19). La bandeja
    vuelve sola en el proximo inicio de sesion. }
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $_.Path -like ''' +
    ExpandConstant('{app}') + '\python\*'' -or $_.Path -like ''' +
    ExpandConstant('{app}') + '\escritorio\*'' } | Stop-Process -Force"',
    '', SW_HIDE, ewWaitUntilTerminated, Codigo);
end;

{ Si la instalacion se aborta despues de parar los servicios (un archivo que no
  se pudo reemplazar), Inno deshace la copia pero nadie los arrancaba: el local
  quedaba sin sistema hasta reiniciar la PC (VM, 2026-10-19). Desde
  ssPostInstall se encarga instalar.ps1. }
procedure DeinitializeSetup;
var
  Codigo: Integer;
begin
  if ServiciosParados then
  begin
    Log('La instalacion no llego a configurar el nodo: se vuelven a arrancar los servicios.');
    Exec(ExpandConstant('{sys}\net.exe'), 'start LibraEdgePostgres', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
    Exec(ExpandConstant('{sys}\net.exe'), 'start LibraEdgeProducto', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
    Exec(ExpandConstant('{sys}\net.exe'), 'start LibraEdgeNodo', '', SW_HIDE, ewWaitUntilTerminated, Codigo);
  end;
end;

{ Como se entra al producto, con la IP de esta PC (ADR-013): lo escribe
  preparar_nodo.ps1 en acceso.txt. En esta PC se entra SIEMPRE por la copia
  local, aunque haya internet. }
function TextoDeAcceso: String;
var
  Lineas: TArrayOfString;
  I: Integer;
begin
  Result := '';
  if LoadStringsFromFile(ExpandConstant('{app}\instalador\acceso.txt'), Lineas) then
    for I := 0 to GetArrayLength(Lineas) - 1 do
      Result := Result + Lineas[I] + #13#10;
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
    ServiciosParados := False;
    Registro := ExpandConstant('{app}\logs\instalacion.log');
    WizardForm.StatusLabel.Caption := 'Preparando la base local y el nodo (puede tardar unos minutos)...';
    Parametros := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\instalador\instalar.ps1') +
      '" -RaizNodo "' + ExpandConstant('{app}') + '" -Datos "' + ExpandConstant('{commonappdata}\LibraEdge') + '"';
    if vSinVincular then
    begin
      Parametros := Parametros + ' -SinVincular';
      Log('Sin vincular: el nodo se vincula despues desde la app.');
    end
    else if vCodigo <> '' then
    begin
      { El codigo, por el entorno que hereda PowerShell (nunca en Parametros:
        el transcript anota la linea de comandos). }
      SetEnvironmentVariable('LIBRAEDGE_CODIGO', CodigoNormalizado(vCodigo));
      Parametros := Parametros + ' -Direccion "' + vDireccion + '"';
      Log('Configurando el nodo con el codigo de vinculacion de ' + vDireccion + '.');
    end
    else
      Parametros := Parametros + ' -Carga "' + vCarga + '"';
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
    begin
      Log('instalar.ps1 termino bien.');
      Log(TextoDeAcceso);
      { La bandeja se abre sola al iniciar sesion (acceso directo de Inicio),
        pero despues de instalar o actualizar no estaba hasta el proximo inicio:
        PrepareToInstall la cierra y nadie la volvia a abrir (VM, 2026-10-19).
        Se abre en la sesion de quien ejecuto el instalador, sin esperar. }
      if FileExists(ExpandConstant('{commonstartup}\LibraEdge - estado del nodo.lnk')) then
        if not ShellExecAsOriginalUser('', ExpandConstant('{commonstartup}\LibraEdge - estado del nodo.lnk'),
                                       '', '', SW_SHOWNORMAL, ewNoWait, Codigo) then
          Log('No se pudo abrir la bandeja: ' + SysErrorMessage(Codigo));
    end;
    SetEnvironmentVariable('LIBRAEDGE_CODIGO', '');
    if CodigoDeSalida <> 0 then
      SuppressibleMsgBox('La configuracion del nodo fallo. El detalle esta en ' + Registro +
        '. Si el codigo vencio o ya se uso, pedir otro en Sucursal sin internet y volver a ejecutar el ' +
        'instalador.', mbError, MB_OK, IDOK);
  end;
end;

{ Lo que se conserva de nodo.env al desinstalar (igual que el postrm de Linux,
  ADR-012): SECRET_KEY y la clave del admin local, nunca el secreto del nodo.
  El archivo se crea vacio, se le pone la ACL de SYSTEM y Administradores (por
  SID) y recien despues se escribe: asi no hay un momento en que lo lea
  cualquier usuario de la PC. Al reinstalar con la carga, preparar_nodo.ps1 se
  lo pasa al ayudante (--previo) y lo borra. }
function EsVariableConservada(const Nombre: String): Boolean;
begin
  Result := (Nombre = 'SECRET_KEY') or (Nombre = 'ADMIN_PASSWORD') or
    ((Length(Nombre) > 15) and (Copy(Nombre, Length(Nombre) - 14, 15) = '_ADMIN_PASSWORD'));
end;

procedure GuardarConservado;
var
  Lineas, Conservadas: TArrayOfString;
  I, N, Igual, Codigo: Integer;
  Destino: String;
begin
  if not LoadStringsFromFile(ExpandConstant('{app}\nodo.env'), Lineas) then
    exit;
  N := 0;
  for I := 0 to GetArrayLength(Lineas) - 1 do
  begin
    Igual := Pos('=', Lineas[I]);
    if (Igual > 1) and EsVariableConservada(Copy(Lineas[I], 1, Igual - 1)) then
    begin
      SetArrayLength(Conservadas, N + 1);
      Conservadas[N] := Lineas[I];
      N := N + 1;
    end;
  end;
  if N = 0 then
    exit;
  Destino := ExpandConstant('{commonappdata}\LibraEdge\conservado.env');
  if not SaveStringToFile(Destino, '', False) then
    exit;
  if not Exec(ExpandConstant('{sys}\icacls.exe'), '"' + Destino + '" /inheritance:r /grant:r *S-1-5-18:F *S-1-5-32-544:F',
              '', SW_HIDE, ewWaitUntilTerminated, Codigo) or (Codigo <> 0) then
  begin
    DeleteFile(Destino);
    exit;
  end;
  SaveStringsToFile(Destino, Conservadas, False);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    GuardarConservado;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Texto: String;
begin
  if (CurPageID = wpFinished) and (CodigoDeSalida = 0) then
  begin
    Texto := TextoDeAcceso;
    if Texto <> '' then
    begin
      WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 + Texto;
      WizardForm.FinishedLabel.AdjustHeight;
    end;
  end;
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := CodigoDeSalida;
end;
