//! La version de escritorio del producto (ADR-015): una ventana propia sobre la copia local del nodo
//! (`http://localhost:<puerto>/`), con el nombre y el icono del producto.
//!
//! - Si el producto responde, lo carga. Si deja de responder (se reinicia el servicio, la PC esta arrancando),
//!   muestra una pantalla de espera propia («Conectando con ContaLibra…») y vuelve sola cuando responde.
//! - Si el nodo de esta PC no esta vinculado, muestra «Vincular con el sistema» (direccion + codigo, ADR-014) y
//!   configura el nodo con la elevacion del sistema (`vinculo.rs`).
//! - F11 alterna la pantalla completa y se recuerda (cajas dedicadas); tambien `--pantalla-completa` y `--ventana`.
//! - Recuerda tamaño y posicion; conserva la sesion del producto entre aperturas (`preferencias.rs`).
//! - Los enlaces que no son del producto (ARCA, Mercado Pago, ayudas) se abren en el navegador del sistema.
//!
//! Si la ventana no se puede crear (falta WebView2 o WebKitGTK), abre el producto en el navegador: el mismo
//! respaldo que el acceso directo «<Producto> (navegador)».
//!
//! **En una caja secundaria** (ADR-017: el mismo binario, instalado sin el nodo; ver `producto.rs`) el producto no es
//! la copia local sino el de la PC principal: la primera vez la busca en la red del local (`descubrimiento.rs`); si
//! hay una la usa, si hay varias deja elegir y si no encuentra ninguna pide la IP a mano. La recuerda
//! (`principal.rs`) y, si deja de responder, muestra la espera y vuelve a buscar por si cambio de IP. No ofrece
//! «Vincular» (no hay nodo) y usa otro identificador (`ar.com.librasuite.<producto>.caja`): otra sesion del
//! navegador embebido y otras preferencias que la app de la PC principal.
//!
//! Linea de comandos (sin argumentos abre la ventana):
//!
//!     --version                         el producto, la version y el modo, y sale
//!     --buscar [--a IP]...              busca las PC principales del producto en la red del local (o en esas IP)
//!                                       sin ventana; imprime una linea JSON por cada una. Sale con 0 si encontro.
//!     --pantalla-completa | --ventana   fija la preferencia y abre
//!     --vincular --direccion D          vincula sin ventana, con el codigo por la entrada estandar (o
//!                                       LIBRAEDGE_CODIGO): la misma funcion que la pantalla. Sale con 0 si quedo.
//!
//! `LIBRAEDGE_ESCRITORIO_INFORME=<archivo>` anota ahi, una por linea, las pantallas que muestra y las paginas
//! del producto que termina de cargar (sin secretos). Lo usa el CI para probar que arranca y carga el producto.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod descubrimiento;
mod navegacion;
mod preferencias;
mod principal;
mod producto;
mod salud;
mod vinculo;

use descubrimiento::Nodo;
use navegacion::{clasificar, Destino, Origen};
use principal::Principal;
use producto::{Modo, Producto, MARCA_SVG, NOMBRE, PRODUCTO, VERSION};
use std::collections::HashSet;
use std::io::Write as _;
use std::net::IpAddr;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::webview::{DownloadEvent, NewWindowResponse, PageLoadEvent};
use tauri::{AppHandle, Manager, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

const PRINCIPAL: &str = "principal";

/// F11 en cualquier pagina, la del producto incluida. Es lo unico que el producto le puede pedir a la app: su
/// capacidad remota solo permite este comando.
const SCRIPT_F11: &str = r#"(function () {
  if (window.__libraedgeF11) return;
  window.__libraedgeF11 = true;
  window.addEventListener('keydown', function (e) {
    if (e.key === 'F11' && !e.repeat) {
      e.preventDefault();
      e.stopPropagation();
      try { window.__TAURI_INTERNALS__.invoke('alternar_pantalla_completa'); } catch (_) {}
    }
  }, true);
})();"#;

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
enum Pantalla {
    Inicio,
    Espera,
    Vincular,
    Vinculando,
    Producto,
    /// Caja: buscando la PC principal en la red del local.
    Buscando,
    /// Caja: hay varias PC principales del producto; se elige una.
    Elegir,
    /// Caja: no se encontro ninguna; se escribe la IP a mano.
    Direccion,
}

