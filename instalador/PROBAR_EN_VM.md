# Probar el instalador del nodo en una VM Windows 10 limpia

Pasos en orden para probar el instalador parametrizado (ADR-010) con **VentaLibra**, sin interfaz gráfica: lo de
Linux se corre en el WSL y lo de Windows **dentro de la VM** con `VBoxManage guestcontrol`. Cada paso dice qué
resultado esperar; si no da eso, parar y mirar el log que se indica. Para RestoLibra o ContaLibra es lo mismo cambiando
`ventalibra` por el producto (y `"producto"` en la carga de prueba).

La carga de prueba (`carga-de-prueba.json`) apunta a un central que no existe (`https://central-de-prueba.invalid`):
todo tiene que quedar instalado y andando, y el nodo **sin conexión con el central**. Eso es lo esperable y es lo que
dice `-SinCentral` en la verificación.

## 0. La VM

- Windows 10 x64 limpio, con las Guest Additions (las necesita `guestcontrol`) e internet sólo para bajar Inno Setup
  si no se copia el instalador.
- Mejor **en español** (el grupo `Administradores` y otros nombres cambian; el instalador usa SIDs) y con la zona
  horaria en **UTC** (así se ve que la zona `-03` la pone el `initdb` y no el sistema operativo).
- Un usuario **administrador**. `guestcontrol` puede arrancar los procesos sin elevar (UAC): el paso V0 lo verifica.
  Si da `False`, usar la cuenta `Administrador` integrada (habilitarla una vez desde la consola de la VM con
  `net user Administrador /active:yes` y ponerle clave) o apagar UAC (`EnableLUA=0` y reiniciar).
- Un snapshot de la VM limpia antes de empezar, para repetir.

## Lo que se aprendió en la primera prueba (2026-10-19)

- **`guestcontrol` pierde las comillas** de `-Command` cuando el comando tiene rutas con espacios o `[...]`: cada paso
  conviene escribirlo en un `.ps1`, copiarlo a la VM y correrlo por ruta. Si la política de ejecución lo bloquea, en la
  VM de prueba: `Set-ExecutionPolicy Bypass -Scope LocalMachine -Force`.
- **Los pasos largos (compilar, instalar), desacoplados**: `Start-Process powershell ... *> salida.out` y consultar
  después si terminó. Con 4 GB de RAM, la sesión de `guestcontrol` se corta por tiempo (`VERR_TIMEOUT`) y, tras
  varios cortes, VBoxService deja de aceptar sesiones (`VERR_DUPLICATE`, «Error starting guest session»): se arregla
  reiniciando la VM.
- **`copyfrom` no escribe en `\\wsl.localhost\...`**: traer los archivos a una carpeta de `C:` y moverlos desde ahí.
- **No sacar un snapshot con la VM prendida** (`--live`): con 4 GB tardó más de hora y media y hubo que matar la VM.
- **La bandeja corre en la sesión del usuario conectado en la pantalla**: para probar una actualización de verdad,
  abrirla antes (el acceso directo de Inicio) y reinstalar encima (V10).

## 1. En el WSL: armar lo que se copia

