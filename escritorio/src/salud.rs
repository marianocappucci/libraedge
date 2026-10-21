//! ¿Responde el producto? Un GET a la ruta de salud de la definicion, con plazos cortos.
//!
//! En la PC principal va a `127.0.0.1` y no a `localhost`: el producto escucha en IPv4 (`--host 0.0.0.0`) y
//! `localhost` puede resolver primero a `::1`. En un puesto secundario (ADR-017), a la IP de la PC principal. Sin
//! dependencias: es una sola linea de HTTP/1.0.

use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream};
use std::time::Duration;

/// El producto local de la PC principal.
pub fn responde(puerto: u16, ruta: &str, plazo: Duration) -> bool {
    responde_en(IpAddr::V4(Ipv4Addr::LOCALHOST), "localhost", puerto, ruta, plazo)
}

/// El producto en `ip:puerto`, con `host` en la cabecera `Host`.
pub fn responde_en(ip: IpAddr, host: &str, puerto: u16, ruta: &str, plazo: Duration) -> bool {
    let direccion = SocketAddr::new(ip, puerto);
    let Ok(mut conexion) = TcpStream::connect_timeout(&direccion, plazo) else { return false };
    let _ = conexion.set_read_timeout(Some(plazo));
    let _ = conexion.set_write_timeout(Some(plazo));
    let pedido = format!("GET {ruta} HTTP/1.0\r\nHost: {host}:{puerto}\r\nUser-Agent: libraedge-escritorio\r\n\r\n");
    if conexion.write_all(pedido.as_bytes()).is_err() {
        return false;
    }
    let mut inicio = [0u8; 16];
    let mut leidos = 0;
    while leidos < inicio.len() {
        match conexion.read(&mut inicio[leidos..]) {
            Ok(0) => break,
            Ok(n) => leidos += n,
            Err(_) => return false,
        }
    }
    estado_ok(&inicio[..leidos])
}

/// `HTTP/1.x 2xx` al principio de la respuesta.
fn estado_ok(inicio: &[u8]) -> bool {
    let texto = String::from_utf8_lossy(inicio);
    let mut partes = texto.split_whitespace();
    matches!((partes.next(), partes.next()), (Some(v), Some(c)) if v.starts_with("HTTP/1.") && c.starts_with('2') && c.len() == 3)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn solo_un_2xx_cuenta_como_arriba() {
        assert!(estado_ok(b"HTTP/1.1 200 OK\r\n"));
        assert!(estado_ok(b"HTTP/1.0 204 No"));
        assert!(!estado_ok(b"HTTP/1.1 503 Service"));
        assert!(!estado_ok(b"HTTP/1.1 302 Found"));
        assert!(!estado_ok(b""));
        assert!(!estado_ok(b"basura"));
    }

    #[test]
    fn pregunta_por_la_ruta_de_salud() {
        let escucha = TcpListener::bind("127.0.0.1:0").unwrap();
        let puerto = escucha.local_addr().unwrap().port();
        let servidor = thread::spawn(move || {
            let (mut c, _) = escucha.accept().unwrap();
            let mut buf = [0u8; 256];
            let n = c.read(&mut buf).unwrap();
            c.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}").unwrap();
            String::from_utf8_lossy(&buf[..n]).to_string()
        });
        assert!(responde(puerto, "/health", Duration::from_secs(2)));
        assert!(servidor.join().unwrap().starts_with("GET /health HTTP/1.0\r\n"));
    }

    #[test]
    fn sin_nadie_escuchando_no_responde() {
        let puerto = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert!(!responde(puerto, "/health", Duration::from_millis(300)));
    }
}