impl Pantalla {
    fn nombre(self) -> &'static str {
        match self {
            Pantalla::Inicio => "inicio",
            Pantalla::Espera => "espera",
            Pantalla::Vincular => "vincular",
            Pantalla::Vinculando => "vinculando",
            Pantalla::Producto => "producto",
            Pantalla::Buscando => "buscando",
            Pantalla::Elegir => "elegir",
            Pantalla::Direccion => "direccion",
        }
    }
}

struct Compartido {
    producto: Producto,
    pantalla: Mutex<Pantalla>,
    /// Donde estaba el usuario cuando el producto dejo de responder: se vuelve ahi.
    ultima_url: Mutex<Option<Url>>,
    informe: Option<PathBuf>,
    preferencias: PathBuf,
    sesion: PathBuf,
    /// Caja (ADR-017): la PC principal elegida, donde se recuerda, lo ultimo que se encontro en la red y si se
    /// pidio buscar otra.
    archivo_principal: PathBuf,
    principal: Mutex<Option<Principal>>,
    encontrados: Mutex<Vec<Nodo>>,
    buscar_otra: AtomicBool,
    /// Los origenes a los que ya se les dio la capacidad de F11.
    con_f11: Mutex<HashSet<String>>,
}

impl Compartido {
    fn pantalla(&self) -> Pantalla {
        *self.pantalla.lock().unwrap()
    }

    fn caja(&self) -> bool {
        self.producto.modo == Modo::Caja
    }

    fn principal(&self) -> Option<Principal> {
        self.principal.lock().unwrap().clone()
    }

    /// De donde se sirve el producto: la copia local o la PC principal elegida (en una caja sin elegir, ninguno).
    fn origen(&self) -> Option<Origen> {
        match self.producto.modo {
            Modo::Nodo => Some(Origen::local(self.producto.puerto)),
            Modo::Caja => self.principal().map(|p| p.origen()),
        }
    }

    /// Para clasificar una navegacion: sin origen nada es el producto.
    fn origen_o_ninguno(&self) -> Origen {
        self.origen().unwrap_or(Origen { host: String::new(), puerto: 0 })
    }

    fn poner(&self, p: Pantalla) {
        *self.pantalla.lock().unwrap() = p;
    }

    fn anotar(&self, linea: &str) {
        if let Some(ruta) = &self.informe {
            if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(ruta) {
                let _ = writeln!(f, "{linea}");
            }
        }
    }

    /// Muestra una pantalla propia (espera o vincular).
    fn mostrar(&self, ventana: &WebviewWindow, p: Pantalla) {
        self.poner(p);
        self.anotar(&format!("pantalla {}", p.nombre()));
        let _ = ventana.navigate(navegacion::pagina(p.nombre()));
    }
}

#[derive(serde::Serialize)]
struct EstadoParaPagina {
    producto: &'static str,
    nombre: &'static str,
    version: &'static str,
    marca: &'static str,
    pantalla: Pantalla,
    url: String,
    configurado: bool,
    windows: bool,
    modo: Modo,
    principal: Option<Principal>,
    encontrados: Vec<Nodo>,
    puerto: u16,
}

#[tauri::command]
fn estado(compartido: State<'_, Arc<Compartido>>) -> EstadoParaPagina {
    EstadoParaPagina {
        producto: PRODUCTO,
        nombre: NOMBRE,
        version: VERSION,
        marca: MARCA_SVG,
        pantalla: compartido.pantalla(),
        url: compartido.origen().map(|o| o.url()).unwrap_or_default(),
        configurado: compartido.producto.configurado(),
        windows: cfg!(windows),
        modo: compartido.producto.modo,
        principal: compartido.principal(),
        encontrados: compartido.encontrados.lock().unwrap().clone(),
        puerto: compartido.producto.puerto,
    }
}

/// Caja: la pagina pide buscar otra PC principal (olvida la elegida).
#[tauri::command]
fn buscar_otra(compartido: State<'_, Arc<Compartido>>) {
    compartido.buscar_otra.store(true, Ordering::SeqCst);
}

/// Caja: se eligio una de las PC principales encontradas.
#[tauri::command]
fn elegir(app: AppHandle, compartido: State<'_, Arc<Compartido>>, indice: usize) -> Result<(), String> {
    let nodo = compartido.encontrados.lock().unwrap().get(indice).cloned().ok_or("Ya no está en la lista.")?;
    fijar_principal(&app, &compartido, Principal::from(&nodo));
    Ok(())
}