```bash
# La VM y su usuario. La clave va en un archivo, no en la linea de comandos.
export VM="win10-libraedge"            # el nombre en VirtualBox
export VM_USUARIO="Administrador"
export VM_CLAVE_ARCHIVO="$HOME/.vm-win10-clave"   # una linea con la clave, chmod 600

# El producto en origin/develop, en un worktree aparte (no el checkout de trabajo).
git -C ~/proyectos/ventalibra fetch origin
git -C ~/proyectos/ventalibra worktree add ~/proyectos/_wt/ventalibra-nodo origin/develop

# El paquete (baja ~400 MB la primera vez y construye el frontend con npm).
cd ~/proyectos/_wt/libraedge-e7
instalador/armar_paquete.sh ventalibra ~/proyectos/_wt/ventalibra-nodo
#   -> "Listo: .../instalador/paquete/ventalibra (... MB), revision <commit>."

# Inno Setup 6.7.3 (si la URL no existe, la ultima 6.x de https://jrsoftware.org/isdl.php; hace falta 6.3 o mas).
curl -fL -o instalador/paquete/.descargas/innosetup.exe https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe

# Todo en un tar: guestcontrol copia un archivo grande mucho mas rapido que miles chicos.
rm -rf ~/proyectos/_wt/vm-e7 && mkdir -p ~/proyectos/_wt/vm-e7/e7/libraedge
rsync -a --exclude 'paquete/.descargas' --exclude 'salida' instalador ~/proyectos/_wt/vm-e7/e7/
cp libraedge/instalador.py ~/proyectos/_wt/vm-e7/e7/libraedge/
cp instalador/paquete/.descargas/innosetup.exe ~/proyectos/_wt/vm-e7/e7/
tar -C ~/proyectos/_wt/vm-e7 -cf ~/proyectos/_wt/vm-e7/e7.tar e7
ls -lh ~/proyectos/_wt/vm-e7/e7.tar

# Un atajo para correr PowerShell en la VM (todos los pasos V usan esto).
ps_vm() {
  VBoxManage guestcontrol "$VM" run --username "$VM_USUARIO" --passwordfile "$VM_CLAVE_ARCHIVO" \
    --wait-stdout --wait-stderr --exe 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' \
    -- powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$1"
}
```

## 2. Copiar a la VM

```bash
VBoxManage guestcontrol "$VM" mkdir --username "$VM_USUARIO" --passwordfile "$VM_CLAVE_ARCHIVO" --parents 'C:\e7in'
VBoxManage guestcontrol "$VM" copyto --username "$VM_USUARIO" --passwordfile "$VM_CLAVE_ARCHIVO" \
  --target-directory 'C:\e7in\' ~/proyectos/_wt/vm-e7/e7.tar
```

## 3. Dentro de la VM

Cada bloque es un `ps_vm '...'` (comillas simples de bash; adentro, PowerShell).

**V0. Administrador elevado, PowerShell 5.1, idioma y zona.** Esperado: `True`, `5.1...`, `es-...`, `UTC`.

```bash
ps_vm '([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator); $PSVersionTable.PSVersion.ToString(); (Get-UICulture).Name; (Get-TimeZone).Id'
# Si la zona no es UTC:
ps_vm 'Set-TimeZone -Id "UTC"; (Get-TimeZone).Id'
```

**V1. Descomprimir.** Esperado: `True True True`.

```bash
ps_vm 'tar.exe -xf C:\e7in\e7.tar -C C:\ ; Test-Path C:\e7\instalador\nodo.iss; Test-Path C:\e7\instalador\paquete\ventalibra\python\python.exe; Test-Path C:\e7\libraedge\instalador.py'
```

**V2. Instalar Inno Setup** (sólo para compilar). Esperado: `True`.

```bash
ps_vm 'Start-Process C:\e7\innosetup.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-","/ALLUSERS" -Wait; Test-Path "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"'
```

**V3. Compilar.** Tarda unos minutos (comprime cientos de MB). Esperado: `Successful compile`, código `0` y `True`.

```bash
ps_vm 'Set-Location C:\e7\instalador; & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DProducto=ventalibra /DVersion=0.10.0 nodo.iss; "codigo=$LASTEXITCODE"; Test-Path C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe'
```

Un error de ISCC dice archivo y línea: traer la salida entera.

**V4. Sin `/CARGA`, en silencio, no instala nada.** Esperado: `codigo=1` y `False`.

```bash
ps_vm '$p = Start-Process C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/LOG=C:\e7\inno-sin-carga.log" -Wait -PassThru; "codigo=$($p.ExitCode)"; Test-Path "C:\Program Files\LibraEdge"'
```

**V5. Instalar en silencio con la carga de prueba.** Tarda unos minutos. Esperado: `codigo=0`.

```bash
ps_vm '$p = Start-Process C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/CARGA=C:\e7\instalador\carga-de-prueba.json","/LOG=C:\e7\inno.log" -Wait -PassThru; "codigo=$($p.ExitCode)"'
```

