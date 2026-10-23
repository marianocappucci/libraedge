; Instalador de la app de un PUESTO SECUNDARIO para Windows (Inno Setup 6.3 o superior), ADR-017.
;
; Es SOLO la version de escritorio del producto (escritorio/, ADR-015): sin nodo, sin PostgreSQL y sin Python. Va en
; las otras PC del local; la PC principal es la que tiene el nodo (nodo.iss). Al abrirse, la app busca sola la PC
; principal en la red del local (descubrimiento, UDP 47823) y abre el producto desde ella.
;
;     ISCC.exe /DProducto=ventalibra /DVersion=0.17.0 puesto.iss
;     libraedge-puesto-ventalibra-0.17.0.exe /VERYSILENT /SUPPRESSMSGBOXES     (desatendido)
;
; Es el MISMO binario que el de la PC principal (paquete\escritorio\<producto>.exe, lo compila escritorio/armar.py):
; instalado aca, sin instalador\producto.json al lado, arranca en modo puesto.
;
; Se publica con nombre fijo (libraedge-puesto-<producto>.exe) en el Release, junto al del nodo. El AppId es por
; producto, fijo y distinto del del nodo: la tabla de abajo la compara un test con productos\*.json.
;
; Los textos van SIN acentos: sin BOM, ISCC lee este archivo con la pagina de codigos del sistema.

#ifndef Producto
  #error Falta el producto: ISCC /DProducto=restolibra|ventalibra|contalibra|libracargo|libraclub puesto.iss
#endif

#if Producto == "restolibra"
  #define NombreProducto "RestoLibra"
  #define AppIdPuesto      "2B30A2D5-950F-465A-8259-8038FC3E1A62"
  #define AppIdNodo      "F4887308-2B5A-4D5A-B694-1F85F48CBA4B"
#elif Producto == "ventalibra"
  #define NombreProducto "VentaLibra"
  #define AppIdPuesto      "FD046125-739B-493B-A30C-6BF729251C24"
  #define AppIdNodo      "ED40FA78-DE81-4F77-8006-2930D4ADBCAB"
#elif Producto == "contalibra"
  #define NombreProducto "ContaLibra"
  #define AppIdPuesto      "C9FBAE20-2ACA-492F-AF32-BA9469BF4634"
  #define AppIdNodo      "4BE6AB1D-4F2C-4255-9D16-C119C22D1DFC"
#elif Producto == "libracargo"
  #define NombreProducto "LibraCargo"
  #define AppIdPuesto      "F131A91E-D036-42AA-AFCC-BB3F512D7948"
  #define AppIdNodo      "A5239B4A-E3B6-4B8F-B075-2A40970AFDAF"
#elif Producto == "libraclub"
  #define NombreProducto "LibraClub"
  #define AppIdPuesto      "BF9DDF2E-ABE6-46C5-BB4A-6C17A60FBFE7"
  #define AppIdNodo      "ED397C8B-C399-4EF8-B829-52A89FAC3251"
#else
  #error Producto desconocido: agregar productos\<producto>.json y su fila en esta tabla
#endif

#ifndef Version
  #define Version "0.0.0"
#endif
#define Editor "Mariano Cappucci"

[Setup]
AppId={{{#AppIdPuesto}}
AppName={#NombreProducto} (puesto)
AppVersion={#Version}
AppPublisher={#Editor}
DefaultDirName={autopf}\{#NombreProducto} (puesto)
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=salida
OutputBaseFilename=libraedge-puesto-{#Producto}-{#Version}
Compression=lzma2/max
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
; La app abierta (de cualquier usuario de esta sesion) se cierra para reemplazarla.
CloseApplications=force
SetupIconFile=iconos\{#Producto}.ico
UninstallDisplayIcon={app}\{#NombreProducto}.exe
#ifdef Firmar
SignTool=firma
SignedUninstaller=yes
#endif

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "paquete\escritorio\{#Producto}.exe"; DestDir: "{app}"; DestName: "{#NombreProducto}.exe"; Flags: ignoreversion

[Icons]
; "<Producto>" en el escritorio publico y en el menu Inicio, como en la PC principal (ADR-013).
Name: "{commondesktop}\{#NombreProducto}"; Filename: "{app}\{#NombreProducto}.exe"; WorkingDir: "{app}"; Comment: "Abre {#NombreProducto} desde la PC principal del local"
Name: "{commonprograms}\{#NombreProducto}"; Filename: "{app}\{#NombreProducto}.exe"; WorkingDir: "{app}"; Comment: "Abre {#NombreProducto} desde la PC principal del local"

[Run]
Filename: "{app}\{#NombreProducto}.exe"; Description: "Abrir {#NombreProducto}"; Flags: postinstall nowait skipifsilent runasoriginaluser

[UninstallRun]
; La app abierta en cualquier sesion tiene tomado su .exe. Solo la de esta carpeta: en la PC principal (donde no hace
; falta el puesto, pero puede estar) la app del nodo tiene el mismo nombre de proceso.
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-Process | Where-Object {{ $_.Path -like '{app}\*' } | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "CerrarApp"

[Code]
{ El runtime de WebView2 (Evergreen): viene con Windows 11 y con las actualizaciones de Edge en Windows 10. Sin el,
  la app no puede abrir su ventana. Mismo criterio que Test-WebView2 de comun.ps1. }
function HayWebView2: Boolean;
var
  Pv: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Pv) and (Pv <> '') and (Pv <> '0.0.0.0'));
end;

{ Esta PC ya tiene el nodo del producto: es la principal y no necesita el puesto. Se avisa y se deja seguir. }
function EsLaPrincipal: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppIdNodo}}_is1');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if EsLaPrincipal then
  begin
    Log('Esta PC ya tiene el nodo LibraEdge de {#NombreProducto}: es la PC principal.');
    if not WizardSilent then
      Result := MsgBox('Esta PC ya tiene {#NombreProducto} con el nodo LibraEdge: es la PC principal del local y no ' +
        'necesita la app de puesto (se entra con el acceso directo "{#NombreProducto}" que ya tiene).' + #13#10#13#10 +
        'Instalar igual?', mbConfirmation, MB_YESNO) = IDYES;
  end;
  if Result and not HayWebView2 then
  begin
    Log('Falta el runtime de WebView2: la app no va a poder abrir su ventana.');
    if not WizardSilent then
      MsgBox('Esta PC no tiene el runtime de Microsoft Edge WebView2, que usa {#NombreProducto} para abrir su ventana. ' +
        'Se instala desde https://go.microsoft.com/fwlink/p/?LinkId=2124703 (Windows 11 ya lo trae).',
        mbInformation, MB_OK);
  end;
end;