/// Caja: la IP de la PC principal, escrita a mano. Primero se le pregunta por el descubrimiento (da el puerto y
/// el nodo); si no contesta, se prueba la salud del producto en el puerto escrito (o el de la definicion).
#[tauri::command]
async fn usar_direccion(
    app: AppHandle,
    compartido: State<'_, Arc<Compartido>>,
    direccion: String,
) -> Result<(), String> {
    let compartido = compartido.inner().clone();
    let Some((ip, puerto)) = descubrimiento::parsear_direccion(&direccion) else {
        return Err("Escribir la IP de la PC principal, por ejemplo 192.168.0.10 (o 192.168.0.10:8000).".into());
    };
    let salud = compartido.producto.salud.clone();
    let por_defecto = compartido.producto.puerto;
    let encontrada = tauri::async_runtime::spawn_blocking(move || {
        let destino = std::net::SocketAddr::from((ip, descubrimiento::PUERTO));
        let contesto = descubrimiento::buscar(PRODUCTO, &[destino], Duration::from_secs(2)).into_iter().next();
        if let Some(n) = contesto.filter(|n| puerto.is_none_or(|p| p == n.puerto)) {
            return Some(Principal::from(&n));
        }
        let puerto = puerto.unwrap_or(por_defecto);
        let ip_texto = ip.to_string();
        if !salud::responde_en(IpAddr::V4(ip), &ip_texto, puerto, &salud, Duration::from_secs(4)) {
            return None;
        }
        Some(Principal { ip: ip_texto, puerto, nodo: String::new(), sucursal: String::new(), negocio: String::new() })
    })
    .await
    .map_err(|e| e.to_string())?;
    match encontrada {
        Some(p) => {
            fijar_principal(&app, &compartido, p);
            Ok(())
        }
        None => Err(format!(
            "No contesta {NOMBRE} en {ip}. Revisar que la PC principal esté prendida, con {NOMBRE} abierto, y que \
             las dos PC estén en la misma red."
        )),
    }
}

/// La pagina del producto solo puede pedir la pantalla completa (F11): una capacidad remota por origen.
fn permitir_f11(app: &AppHandle, compartido: &Compartido, origen: &Origen) {
    let clave = format!("{}:{}", origen.host, origen.puerto);
    if !compartido.con_f11.lock().unwrap().insert(clave.clone()) {
        return;
    }
    let id: String = clave.chars().map(|c| if c.is_ascii_alphanumeric() { c } else { '-' }).collect();
    let mut remota = tauri::ipc::CapabilityBuilder::new(format!("producto-{id}")).window(PRINCIPAL);
    for host in origen.hosts() {
        remota = remota.remote(format!("http://{host}:{}/*", origen.puerto));
    }
    if let Err(e) = app.add_capability(remota.permission("allow-alternar-pantalla-completa")) {
        eprintln!("Sin F11 en la pagina del producto: {e}");
    }
}

/// Caja: queda elegida esta PC principal (se recuerda); el vigilante la carga apenas responde.
fn fijar_principal(app: &AppHandle, compartido: &Compartido, p: Principal) {
    principal::guardar(&compartido.archivo_principal, &p);
    compartido.anotar(&format!("principal {}:{} {}", p.ip, p.puerto, p.nodo));
    permitir_f11(app, compartido, &p.origen());
    *compartido.principal.lock().unwrap() = Some(p);
    compartido.poner(Pantalla::Inicio);
}

/// Caja: busca las PC principales del producto en la red del local.
fn buscar_en_la_red(compartido: &Compartido) -> Vec<Nodo> {
    let destinos = descubrimiento::destinos_broadcast(descubrimiento::PUERTO);
    let nodos = descubrimiento::buscar(PRODUCTO, &destinos, Duration::from_secs(2));
    for n in &nodos {
        compartido.anotar(&format!("encontrado {}:{} {}", n.ip, n.puerto, n.nodo));
    }
    nodos
}

