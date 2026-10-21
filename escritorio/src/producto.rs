//! Lo que la app sabe del producto y del nodo de esta PC.
//!
//! Lo fijo (nombre, producto) viene del build; el puerto y la ruta de salud se releen de la definicion
//! instalada (`<raiz>/instalador/producto.json`, la misma que usa el instalador) por si cambiaran sin
//! recompilar la app. La raiz del nodo es la carpeta de arriba de la del ejecutable, en los dos sistemas:
//!
//! - Windows: `C:\Program Files\LibraEdge\escritorio\<Producto>.exe` -> `C:\Program Files\LibraEdge`
//! - Ubuntu:  `/opt/libraedge-nodo/escritorio/libraedge-nodo-<producto>` -> `/opt/libraedge-nodo`
//!
//! **El modo sale de ahi** (ADR-017): si en la raiz esta la definicion instalada (`instalador/producto.json`) la app es
//! la de la PC principal (modo nodo: `localhost`, «Vincular»); si no, es la de un puesto secundario (modo puesto: busca la
//! PC principal en la red del local). Es el mismo binario: el instalador de puesto lo pone en otra carpeta
//! (`C:\Program Files\<Producto> (puesto)\<Producto>.exe`, `/opt/libraedge-puesto/<producto>/libraedge-puesto-<producto>`).

use std::path::{Path, PathBuf};

pub const PRODUCTO: &str = env!("LIBRAEDGE_PRODUCTO");
pub const NOMBRE: &str = env!("LIBRAEDGE_NOMBRE");
pub const VERSION: &str = env!("LIBRAEDGE_VERSION");
const PUERTO: &str = env!("LIBRAEDGE_PUERTO");
const SALUD: &str = env!("LIBRAEDGE_SALUD");
/// La marca del producto, para las pantallas propias (la copia el build de `instalador/marca/<producto>.svg`).
pub const MARCA_SVG: &str = include_str!(concat!(env!("OUT_DIR"), "/marca.svg"));

/// Donde corre la app.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Modo {
    /// La PC principal: el producto es la copia local del nodo.
    Nodo,
    /// Un puesto secundario: el producto es el de la PC principal, que se busca en la red del local.
    Puesto,
}

#[derive(Clone, Debug)]
pub struct Producto {
    pub puerto: u16,
    pub salud: String,
    pub raiz: Option<PathBuf>,
    pub modo: Modo,
}

impl Producto {
    pub fn detectar() -> Self {
        // En Linux se resuelven los enlaces; en Windows no: canonicalize devuelve `\\?\C:\...`, que PowerShell 5.1
        // no acepta en `-File` (la vinculacion elevada fallaba sin abrir el transcript, CI 2026-10-20).
        match std::env::current_exe() {
            Ok(exe) if cfg!(windows) => Self::en(&exe),
            Ok(exe) => Self::en(&exe.canonicalize().unwrap_or(exe)),
            Err(_) => {
                Producto { puerto: PUERTO.parse().unwrap_or(8000), salud: SALUD.to_string(), raiz: None, modo: Modo::Puesto }
            }
        }
    }

    /// Lo que se sabe con el ejecutable en `exe`.
    pub fn en(exe: &Path) -> Self {
        let raiz = exe.parent().and_then(Path::parent).map(Path::to_path_buf);
        let mut producto =
            Producto { puerto: PUERTO.parse().unwrap_or(8000), salud: SALUD.to_string(), raiz, modo: Modo::Puesto };
        if let Some(def) = producto.raiz.as_ref().map(|r| r.join("instalador").join("producto.json")) {
            if def.is_file() {
                producto.modo = Modo::Nodo;
            }
            if let Ok(texto) = std::fs::read_to_string(def) {
                producto.aplicar_definicion(&texto);
            }
        }
        producto
    }

    /// Toma puerto y salud de la definicion instalada, si es del mismo producto.
    pub fn aplicar_definicion(&mut self, texto: &str) {
        let Ok(def) = serde_json::from_str::<serde_json::Value>(texto) else { return };
        if def["producto"].as_str() != Some(PRODUCTO) {
            return;
        }
        if let Some(p) = def["puerto"].as_u64().and_then(|p| u16::try_from(p).ok()) {
            self.puerto = p;
        }
        if let Some(s) = def["salud"].as_str().filter(|s| s.starts_with('/')) {
            self.salud = s.to_string();
        }
    }

    /// Por donde se entra en esta PC (ADR-013): siempre la copia local, con `localhost` (otro host es otra sesion).
    pub fn url(&self) -> String {
        format!("http://localhost:{}/", self.puerto)
    }

    /// El archivo que existe solo cuando el nodo quedo configurado (tiene el secreto: la app no lo lee, solo
    /// mira si esta). Linux: `/etc/libraedge-nodo/nodo.env`; Windows: `<raiz>\nodo.env`.
    pub fn archivo_de_configuracion(&self) -> Option<PathBuf> {
        if cfg!(windows) {
            self.raiz.as_ref().map(|r| r.join("nodo.env"))
        } else {
            Some(PathBuf::from("/etc/libraedge-nodo/nodo.env"))
        }
    }

    /// ¿Esta PC ya esta vinculada? Si no se puede saber (sin permiso para mirar), se asume que si: la app
    /// muestra la espera y no ofrece vincular algo que ya esta.
    pub fn configurado(&self) -> bool {
        if self.modo == Modo::Puesto {
            return true; // un puesto no tiene nodo que vincular
        }
        match self.archivo_de_configuracion() {
            Some(ruta) => ruta.try_exists().unwrap_or(true),
            None => true,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn base() -> Producto {
        Producto { puerto: 8000, salud: "/health".into(), raiz: None, modo: Modo::Nodo }
    }

    #[test]
    fn la_definicion_instalada_cambia_puerto_y_salud() {
        let mut p = base();
        p.aplicar_definicion(&format!(r#"{{"producto": "{PRODUCTO}", "puerto": 8100, "salud": "/api/salud"}}"#));
        assert_eq!((p.puerto, p.salud.as_str()), (8100, "/api/salud"));
        assert_eq!(p.url(), "http://localhost:8100/");
    }

    #[test]
    fn una_definicion_de_otro_producto_o_rota_no_cambia_nada() {
        let mut p = base();
        p.aplicar_definicion(r#"{"producto": "otro", "puerto": 9}"#);
        p.aplicar_definicion("no es json");
        p.aplicar_definicion(&format!(r#"{{"producto": "{PRODUCTO}", "puerto": 99999, "salud": "sin-barra"}}"#));
        assert_eq!((p.puerto, p.salud.as_str()), (8000, "/health"));
    }

    #[test]
    fn sin_la_definicion_instalada_es_una_puesto() {
        let dir = std::env::temp_dir().join(format!("libraedge-modo-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("escritorio")).unwrap();
        let exe = dir.join("escritorio").join("app");
        assert_eq!(Producto::en(&exe).modo, Modo::Puesto);
        std::fs::create_dir_all(dir.join("instalador")).unwrap();
        std::fs::write(dir.join("instalador").join("producto.json"), "{}").unwrap();
        assert_eq!(Producto::en(&exe).modo, Modo::Nodo);
        let puesto = Producto { modo: Modo::Puesto, ..base() };
        assert!(puesto.configurado());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
