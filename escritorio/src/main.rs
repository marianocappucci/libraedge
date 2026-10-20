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
//! Linea de comandos (sin argumentos abre la ventana):
//!
//!     --version                         el producto y la version, y sale
//!     --pantalla-completa | --ventana   fija la preferencia y abre
//!     --vincular --direccion D          vincula sin ventana, con el codigo por la entrada estandar (o
//!                                       LIBRAEDGE_CODIGO): la misma funcion que la pantalla. Sale con 0 si quedo.
//!
//! `LIBRAEDGE_ESCRITORIO_INFORME=<archivo>` anota ahi, una por linea, las pantallas que muestra y las paginas
//! del producto que termina de cargar (sin secretos). Lo usa el CI para probar que arranca y carga el producto.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod navegacion;
mod preferencias;
mod producto;
mod salud;
mod vinculo;

use navegacion::{clasificar, Destino};
use producto::{Producto, MARCA_SVG, NOMBRE, PRODUCTO, VERSION};
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
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
}

impl Pantalla {
    fn nombre(self) -> &'static str {
        match self {
            Pantalla::Inicio => "inicio",
            Pantalla::Espera => "espera",
            Pantalla::Vincular => "vincular",
            Pantalla::Vinculando => "vinculando",
            Pantalla::Producto => "producto",
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
}

impl Compartido {
    fn pantalla(&self) -> Pantalla {
        *self.pantalla.lock().unwrap()
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
}

#[tauri::command]
fn estado(compartido: State<'_, Arc<Compartido>>) -> EstadoParaPagina {
    EstadoParaPagina {
        producto: PRODUCTO,
        nombre: NOMBRE,
        version: VERSION,
        marca: MARCA_SVG,
        pantalla: compartido.pantalla(),
        url: compartido.producto.url(),
        configurado: compartido.producto.configurado(),
        windows: cfg!(windows),
    }
}

#[tauri::command]
async fn vincular(compartido: State<'_, Arc<Compartido>>, direccion: String, codigo: String) -> Result<String, String> {
    let compartido = compartido.inner().clone();
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
    let Ok(url) = Url::parse(&compartido.producto.url()) else { return };
    if let Ok(cookies) = ventana.cookies_for_url(url) {
        preferencias::guardar_sesion(&compartido.sesion, &preferencias::de_sesion(&cookies));
    }
}

fn reponer_sesion(ventana: &WebviewWindow, compartido: &Compartido) {
    for guardada in preferencias::leer_sesion(&compartido.sesion).unwrap_or_default() {
        let mut cookie = preferencias::a_cookie(&guardada);
        if cookie.domain().is_none() {
            // Sin punto adelante es una cookie del host (como la que puso el producto), no de dominio.
            cookie.set_domain("localhost");
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
    loop {
        let Some(ventana) = app.get_webview_window(PRINCIPAL) else { return };
        let arriba = salud::responde(producto.puerto, &producto.salud, Duration::from_secs(3));
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
                        if clasificar(&url, producto.puerto) == Destino::Producto {
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
                let guardada = compartido.ultima_url.lock().unwrap().take();
                if let Some(url) = guardada.or_else(|| Url::parse(&producto.url()).ok()) {
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
            }
        }
        let pausa = if actual == Pantalla::Producto { Duration::from_secs(3) } else { Duration::from_millis(1500) };
        std::thread::sleep(pausa);
    }
}

/// Adentro solo el producto y las pantallas propias; lo externo, al navegador del sistema.
fn navegar(compartido: &Compartido, url: &Url, puerto: u16) -> bool {
    match clasificar(url, puerto) {
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
fn ventana_nueva(app: &AppHandle, url: Url, puerto: u16, contador: &AtomicUsize) -> NewWindowResponse<tauri::Wry> {
    match clasificar(&url, puerto) {
        Destino::Producto => {
            let etiqueta = format!("ventana-{}", contador.fetch_add(1, Ordering::Relaxed) + 1);
            let _ = WebviewWindowBuilder::new(app, etiqueta, WebviewUrl::External(url))
                .title(NOMBRE)
                .inner_size(1000.0, 760.0)
                .initialization_script(SCRIPT_F11)
                .on_navigation(move |u| match clasificar(u, puerto) {
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
    let puerto = compartido.producto.puerto;
    let app_nueva = app.clone();
    let c_carga = compartido.clone();
    let c_nav = compartido.clone();
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
        .on_navigation(move |url| navegar(&c_nav, url, puerto))
        .on_new_window(move |url, _features| ventana_nueva(&app_nueva, url, puerto, &contador))
        .on_download(move |_webview, evento| descargar(evento, descargas.as_deref()))
        .on_page_load(move |_ventana, carga| {
            if carga.event() == PageLoadEvent::Finished && clasificar(carga.url(), puerto) == Destino::Producto {
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

    if args.iter().any(|a| a == "--version") {
        println!(
            "{NOMBRE} {VERSION} (LibraEdge, version de escritorio; producto {PRODUCTO}, puerto {})",
            producto.puerto
        );
        return;
    }
    #[cfg(windows)]
    if args.iter().any(|a| a == "--vincular-elevado") {
        let direccion = valor(&args, "--direccion").unwrap_or_default();
        let archivo = PathBuf::from(valor(&args, "--archivo-codigo").unwrap_or_default());
        std::process::exit(vinculo::vincular_elevado(&producto, &direccion, &archivo));
    }
    if args.iter().any(|a| a == "--vincular") {
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
            .invoke_handler(tauri::generate_handler![estado, vincular, alternar_pantalla_completa])
            .setup(move |app| {
                let config = app.path().app_config_dir()?;
                let datos = app.path().app_local_data_dir()?;
                let compartido = Arc::new(Compartido {
                    producto: producto.clone(),
                    pantalla: Mutex::new(Pantalla::Inicio),
                    ultima_url: Mutex::new(None),
                    informe: informe.clone(),
                    preferencias: config.join("preferencias.json"),
                    sesion: datos.join("sesion.json"),
                });
                let mut prefs = preferencias::leer(&compartido.preferencias);
                if let Some(valor) = forzar {
                    prefs.pantalla_completa = valor;
                    preferencias::guardar(&compartido.preferencias, &prefs);
                }
                // La pagina del producto solo puede pedir la pantalla completa (F11).
                let mut remota = tauri::ipc::CapabilityBuilder::new("producto").window(PRINCIPAL);
                for host in ["localhost", "127.0.0.1"] {
                    remota = remota.remote(format!("http://{host}:{}/*", producto.puerto));
                }
                if let Err(e) = app.add_capability(remota.permission("allow-alternar-pantalla-completa")) {
                    eprintln!("Sin F11 en la pagina del producto: {e}");
                }
                app.manage(compartido.clone());
                compartido.anotar(&format!("inicio {PRODUCTO} {VERSION}"));
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
                        abrir_en_el_navegador(&compartido.producto.url());
                        app.handle().exit(0);
                    }
                }
                Ok(())
            })
            .run(tauri::generate_context!())
    }));
    if !matches!(resultado, Ok(Ok(()))) {
        eprintln!("No se pudo abrir la ventana de {NOMBRE}; se abre en el navegador.");
        abrir_en_el_navegador(&url);
    }
}