#[tauri::command]
async fn vincular(compartido: State<'_, Arc<Compartido>>, direccion: String, codigo: String) -> Result<String, String> {
    let compartido = compartido.inner().clone();
    if compartido.caja() {
        return Err("En las otras cajas no se vincula: se vincula la PC principal.".into());
    }
    if compartido.pantalla() == Pantalla::Vinculando {
        return Err("Ya se está vinculando.".into());
    }
    compartido.poner(Pantalla::Vinculando);
    compartido.anotar("vinculando");
    let producto = compartido.producto.clone();
    let resultado = tauri::async_runtime::spawn_blocking(move || vinculo::vincular(&producto, &direccion, &codigo))
        .await
        .map_err(|e| e.to_string());
    match resultado {
        Ok(r) if r.ok => {
            compartido.anotar("vinculado");
            // La pagina sigue siendo la de vincular, que pasa a «Conectando…»; el vigilante carga el producto
            // apenas responde.
            compartido.poner(Pantalla::Espera);
            abrir_bandeja();
            Ok(r.detalle)
        }
        otro => {
            compartido.anotar("vinculo fallido");
            compartido.poner(Pantalla::Vincular);
            Err(otro.map(|r| r.detalle).unwrap_or_else(|e| e))
        }
    }
}

#[tauri::command]
fn alternar_pantalla_completa(ventana: WebviewWindow, compartido: State<'_, Arc<Compartido>>) {
    let completa = !ventana.is_fullscreen().unwrap_or(false);
    let _ = ventana.set_fullscreen(completa);
    preferencias::guardar(&compartido.preferencias, &preferencias::Preferencias { pantalla_completa: completa });
    compartido.anotar(if completa { "pantalla-completa si" } else { "pantalla-completa no" });
}

/// En Windows la bandeja del nodo la abre el instalador al terminar; si el nodo se vinculo desde la app, se abre
/// aca, con el acceso directo del inicio comun que crea preparar_nodo.ps1. En Linux la abre instalar.sh.
fn abrir_bandeja() {
    #[cfg(windows)]
    if let Some(datos) = std::env::var_os("ProgramData") {
        let lnk =
            PathBuf::from(datos).join(r"Microsoft\Windows\Start Menu\Programs\StartUp\LibraEdge - estado del nodo.lnk");
        if lnk.is_file() {
            let _ = tauri_plugin_opener::open_path(lnk, None::<&str>);
        }
    }
}

/// Abre la copia local en el navegador, el respaldo de cuando la ventana no se puede crear.
fn abrir_en_el_navegador(url: &str) {
    #[cfg(not(windows))]
    {
        let lanzador = "/opt/libraedge-nodo/bandeja/abrir_producto.sh";
        if Path::new(lanzador).is_file()
            && std::process::Command::new(lanzador).arg("--navegador").arg(url).spawn().is_ok()
        {
            return;
        }
    }
    #[cfg(windows)]
    {
        // Edge en modo aplicacion, como el acceso directo de antes (ADR-013).
        let edge = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        .into_iter()
        .find(|r| Path::new(r).is_file());
        if let Some(edge) = edge {
            if std::process::Command::new(edge).arg(format!("--app={url}")).spawn().is_ok() {
                return;
            }
        }
    }
    let _ = tauri_plugin_opener::open_url(url, None::<&str>);
}

fn guardar_sesion(ventana: &WebviewWindow, compartido: &Compartido) {
    let Some(origen) = compartido.origen() else { return };
    let Ok(url) = Url::parse(&origen.url()) else { return };
    if let Ok(cookies) = ventana.cookies_for_url(url) {
        preferencias::guardar_sesion(&compartido.sesion, &preferencias::de_sesion(&cookies));
    }
}

fn reponer_sesion(ventana: &WebviewWindow, compartido: &Compartido) {
    let Some(origen) = compartido.origen() else { return };
    for guardada in preferencias::leer_sesion(&compartido.sesion).unwrap_or_default() {
        let mut cookie = preferencias::a_cookie(&guardada);
        // Sin punto adelante es una cookie del host (como la que puso el producto), no de dominio. En una caja, la
        // del host de la PC principal: si cambio de IP, la sesion sigue siendo la misma en el mismo producto.
        if compartido.caja() || cookie.domain().is_none() {
            cookie.set_domain(origen.host.clone());
        }
        let _ = ventana.set_cookie(cookie);
    }
}

