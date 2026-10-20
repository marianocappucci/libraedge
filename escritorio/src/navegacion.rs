//! Que se abre adentro de la app y que en el navegador del sistema.
//!
//! Adentro: el producto local (`localhost` o `127.0.0.1` en su puerto) y las pantallas propias de la app.
//! Afuera, en el navegador del sistema: todo lo demas que sea web o un enlace de otra aplicacion (ARCA,
//! Mercado Pago, las ayudas, `mailto:`, WhatsApp). Lo que no es ninguna de las dos cosas (`file:`,
//! `javascript:`) no se abre.

use tauri::Url;

#[derive(Debug, PartialEq, Eq)]
pub enum Destino {
    /// La copia local del producto.
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

pub fn clasificar(url: &Url, puerto: u16) -> Destino {
    match url.scheme() {
        "http" | "https" => {
            let host = url.host_str().unwrap_or_default();
            if url.scheme() == "http" && (host == "localhost" || host == "127.0.0.1") && url.port() == Some(puerto) {
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
        clasificar(&Url::parse(texto).unwrap(), 8000)
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
        assert_eq!(clasificar(&pagina("espera"), 8000), Destino::Paginas);
    }
}