Con `codigo=20`, el motivo está al final del log de la configuración:

```bash
ps_vm 'Get-Content "C:\Program Files\LibraEdge\logs\instalacion.log" -Tail 80'
```

Con otro código, el log de Inno: `ps_vm 'Get-Content C:\e7\inno.log -Tail 60'`.

**V6. Verificar.** Esperar dos minutos (que el servicio del nodo cicle) y correr la verificación. Esperado: líneas `OK`,
un `AVISO` de «sin conexión con el central (esperado con -SinCentral)», `Nodo verificado: todo bien.` y `codigo=0`.

```bash
sleep 120
ps_vm '& "C:\Program Files\LibraEdge\instalador\verificar_nodo.ps1" -SinCentral; "codigo=$LASTEXITCODE"'
```

Si algún servicio no está `Running`, su log: `ps_vm 'Get-Content "C:\Program Files\LibraEdge\logs\LibraEdgeProducto.log" -Tail 60'`
(o `LibraEdgeNodo.log`).

**V7. Ningún secreto en los logs.** Esperado: `0` y `0`.

```bash
ps_vm '$logs = @("C:\e7\inno.log") + (Get-ChildItem "C:\Program Files\LibraEdge\logs\*.log").FullName; (Select-String -Path $logs -SimpleMatch "prueba-prueba-prueba-prueba-prueba").Count; $c = ([IO.File]::ReadAllText("C:\ProgramData\LibraEdge\postgres.clave")).Trim(); (Select-String -Path $logs -SimpleMatch $c).Count'
```

**V8. Lo que dejó instalado.** Esperado: el `config.json` con el CUIT ficticio `30-99999904-9`, la ACL de `nodo.env` y
de `postgres.clave` sólo con `NT AUTHORITY\SYSTEM` y `BUILTIN\Administradores`, y `producto.txt` = `ventalibra`.

```bash
ps_vm 'Get-Content C:\ProgramData\LibraEdge\app\config.json; (Get-Acl "C:\Program Files\LibraEdge\nodo.env").Access | Select-Object IdentityReference, FileSystemRights; (Get-Acl C:\ProgramData\LibraEdge\postgres.clave).Access | Select-Object IdentityReference; Get-Content C:\ProgramData\LibraEdge\producto.txt'
```

**V8b. El acceso directo «VentaLibra» y cómo se entra (ADR-013).** Lo que el CI no ve: con el asistente (no
`/VERYSILENT`), la página final dice «En este local se entra siempre por la copia local…» con la IP de la VM; en el
escritorio y en el menú Inicio está «VentaLibra» con la marca naranja; al abrirlo, Edge muestra **una ventana sin barra
de direcciones** con el título y el ícono del producto (anotar si la primera vez Edge pide su configuración inicial);
en la bandeja, el clic abre lo mismo y «Otras cajas: http://<IP>:8000 (copiar)» deja la dirección en el portapapeles.
La bandeja nueva llega con el pin de LibraEdge del producto (ADR-013): con una versión anterior no tiene esas líneas.

```bash
ps_vm '$s = New-Object -ComObject WScript.Shell; foreach ($c in "CommonDesktopDirectory","CommonPrograms") { $l = $s.CreateShortcut((Join-Path ([Environment]::GetFolderPath($c)) "VentaLibra.lnk")); "$($l.TargetPath) $($l.Arguments) [$($l.IconLocation)]" }; Get-Content "C:\Program Files\LibraEdge\instalador\acceso.txt"'
```

**V9. El producto desde «otra caja».** Desde el WSL, con el puerto reenviado o la VM en red puente:
`curl -s http://<ip-de-la-vm>:8000/health` tiene que devolver un objeto JSON. Si la red de la VM quedó como
**Pública**, la regla de firewall no aplica (sólo Privado y Dominio): `ps_vm 'Get-NetConnectionProfile'`.

**V10. Reinstalar encima (actualizar).** Esperado: `codigo=0`, `SECRET_KEY igual: True` y la verificación otra vez en 0.