/// El hilo que mira si el producto responde y decide que pantalla va.
fn vigilar(app: AppHandle, compartido: Arc<Compartido>) {
    let producto = compartido.producto.clone();
    let mut fallos = 0u32;
    let mut sesion_repuesta = false;
    let mut ultimo_guardado = Instant::now();
    let mut ultimo_origen: Option<Origen> = None;
    let mut vueltas = 0u64;
    loop {
        let Some(ventana) = app.get_webview_window(PRINCIPAL) else { return };
        vueltas += 1;
        if compartido.caja() && compartido.buscar_otra.swap(false, Ordering::SeqCst) {
            principal::olvidar(&compartido.archivo_principal);
            *compartido.principal.lock().unwrap() = None;
            compartido.anotar("buscar otra");
            compartido.poner(Pantalla::Inicio);
        }
        let Some(origen) = compartido.origen() else {
            // Caja sin PC principal elegida: buscarla.
            elegir_principal(&app, &ventana, &compartido, vueltas);
            std::thread::sleep(Duration::from_millis(1500));
            continue;
        };
        if ultimo_origen.as_ref() != Some(&origen) {
            sesion_repuesta = false;
            fallos = 0;
            ultimo_origen = Some(origen.clone());
        }
        let arriba = match producto.modo {
            Modo::Nodo => salud::responde(producto.puerto, &producto.salud, Duration::from_secs(3)),
            Modo::Caja => origen.host.parse::<IpAddr>().is_ok_and(|ip| {
                salud::responde_en(ip, &origen.host, origen.puerto, &producto.salud, Duration::from_secs(3))
            }),
        };
        let actual = compartido.pantalla();
        match actual {
            Pantalla::Producto if arriba => {
                fallos = 0;
                if ultimo_guardado.elapsed() > Duration::from_secs(20) {
                    guardar_sesion(&ventana, &compartido);
                    ultimo_guardado = Instant::now();
                }
            }
            Pantalla::Producto => {
                fallos += 1;
                // Dos seguidos (unos 6 segundos): un corte de un instante no saca al usuario de donde esta.
                if fallos >= 2 {
                    fallos = 0;
                    if let Ok(url) = ventana.url() {
                        if clasificar(&url, &origen) == Destino::Producto {
                            *compartido.ultima_url.lock().unwrap() = Some(url);
                        }
                    }
                    compartido.mostrar(&ventana, Pantalla::Espera);
                }
            }
            Pantalla::Vinculando => {}
            _ if arriba => {
                if !sesion_repuesta {
                    reponer_sesion(&ventana, &compartido);
                    sesion_repuesta = true;
                }
                // La URL donde estaba, si es del mismo origen (en una caja la PC principal pudo cambiar de IP).
                let guardada = compartido.ultima_url.lock().unwrap().take();
                let destino = guardada
                    .filter(|u| clasificar(u, &origen) == Destino::Producto)
                    .or_else(|| Url::parse(&origen.url()).ok());
                if let Some(url) = destino {
                    compartido.poner(Pantalla::Producto);
                    compartido.anotar(&format!("abriendo {url}"));
                    let _ = ventana.navigate(url);
                }
                ultimo_guardado = Instant::now();
            }
            _ => {
                let deseada = if producto.configurado() { Pantalla::Espera } else { Pantalla::Vincular };
                if actual != deseada {
                    compartido.mostrar(&ventana, deseada);
                }
                // Caja: mientras espera, cada tanto vuelve a buscar por si la PC principal cambio de IP.
                if let Some(p) = compartido.principal().filter(|_| actual == Pantalla::Espera && vueltas % 4 == 0) {
                    let otra = buscar_en_la_red(&compartido)
                        .into_iter()
                        .find(|n| p.es(n) && (n.ip != p.ip || n.puerto != p.puerto));
                    if let Some(n) = otra {
                        compartido.anotar(&format!("cambio de direccion {}:{} -> {}:{}", p.ip, p.puerto, n.ip, n.puerto));
                        let nueva = Principal { ip: n.ip, puerto: n.puerto, ..p };
                        fijar_principal(&app, &compartido, nueva);
                        compartido.poner(Pantalla::Espera);
                    }
                }
            }
        }
        let pausa = if actual == Pantalla::Producto { Duration::from_secs(3) } else { Duration::from_millis(1500) };
        std::thread::sleep(pausa);
    }
}

