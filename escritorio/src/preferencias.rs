//! Lo que la app recuerda entre aperturas, en la carpeta de configuracion del usuario (por producto):
//!
//! - `preferencias.json`: la pantalla completa (F11 o `--pantalla-completa`/`--ventana`). El tamaño y la
//!   posicion de la ventana los guarda el plugin `window-state` en su propio archivo.
//! - `sesion.json`: las cookies de sesion del producto. La de libraauth no tiene vencimiento (cookie «de
//!   sesion»), asi que el navegador embebido la descarta al cerrarse y habria que volver a entrar en cada
//!   apertura. La app la guarda mientras el producto esta abierto y la repone antes de cargarlo; el central
//!   (el producto) sigue mandando: la sesion vence igual por inactividad (8 h de libraauth) y cerrar sesion la
//!   borra. Las cookies con vencimiento ya las guarda el navegador embebido.

use cookie::{Cookie, SameSite};
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq)]
pub struct Preferencias {
    #[serde(default)]
    pub pantalla_completa: bool,
}

pub fn leer(archivo: &Path) -> Preferencias {
    std::fs::read_to_string(archivo).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
}

pub fn guardar(archivo: &Path, preferencias: &Preferencias) {
    if let Some(dir) = archivo.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = escribir_privado(archivo, &serde_json::to_string_pretty(preferencias).unwrap_or_default());
}

/// Escribe solo para el usuario (0600 en Linux; en Windows, la carpeta del perfil ya es solo suya).
pub(crate) fn escribir_privado(archivo: &Path, contenido: &str) -> std::io::Result<()> {
    let temporal = archivo.with_extension("tmp");
    {
        let mut opciones = std::fs::OpenOptions::new();
        opciones.write(true).create(true).truncate(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opciones.mode(0o600);
        }
        let mut f = opciones.open(&temporal)?;
        std::io::Write::write_all(&mut f, contenido.as_bytes())?;
    }
    std::fs::rename(&temporal, archivo)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CookieGuardada {
    nombre: String,
    valor: String,
    ruta: Option<String>,
    dominio: Option<String>,
    segura: bool,
    solo_http: bool,
    same_site: Option<String>,
}

/// Las cookies de sesion (sin vencimiento) de una lista: las que el navegador embebido no guarda solo.
pub fn de_sesion(cookies: &[Cookie<'_>]) -> Vec<CookieGuardada> {
    cookies
        .iter()
        .filter(|c| c.expires().is_none_or(|e| e.is_session()) && c.max_age().is_none())
        .map(|c| CookieGuardada {
            nombre: c.name().to_string(),
            valor: c.value().to_string(),
            ruta: c.path().map(str::to_string),
            dominio: c.domain().map(str::to_string),
            segura: c.secure().unwrap_or(false),
            solo_http: c.http_only().unwrap_or(false),
            same_site: c.same_site().map(|s| s.to_string()),
        })
        .collect()
}

pub fn a_cookie(g: &CookieGuardada) -> Cookie<'static> {
    let mut c = Cookie::new(g.nombre.clone(), g.valor.clone());
    c.set_path(g.ruta.clone().unwrap_or_else(|| "/".into()));
    if let Some(d) = &g.dominio {
        c.set_domain(d.clone());
    }
    c.set_secure(g.segura);
    c.set_http_only(g.solo_http);
    c.set_same_site(match g.same_site.as_deref() {
        Some("Strict") => Some(SameSite::Strict),
        Some("None") => Some(SameSite::None),
        Some("Lax") => Some(SameSite::Lax),
        _ => None,
    });
    c
}

/// Guarda las cookies de sesion; sin ninguna (se cerro la sesion) borra el archivo. Devuelve si cambio.
pub fn guardar_sesion(archivo: &Path, cookies: &[CookieGuardada]) -> bool {
    let anterior = leer_sesion(archivo);
    if anterior.as_deref() == Some(cookies) {
        return false;
    }
    if cookies.is_empty() {
        let _ = std::fs::remove_file(archivo);
    } else {
        if let Some(dir) = archivo.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        let _ = escribir_privado(archivo, &serde_json::to_string(cookies).unwrap_or_default());
    }
    true
}

pub fn leer_sesion(archivo: &Path) -> Option<Vec<CookieGuardada>> {
    std::fs::read_to_string(archivo).ok().and_then(|t| serde_json::from_str(&t).ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(nombre: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("libraedge-escritorio-test-{}-{nombre}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn la_pantalla_completa_se_recuerda() {
        let archivo = tmp("pref").join("preferencias.json");
        assert_eq!(leer(&archivo), Preferencias::default());
        guardar(&archivo, &Preferencias { pantalla_completa: true });
        assert!(leer(&archivo).pantalla_completa);
        std::fs::write(&archivo, "{roto").unwrap();
        assert!(!leer(&archivo).pantalla_completa);
    }

    #[test]
    fn solo_se_guardan_las_cookies_de_sesion_y_vuelven_iguales() {
        let sesion = Cookie::parse("contalibra_session=abc.123; Path=/; HttpOnly; Secure; SameSite=Lax").unwrap();
        let persistente = Cookie::parse("tema=oscuro; Path=/; Max-Age=3600").unwrap();
        let guardadas = de_sesion(&[sesion.clone(), persistente]);
        assert_eq!(guardadas.len(), 1);
        let vuelta = a_cookie(&guardadas[0]);
        assert_eq!((vuelta.name(), vuelta.value()), ("contalibra_session", "abc.123"));
        assert_eq!(
            (vuelta.http_only(), vuelta.secure(), vuelta.same_site()),
            (Some(true), Some(true), Some(SameSite::Lax))
        );
        assert_eq!(vuelta.path(), Some("/"));
    }

    #[test]
    fn sin_sesion_se_borra_el_archivo() {
        let archivo = tmp("sesion").join("sesion.json");
        let c = de_sesion(&[Cookie::parse("s=1; Path=/").unwrap()]);
        assert!(guardar_sesion(&archivo, &c));
        assert!(!guardar_sesion(&archivo, &c)); // igual: no reescribe
        assert_eq!(leer_sesion(&archivo).unwrap(), c);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&archivo).unwrap().permissions().mode() & 0o777, 0o600);
        }
        assert!(guardar_sesion(&archivo, &[]));
        assert!(!archivo.exists());
    }
}