```bash
ps_vm '$antes = (Select-String -Path "C:\Program Files\LibraEdge\nodo.env" -Pattern "^SECRET_KEY=").Line.GetHashCode(); $p = Start-Process C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/CARGA=C:\e7\instalador\carga-de-prueba.json","/LOG=C:\e7\inno-2.log" -Wait -PassThru; "codigo=$($p.ExitCode)"; $despues = (Select-String -Path "C:\Program Files\LibraEdge\nodo.env" -Pattern "^SECRET_KEY=").Line.GetHashCode(); "SECRET_KEY igual: $($antes -eq $despues)"'
sleep 120
ps_vm '& "C:\Program Files\LibraEdge\instalador\verificar_nodo.ps1" -SinCentral; "codigo=$LASTEXITCODE"'
```

**V11. Una carga de otro producto no se instala ni deja el local parado.** Esperado: `codigo=20`, el log con «La carga
es de 'restolibra' y este instalador es de 'ventalibra'» y los servicios otra vez `Running`.

```bash
ps_vm '$c = Get-Content C:\e7\instalador\carga-de-prueba.json -Raw | ConvertFrom-Json; $c.producto = "restolibra"; $c | ConvertTo-Json -Depth 5 | Set-Content C:\e7\carga-otra.json -Encoding ASCII; $p = Start-Process C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/CARGA=C:\e7\carga-otra.json","/LOG=C:\e7\inno-3.log" -Wait -PassThru; "codigo=$($p.ExitCode)"; Select-String -Path "C:\Program Files\LibraEdge\logs\instalacion.log" -Pattern "es de .restolibra." | Select-Object -Last 1; Get-Service LibraEdge* | Select-Object Name, Status'
```

**V12. Reiniciar.** Esperado: después del reinicio, la verificación en 0 sin tocar nada.

```bash
ps_vm 'Restart-Computer -Force'
sleep 240
ps_vm '& "C:\Program Files\LibraEdge\instalador\verificar_nodo.ps1" -SinCentral; "codigo=$LASTEXITCODE"'
```

**V13. El respaldo.** Esperado: `[ventalibra] verificado: el respaldo restaura y las medidas coinciden.` y `codigo=0`.

```bash
ps_vm 'Start-ScheduledTask -TaskName LibraEdgeRespaldo; Start-Sleep 60; (Get-ScheduledTaskInfo -TaskName LibraEdgeRespaldo).LastTaskResult; Get-ChildItem C:\ProgramData\LibraEdge\respaldos'
ps_vm '& "C:\Program Files\LibraEdge\instalador\respaldo.ps1" -RaizPostgres "C:\Program Files\LibraEdge\postgres" -ArchivoClave C:\ProgramData\LibraEdge\postgres.clave -Bases ventalibra -BaseNodo ventalibra; "codigo=$LASTEXITCODE"'
```

**V14. Desinstalar.** Esperado: sin servicios `LibraEdge*`, sin `nodo.env`, sin la regla de firewall, sin los accesos
directos «VentaLibra» del escritorio y del menú Inicio, y los datos
(`datos`, `postgres.clave`, `app`) **intactos**.

```bash
ps_vm 'Start-Process "C:\Program Files\LibraEdge\unins000.exe" -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART" -Wait; Start-Sleep 10; Get-Service LibraEdge* -ErrorAction SilentlyContinue; Test-Path "C:\Program Files\LibraEdge\nodo.env"; Get-NetFirewallRule -DisplayName "LibraEdge producto" -ErrorAction SilentlyContinue; Get-ChildItem C:\ProgramData\LibraEdge'
```

**V15. El asistente con el código (ADR-014; sólo en la VM: el CI instala en silencio).** Con un central de verdad
(un `dev.` del producto, «Sucursal sin internet» → «Dar de alta el nodo»), ejecutar el instalador **sin argumentos** y
escribir la dirección y el código en «Vincular con el sistema». Esperado: la página del archivo se saltea, la
instalación termina con 0, el nodo aparece «al día» en la pantalla del central y `C:\ProgramData\LibraEdge\carga-nodo.json`
no quedó. Con un código mal tipeado, el asistente avisa y no avanza; con uno vencido, termina con 20 y dice que se pida
otro.