/// Caja sin PC principal: la busca; si hay una la usa, si hay varias deja elegir y si no hay ninguna pide la IP. En
/// «elegir» se espera; en «direccion» se sigue buscando cada tanto (la PC principal pudo estar apagada).
fn elegir_principal(app: &AppHandle, ventana: &WebviewWindow, compartido: &Compartido, vueltas: u64) {
    let actual = compartido.pantalla();
    match actual {
        Pantalla::Elegir => {}
        Pantalla::Direccion if vueltas % 7 != 0 => {}
        _ => {
            if actual != Pantalla::Direccion {
                compartido.mostrar(ventana, Pantalla::Buscando);
            }
            let nodos = buscar_en_la_red(compartido);
            match nodos.len() {
                1 => fijar_principal(app, compartido, Principal::from(&nodos[0])),
                0 if actual != Pantalla::Direccion => compartido.mostrar(ventana, Pantalla::Direccion),
                0 => {}
                _ => {
                    *compartido.encontrados.lock().unwrap() = nodos;
                    compartido.mostrar(ventana, Pantalla::Elegir);
                }
            }
        }
    }
}

/// Adentro solo el producto y las pantallas propias; lo externo, al navegador del sistema.
fn navegar(compartido: &Compartido, url: &Url) -> bool {
    match clasificar(url, &compartido.origen_o_ninguno()) {
        Destino::Producto | Destino::Paginas | Destino::Interno => true,
        Destino::Externo => {
            compartido.anotar(&format!("externo {}", url.scheme()));
            let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            false
        }
        Destino::Bloqueado => false,
    }
}

/// `target=_blank` y `window.open`: lo del producto (un comprobante para imprimir, un reporte) en otra ventana
/// de la app, con la misma sesion; lo externo, en el navegador.
fn ventana_nueva(
    app: &AppHandle,
    compartido: &Arc<Compartido>,
    url: Url,
    contador: &AtomicUsize,
) -> NewWindowResponse<tauri::Wry> {
    match clasificar(&url, &compartido.origen_o_ninguno()) {
        Destino::Producto => {
            let etiqueta = format!("ventana-{}", contador.fetch_add(1, Ordering::Relaxed) + 1);
            let c = compartido.clone();
            let _ = WebviewWindowBuilder::new(app, etiqueta, WebviewUrl::External(url))
                .title(NOMBRE)
                .inner_size(1000.0, 760.0)
                .initialization_script(SCRIPT_F11)
                .on_navigation(move |u| match clasificar(u, &c.origen_o_ninguno()) {
                    Destino::Producto | Destino::Interno => true,
                    Destino::Externo => {
                        let _ = tauri_plugin_opener::open_url(u.as_str(), None::<&str>);
                        false
                    }
                    _ => false,
                })
                .build();
            NewWindowResponse::Deny
        }
        Destino::Externo => {
            let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            NewWindowResponse::Deny
        }
        // about:blank y blob: los arma el producto (vistas previas): en Windows, la ventana de WebView2.
        Destino::Interno if cfg!(windows) => NewWindowResponse::Allow,
        _ => NewWindowResponse::Deny,
    }
}

/// Las descargas (exportar a Excel, un PDF) van a la carpeta Descargas del usuario y se muestran al terminar.
fn descargar(evento: DownloadEvent<'_>, descargas: Option<&Path>) -> bool {
    match evento {
        DownloadEvent::Requested { url, destination } => {
            if let Some(dir) = descargas {
                if destination.as_os_str().is_empty() || !destination.is_absolute() {
                    let nombre = destination
                        .file_name()
                        .map(|n| n.to_os_string())
                        .or_else(|| {
                            url.path_segments()
                                .and_then(|mut s| s.next_back())
                                .filter(|n| !n.is_empty())
                                .map(Into::into)
                        })
                        .unwrap_or_else(|| "descarga".into());
                    *destination = dir.join(nombre);
                }
            }
            true
        }
        DownloadEvent::Finished { path, success, .. } => {
            if let (true, Some(p)) = (success, path) {
                let _ = tauri_plugin_opener::reveal_item_in_dir(p);
            }
            true
        }
        _ => true,
    }
}

