//! Que se abre adentro de la app y que en el navegador del sistema.
//!
//! Adentro: el producto (su origen: en la PC principal, `localhost` o `127.0.0.1` en su puerto; en un puesto
//! secundaria, la IP de la PC principal en su puerto, ADR-017) y las pantallas propias de la app.
//! Afuera, en el navegador del sistema: todo lo demas que sea web o un enlace de otra aplicacion (ARCA,
//! Mercado Pago, las ayudas, `mailto:`, WhatsApp). Lo que no es ninguna de las dos cosas (`file:`,
//! `javascript:`) no se abre.

use tauri::Url;

/// De donde se sirve el producto: `http://<host>:<puerto>/`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Origen {
    pub host: String,
    pub puerto: u16,
}

impl Origen {
    /// La copia local de la PC principal (ADR-013): siempre `localhost` (otro host es otra sesion).
    pub fn local(puerto: u16) -> Self {
        Origen { host: "localhost".into(), puerto }
    }

    pub fn url(&self) -> String {
        format!("http://{}:{}/", self.host, self.puerto)
    }

    /// Los hosts que son el producto: el del origen y, en la PC principal, tambien `127.0.0.1`.
    pub fn hosts(&self) -> Vec<String> {
        if self.host == "localhost" {
            vec!["localhost".into(), "127.0.0.1".into()]
        } else {
            vec![self.host.clone()]
        }
    }

    fn es(&self, url: &Url) -> bool {
        url.scheme() == "http"
            && url.port() == Some(self.puerto)
            && self.hosts().iter().any(|h| url.host_str() == Some(h.as_str()))
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum Destino {
    /// El producto (la copia local o la PC principal).
    Producto,
    /// Las pantallas propias de la app (espera, vincular).
    Paginas,
    /// `about:blank`, `data:` y `blob:` (descargas y vistas previas que arma el producto).
    Interno,
    /// Se abre en el navegador del sistema.
    Externo,
    /// No se abre.
    Bloqueado,
}

pub fn clasificar(url: &Url, origen: &Origen) -> Destino {
    match url.scheme() {
        "http" | "https" => {
            let host = url.host_str().unwrap_or_default();
            if origen.es(url) {
                Destino::Producto
            } else if host == "tauri.localhost" {
                Destino::Paginas
            } else {
                Destino::Externo
            }
        }
        "tauri" => Destino::Paginas,
        "about" | "data" | "blob" => Destino::Interno,
        "mailto" | "tel" | "whatsapp" => Destino::Externo,
        _ => Destino::Bloqueado,
    }
}

/// La direccion de una pantalla propia (`index.html?p=<pantalla>`), servida por la app.
pub fn pagina(pantalla: &str) -> Url {
    let base = if cfg!(windows) { "http://tauri.localhost/" } else { "tauri://localhost/" };
    Url::parse(&format!("{base}index.html?p={pantalla}")).expect("url de pagina")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn de(texto: &str) -> Destino {
        clasificar(&Url::parse(texto).unwrap(), &Origen::local(8000))
    }

    fn de_puesto(texto: &str) -> Destino {
        clasificar(&Url::parse(texto).unwrap(), &Origen { host: "192.168.0.10".into(), puerto: 8000 })
    }

    #[test]
    fn el_producto_local_queda_adentro() {
        assert_eq!(de("http://localhost:8000/"), Destino::Producto);
        assert_eq!(de("http://localhost:8000/ventas?x=1#y"), Destino::Producto);
        assert_eq!(de("http://127.0.0.1:8000/api/health"), Destino::Producto);
    }

    #[test]
    fn otro_puerto_otro_host_o_https_van_al_navegador() {
        assert_eq!(de("http://localhost:8001/"), Destino::Externo);
        assert_eq!(de("https://localhost:8000/"), Destino::Externo);
        assert_eq!(de("http://192.168.0.10:8000/"), Destino::Externo);
        assert_eq!(de("https://auth.afip.gob.ar/contribuyente_/login.xhtml"), Destino::Externo);
        assert_eq!(de("https://www.mercadopago.com.ar/"), Destino::Externo);
        assert_eq!(de("https://demo.contalibra.com.ar/"), Destino::Externo);
        assert_eq!(de("mailto:soporte@example.com"), Destino::Externo);
        assert_eq!(de("whatsapp://send?text=hola"), Destino::Externo);
    }

    #[test]
    fn las_pantallas_propias_y_lo_interno() {
        assert_eq!(de("tauri://localhost/index.html?p=espera"), Destino::Paginas);
        assert_eq!(de("http://tauri.localhost/index.html"), Destino::Paginas);
        assert_eq!(de("about:blank"), Destino::Interno);
        assert_eq!(de("blob:http://localhost:8000/1234"), Destino::Interno);
        assert_eq!(de("file:///etc/passwd"), Destino::Bloqueado);
        assert_eq!(de("javascript:alert(1)"), Destino::Bloqueado);
        assert_eq!(clasificar(&pagina("espera"), &Origen::local(8000)), Destino::Paginas);
    }

    #[test]
    fn en_una_puesto_el_producto_es_la_pc_principal_y_localhost_no() {
        assert_eq!(de_puesto("http://192.168.0.10:8000/"), Destino::Producto);
        assert_eq!(de_puesto("http://192.168.0.10:8000/ventas?x=1"), Destino::Producto);
        assert_eq!(de_puesto("http://localhost:8000/"), Destino::Externo);
        assert_eq!(de_puesto("http://127.0.0.1:8000/"), Destino::Externo);
        assert_eq!(de_puesto("http://192.168.0.10:8001/"), Destino::Externo);
        assert_eq!(de_puesto("https://192.168.0.10:8000/"), Destino::Externo);
        assert_eq!(de_puesto("http://192.168.0.11:8000/"), Destino::Externo);
        assert_eq!(de_puesto("http://192.168.0.100:8000/"), Destino::Externo);
        let o = Origen { host: "192.168.0.10".into(), puerto: 8000 };
        assert_eq!((o.url(), o.hosts()), ("http://192.168.0.10:8000/".to_string(), vec!["192.168.0.10".to_string()]));
    }
}