**V16. La app sin vincular (ADR-015).** En una VM recién restaurada (sin el nodo), ejecutar el instalador **sin
argumentos**, dejar vacíos el código y el archivo y aceptar «Instalar sin vincular». Esperado: termina con 0, el
asistente ofrece «Abrir VentaLibra» y la app abre «Vincular con el sistema» con la marca del producto (no la ventana
de Edge). Escribir la dirección y un código de un `dev.` del producto: aparece UAC con el nombre y el ícono de VentaLibra
(«Editor desconocido» mientras no haya firma), «Vinculando…», y al terminar la app carga el producto sola y aparece la
bandeja. Con un código vencido: el error en la pantalla (sin el código) y se puede reintentar. Con un usuario estándar
(no administrador), UAC pide la clave de un administrador.

```bash
ps_vm '$p = Start-Process C:\e7\instalador\salida\libraedge-nodo-ventalibra-0.10.0.exe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SINVINCULAR","/LOG=C:\e7\inno-sin-vincular.log" -Wait -PassThru; "codigo=$($p.ExitCode)"; Test-Path "C:\Program Files\LibraEdge\nodo.env"; (New-Object -ComObject WScript.Shell).CreateShortcut("C:\Users\Public\Desktop\VentaLibra.lnk").TargetPath'
```

**V17. La ventana (ADR-015).** Con el nodo configurado, abrir «VentaLibra» del escritorio. Esperado: la ventana propia
con el título y el ícono del producto (también en la barra de tareas y en Alt+Tab), maximizada la primera vez; F11
alterna la pantalla completa y al cerrar y abrir sigue igual; cerrar y abrir **no** pide volver a loguearse; cambiar
tamaño y posición se recuerda; un enlace externo (una ayuda, ARCA) se abre en Edge/el navegador por defecto y no en la
app; exportar un reporte deja el archivo en Descargas; imprimir un comprobante abre el diálogo de impresión;
`Restart-Service LibraEdgeProducto` con la app abierta muestra «Conectando con VentaLibra…» y vuelve sola a la misma
pantalla; abrir el acceso directo con la app abierta la trae al frente. «VentaLibra (navegador)» del menú Inicio abre
Edge en modo aplicación.

**V18. Reiniciar con la app abierta y en el inicio de sesión.** Copiar el acceso directo al inicio común (README,
«Abrirlo al iniciar sesión»), reiniciar. Esperado: la app arranca antes que el servicio, muestra la espera y entra sola
cuando el producto responde, sin error del navegador.

## 4. Traer los logs

```bash
mkdir -p ~/proyectos/_wt/vm-e7/logs
for f in 'C:\e7\inno.log' 'C:\e7\inno-2.log' 'C:\e7\inno-3.log' 'C:\Program Files\LibraEdge\logs\instalacion.log'; do
  VBoxManage guestcontrol "$VM" copyfrom --username "$VM_USUARIO" --passwordfile "$VM_CLAVE_ARCHIVO" \
    --target-directory ~/proyectos/_wt/vm-e7/logs/ "$f" || true
done
```

(`instalacion.log` ya no está después de V14: traerlo antes de desinstalar si hace falta.)

## Con un central de verdad (opcional)

Para ver el nodo **en línea**, la carga tiene que salir de un central del producto: `libraedge-central nodo registrar
<node_id> --sucursal <sucursal> --carga /app/data/carga-nodo.json --central-url https://...` dentro del contenedor (con
el adicional `offline` contratado). `--carga` es de esta versión de LibraEdge: hasta que el producto suba el pin,
`registrar` imprime las cuatro variables y la carga se arma a mano copiando `carga-de-prueba.json` y reemplazando
`entorno` (más `LIBRAEDGE_CENTRAL_URL`) y `emisor`. Con esa carga, V6 se corre **sin** `-SinCentral` y tiene que dar
«nodo en línea con el central».