fn ventana_principal(
    app: &AppHandle,
    compartido: Arc<Compartido>,
    pantalla_completa: bool,
) -> tauri::Result<WebviewWindow> {
    let app_nueva = app.clone();
    let c_carga = compartido.clone();
    let c_nav = compartido.clone();
    let c_nueva = compartido.clone();
    let c_titulo = compartido.clone();
    let descargas = app.path().download_dir().ok();
    let contador = AtomicUsize::new(0);
    WebviewWindowBuilder::new(app, PRINCIPAL, WebviewUrl::App("index.html?p=inicio".into()))
        .title(NOMBRE)
        .inner_size(1280.0, 800.0)
        .min_inner_size(800.0, 560.0)
        .maximized(true)
        .fullscreen(pantalla_completa)
        .zoom_hotkeys_enabled(true)
        .initialization_script(SCRIPT_F11)
        .on_navigation(move |url| navegar(&c_nav, url))
        .on_new_window(move |url, _features| ventana_nueva(&app_nueva, &c_nueva, url, &contador))
        .on_download(move |_webview, evento| descargar(evento, descargas.as_deref()))
        .on_document_title_changed(move |ventana, titulo| {
            // El titulo de la pagina del producto (no tiene secretos): el CI lo usa para ver que cargo el producto y
            // no una pagina de error del navegador.
            if ventana.url().is_ok_and(|u| clasificar(&u, &c_titulo.origen_o_ninguno()) == Destino::Producto) {
                c_titulo.anotar(&format!("titulo {titulo}"));
            }
        })
        .on_page_load(move |_ventana, carga| {
            if carga.event() == PageLoadEvent::Finished
                && clasificar(carga.url(), &c_carga.origen_o_ninguno()) == Destino::Producto
            {
                c_carga.anotar(&format!("producto {}", carga.url()));
            }
        })
        .build()
}

fn leer_codigo() -> String {
    if let Ok(c) = std::env::var("LIBRAEDGE_CODIGO") {
        std::env::remove_var("LIBRAEDGE_CODIGO");
        return c;
    }
    let mut linea = String::new();
    let _ = std::io::stdin().read_line(&mut linea);
    linea.trim().to_string()
}

fn valor(args: &[String], opcion: &str) -> Option<String> {
    args.iter().position(|a| a == opcion).and_then(|i| args.get(i + 1)).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let producto = Producto::detectar();

    let modo = match producto.modo {
        Modo::Nodo => "nodo",
        Modo::Caja => "caja",
    };
    if args.iter().any(|a| a == "--version") {
        println!(
            "{NOMBRE} {VERSION} (LibraEdge, version de escritorio; producto {PRODUCTO}, puerto {}, modo {modo})",
            producto.puerto
        );
        return;
    }
    if args.iter().any(|a| a == "--buscar") {
        // Sin ventana (el CI y soporte): la misma busqueda que la pantalla de una caja.
        let ips: Vec<std::net::SocketAddr> = args
            .windows(2)
            .filter(|par| par[0] == "--a")
            .filter_map(|par| par[1].parse::<std::net::Ipv4Addr>().ok())
            .map(|ip| std::net::SocketAddr::from((ip, descubrimiento::PUERTO)))
            .collect();
        let destinos = if ips.is_empty() { descubrimiento::destinos_broadcast(descubrimiento::PUERTO) } else { ips };
        let nodos = descubrimiento::buscar(PRODUCTO, &destinos, Duration::from_secs(3));
        for n in &nodos {
            println!("{}", serde_json::to_string(n).unwrap_or_default());
        }
        std::process::exit(if nodos.is_empty() { 1 } else { 0 });
    }
    #[cfg(windows)]
    if args.iter().any(|a| a == "--vincular-elevado") {
        let direccion = valor(&args, "--direccion").unwrap_or_default();
        let archivo = PathBuf::from(valor(&args, "--archivo-codigo").unwrap_or_default());
        std::process::exit(vinculo::vincular_elevado(&producto, &direccion, &archivo));
    }
    if args.iter().any(|a| a == "--vincular") {
        if producto.modo == Modo::Caja {
            eprintln!("En las otras cajas no se vincula: se vincula la PC principal.");
            std::process::exit(2);
        }
        let direccion = valor(&args, "--direccion").unwrap_or_default();
        let r = vinculo::vincular(&producto, &direccion, &leer_codigo());
        println!("{}", r.detalle);
        std::process::exit(if r.ok { 0 } else { 1 });
    }

    // WebKitGTK con el renderizador DMA-BUF deja la ventana en blanco en maquinas virtuales y con algunos
    // drivers; sin el anda en todas. Con la variable ya puesta (por ejemplo en 0) se respeta.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }

    let informe = std::env::var_os("LIBRAEDGE_ESCRITORIO_INFORME").map(PathBuf::from);
    let forzar = if args.iter().any(|a| a == "--pantalla-completa") {
        Some(true)
    } else if args.iter().any(|a| a == "--ventana") {
        Some(false)
    } else {
        None
    };
    let url = producto.url();
    let caja = producto.modo == Modo::Caja;
    // Una caja usa otro identificador que la app de la PC principal: otra sesion del navegador embebido, otras
    // preferencias y otra instancia unica (las dos pueden estar en la misma PC, como en el CI).
    let mut contexto = tauri::generate_context!();
    if caja {
        contexto.config_mut().identifier = format!("ar.com.librasuite.{PRODUCTO}.caja");
    }

    let principal_al_salir: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let principal_en_setup = principal_al_salir.clone();
    let resultado = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
        tauri::Builder::default()
            .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
                // Abrir el acceso directo con la app abierta la trae al frente.
                if let Some(v) = app.get_webview_window(PRINCIPAL) {
                    let _ = v.unminimize();
                    let _ = v.show();
                    let _ = v.set_focus();
                }
            }))
            .plugin(
                tauri_plugin_window_state::Builder::default()
                    .with_state_flags(
                        tauri_plugin_window_state::StateFlags::SIZE
                            | tauri_plugin_window_state::StateFlags::POSITION
                            | tauri_plugin_window_state::StateFlags::MAXIMIZED,
                    )
                    .with_filter(|etiqueta| etiqueta == PRINCIPAL)
                    .build(),
            )
            .plugin(tauri_plugin_opener::init())
            .invoke_handler(tauri::generate_handler![
                estado,
                vincular,
                alternar_pantalla_completa,
                buscar_otra,
                elegir,
                usar_direccion
            ])
            .setup(move |app| {
                let config = app.path().app_config_dir()?;
                let datos = app.path().app_local_data_dir()?;
                let archivo_principal = config.join("principal.json");
                let elegida = if caja { principal::leer(&archivo_principal) } else { None };
                let compartido = Arc::new(Compartido {
                    producto: producto.clone(),
                    pantalla: Mutex::new(Pantalla::Inicio),
                    ultima_url: Mutex::new(None),
                    informe: informe.clone(),
                    preferencias: config.join("preferencias.json"),
                    sesion: datos.join("sesion.json"),
                    archivo_principal,
                    principal: Mutex::new(elegida),
                    encontrados: Mutex::new(Vec::new()),
                    buscar_otra: AtomicBool::new(false),
                    con_f11: Mutex::new(HashSet::new()),
                });
                let mut prefs = preferencias::leer(&compartido.preferencias);
                if let Some(valor) = forzar {
                    prefs.pantalla_completa = valor;
                    preferencias::guardar(&compartido.preferencias, &prefs);
                }
                // La pagina del producto solo puede pedir la pantalla completa (F11): la copia local, o la PC
                // principal ya elegida (si se elige otra, fijar_principal le da la suya).
                if let Some(origen) = compartido.origen() {
                    permitir_f11(app.handle(), &compartido, &origen);
                }
                *principal_en_setup.lock().unwrap() = compartido.origen().map(|o| o.url());
                app.manage(compartido.clone());
                compartido.anotar(&format!("inicio {PRODUCTO} {VERSION} {}", if caja { "caja" } else { "nodo" }));
                // Un error aca adentro seria un panic dentro del bucle de eventos (en Linux, a traves de GTK: aborta
                // sin respaldo). Si la ventana no se puede crear (WebView2 o WebKitGTK rotos), el navegador.
                match ventana_principal(app.handle(), compartido.clone(), prefs.pantalla_completa) {
                    Ok(_) => {
                        let handle = app.handle().clone();
                        std::thread::spawn(move || vigilar(handle, compartido));
                    }
                    Err(e) => {
                        eprintln!("No se pudo abrir la ventana de {NOMBRE} ({e}); se abre en el navegador.");
                        compartido.anotar("respaldo navegador");
                        if let Some(origen) = compartido.origen() {
                            abrir_en_el_navegador(&origen.url());
                        }
                        app.handle().exit(0);
                    }
                }
                Ok(())
            })
            .run(contexto)
    }));
    if !matches!(resultado, Ok(Ok(()))) {
        eprintln!("No se pudo abrir la ventana de {NOMBRE}; se abre en el navegador.");
        // En una caja, la PC principal recordada (si no hay ninguna, no hay a donde ir).
        let destino = if caja { principal_al_salir.lock().ok().and_then(|p| p.clone()) } else { Some(url) };
        if let Some(destino) = destino {
            abrir_en_el_navegador(&destino);
        }
    }
}
