//! La caja secundaria encuentra la PC principal en la red del local (ADR-017).
//!
//! Manda un pedido por UDP al puerto 47823 en broadcast (`255.255.255.255` y el broadcast de la red /24 de esta PC:
//! en Windows el primero sale por una sola interfaz) y junta las respuestas de los nodos **de su producto** durante
//! un par de segundos. El que contesta es `libraedge/descubrimiento.py` en la PC principal; los dos lados tienen
//! que decir lo mismo (`tests/test_descubrimiento.py` compara las constantes).
//!
//! - El pedido trae relleno hasta 512 bytes: el nodo no contesta pedidos mas cortos, asi no sirve para amplificar.
//! - La IP que se usa es la de **quien contesto**, nunca un dato de la respuesta: una respuesta no puede mandar la
//!   caja a otro lado. Y solo se aceptan IPv4 privadas, de enlace local o loopback.
//! - Nada de lo que viaja es secreto: producto, nombre, puerto, el identificador del nodo, la sucursal y el negocio.

use serde::{Deserialize, Serialize};
use std::net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket};
use std::time::{Duration, Instant};

pub const PUERTO: u16 = 47823;
pub const PEDIDO: &str = "descubrir/1";
pub const RESPUESTA: &str = "nodo/1";
pub const TAMANO_DEL_PEDIDO: usize = 512;
const TAMANO_MAXIMO: usize = 2048;

/// Una PC principal que contesto.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Nodo {
    /// De quien vino la respuesta (no de la respuesta).
    pub ip: String,
    pub puerto: u16,
    #[serde(default)]
    pub nodo: String,
    #[serde(default)]
    pub sucursal: String,
    #[serde(default)]
    pub negocio: String,
}

#[derive(Deserialize)]
struct Respuesta {
    libraedge: String,
    producto: String,
    puerto: u16,
    #[serde(default)]
    nodo: String,
    #[serde(default)]
    sucursal: String,
    #[serde(default)]
    negocio: String,
}

/// A quien se le cree: IPv4 privada (10/8, 172.16/12, 192.168/16), de enlace local o loopback.
pub fn ip_permitida(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => (v4.is_private() || v4.is_link_local() || v4.is_loopback()) && !v4.is_broadcast(),
        IpAddr::V6(v6) => v6.to_ipv4_mapped().is_some_and(|v4| ip_permitida(IpAddr::V4(v4))),
    }
}

/// El pedido, con relleno de espacios hasta [`TAMANO_DEL_PEDIDO`] (JSON los admite al final).
pub fn pedido(producto: &str) -> Vec<u8> {
    let mut cuerpo = serde_json::json!({ "libraedge": PEDIDO, "producto": producto }).to_string().into_bytes();
    if cuerpo.len() < TAMANO_DEL_PEDIDO {
        cuerpo.resize(TAMANO_DEL_PEDIDO, b' ');
    }
    cuerpo
}

/// Una respuesta valida de un nodo de `producto`, recibida de `de`.
pub fn leer_respuesta(datos: &[u8], producto: &str, de: IpAddr) -> Option<Nodo> {
    if !ip_permitida(de) || datos.len() > TAMANO_MAXIMO {
        return None;
    }
    let r: Respuesta = serde_json::from_slice(datos).ok()?;
    if r.libraedge != RESPUESTA || r.producto != producto || r.puerto == 0 {
        return None;
    }
    let ip = match de {
        IpAddr::V6(v6) => v6.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(de),
        otra => otra,
    };
    Some(Nodo { ip: ip.to_string(), puerto: r.puerto, nodo: r.nodo, sucursal: r.sucursal, negocio: r.negocio })
}

/// La IPv4 de esta PC en la ruta por defecto (conectar un socket UDP no manda nada).
pub fn ip_local() -> Option<Ipv4Addr> {
    let sock = UdpSocket::bind("0.0.0.0:0").ok()?;
    sock.connect("8.8.8.8:80").ok()?;
    match sock.local_addr().ok()?.ip() {
        IpAddr::V4(v4) if !v4.is_unspecified() => Some(v4),
        _ => None,
    }
}

/// A donde se manda el pedido: el broadcast general y el de la red /24 de esta PC.
pub fn destinos_broadcast(puerto: u16) -> Vec<SocketAddr> {
    let mut destinos = vec![SocketAddr::from((Ipv4Addr::BROADCAST, puerto))];
    if let Some(ip) = ip_local() {
        let [a, b, c, _] = ip.octets();
        destinos.push(SocketAddr::from((Ipv4Addr::new(a, b, c, 255), puerto)));
    }
    destinos
}

/// Busca los nodos de `producto`: manda el pedido a `destinos` y junta respuestas durante `espera`. Sin repetir
/// el nodo (contesta una vez por cada broadcast que le llega).
pub fn buscar(producto: &str, destinos: &[SocketAddr], espera: Duration) -> Vec<Nodo> {
    let mut encontrados: Vec<Nodo> = Vec::new();
    let Ok(sock) = UdpSocket::bind("0.0.0.0:0") else { return encontrados };
    let _ = sock.set_broadcast(true);
    let pedido = pedido(producto);
    for destino in destinos {
        let _ = sock.send_to(&pedido, destino);
    }
    let limite = Instant::now() + espera;
    let mut buf = [0u8; TAMANO_MAXIMO + 1];
    while let Some(resto) = limite.checked_duration_since(Instant::now()).filter(|d| !d.is_zero()) {
        let _ = sock.set_read_timeout(Some(resto));
        match sock.recv_from(&mut buf) {
            Ok((n, de)) => {
                if let Some(nodo) = leer_respuesta(&buf[..n], producto, de.ip()) {
                    let repetido = encontrados.iter().any(|e| {
                        if nodo.nodo.is_empty() { e.ip == nodo.ip && e.puerto == nodo.puerto } else { e.nodo == nodo.nodo }
                    });
                    if !repetido {
                        encontrados.push(nodo);
                    }
                }
            }
            Err(e) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => break,
            // En Windows un ICMP «puerto inalcanzable» llega como error del recv (WSAECONNRESET): se sigue.
            Err(_) => continue,
        }
    }
    encontrados
}

/// Lo que se escribe a mano cuando no se encontro la PC principal: `192.168.0.10`, `192.168.0.10:8000` o
/// `http://192.168.0.10:8000/` (lo que muestran la bandeja y el cartel final). Solo una IPv4 de la red del local.
pub fn parsear_direccion(texto: &str) -> Option<(Ipv4Addr, Option<u16>)> {
    let mut t = texto.trim();
    t = t.strip_prefix("http://").unwrap_or(t);
    t = t.trim_end_matches('/');
    let (ip, puerto) = match t.split_once(':') {
        Some((ip, p)) => (ip, Some(p.parse::<u16>().ok().filter(|p| *p != 0)?)),
        None => (t, None),
    };
    let ip: Ipv4Addr = ip.parse().ok()?;
    (ip_permitida(IpAddr::V4(ip)) && !ip.is_loopback()).then_some((ip, puerto))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    fn respuesta(producto: &str, nodo: &str) -> Vec<u8> {
        serde_json::json!({"libraedge": RESPUESTA, "producto": producto, "nombre": "ContaLibra", "puerto": 8000,
                           "nodo": nodo, "sucursal": "Casa central", "negocio": "Comercio de Prueba"})
        .to_string()
        .into_bytes()
    }

    #[test]
    fn el_pedido_trae_relleno_hasta_512() {
        let p = pedido("contalibra");
        assert_eq!(p.len(), TAMANO_DEL_PEDIDO);
        let v: serde_json::Value = serde_json::from_slice(&p).unwrap();
        assert_eq!((v["libraedge"].as_str(), v["producto"].as_str()), (Some(PEDIDO), Some("contalibra")));
    }

    #[test]
    fn la_ip_es_la_de_quien_contesto_y_solo_de_la_red_del_local() {
        let lan: IpAddr = "192.168.0.10".parse().unwrap();
        let n = leer_respuesta(&respuesta("contalibra", "nodo-1-abc"), "contalibra", lan).unwrap();
        assert_eq!((n.ip.as_str(), n.puerto, n.nodo.as_str()), ("192.168.0.10", 8000, "nodo-1-abc"));
        assert_eq!(n.sucursal, "Casa central");
        for afuera in ["8.8.8.8", "200.45.1.1", "255.255.255.255", "::1"] {
            assert!(leer_respuesta(&respuesta("contalibra", "x"), "contalibra", afuera.parse().unwrap()).is_none());
        }
        let mapeada: IpAddr = "::ffff:10.0.0.5".parse().unwrap();
        assert_eq!(leer_respuesta(&respuesta("contalibra", "x"), "contalibra", mapeada).unwrap().ip, "10.0.0.5");
    }

    #[test]
    fn otro_producto_u_otra_cosa_no_cuenta() {
        let lan: IpAddr = "10.0.0.2".parse().unwrap();
        assert!(leer_respuesta(&respuesta("ventalibra", "x"), "contalibra", lan).is_none());
        assert!(leer_respuesta(b"basura", "contalibra", lan).is_none());
        assert!(leer_respuesta(br#"{"libraedge": "otro", "producto": "contalibra", "puerto": 8000}"#, "contalibra", lan)
            .is_none());
        assert!(leer_respuesta(br#"{"libraedge": "nodo/1", "producto": "contalibra", "puerto": 0}"#, "contalibra", lan)
            .is_none());
    }

    #[test]
    fn la_direccion_escrita_a_mano() {
        let ip = Ipv4Addr::new(192, 168, 0, 10);
        assert_eq!(parsear_direccion("192.168.0.10"), Some((ip, None)));
        assert_eq!(parsear_direccion(" 192.168.0.10:8000 "), Some((ip, Some(8000))));
        assert_eq!(parsear_direccion("http://192.168.0.10:8000/"), Some((ip, Some(8000))));
        assert_eq!(parsear_direccion("172.16.5.4"), Some((Ipv4Addr::new(172, 16, 5, 4), None)));
        for mala in ["", "8.8.8.8", "127.0.0.1", "192.168.0.10:0", "192.168.0.10:x", "sistema.com.ar", "192.168.0"] {
            assert_eq!(parsear_direccion(mala), None, "{mala}");
        }
    }

    #[test]
    fn busca_y_junta_las_respuestas_sin_repetir() {
        // Un nodo de prueba en loopback que contesta dos veces (como con dos broadcasts) y otro producto.
        let nodo = UdpSocket::bind("127.0.0.1:0").unwrap();
        let destino = nodo.local_addr().unwrap();
        let hilo = thread::spawn(move || {
            let mut buf = [0u8; 1024];
            let (n, de) = nodo.recv_from(&mut buf).unwrap();
            let pedido: serde_json::Value = serde_json::from_slice(&buf[..n]).unwrap();
            assert_eq!(n, TAMANO_DEL_PEDIDO);
            let producto = pedido["producto"].as_str().unwrap().to_string();
            nodo.send_to(&respuesta(&producto, "nodo-1-abc"), de).unwrap();
            nodo.send_to(&respuesta(&producto, "nodo-1-abc"), de).unwrap();
            nodo.send_to(&respuesta("ventalibra", "nodo-2"), de).unwrap();
        });
        let encontrados = buscar("contalibra", &[destino], Duration::from_millis(800));
        hilo.join().unwrap();
        assert_eq!(encontrados.len(), 1);
        assert_eq!((encontrados[0].ip.as_str(), encontrados[0].nodo.as_str()), ("127.0.0.1", "nodo-1-abc"));
    }

    #[test]
    fn sin_nadie_no_encuentra_nada() {
        let puerto = UdpSocket::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert!(buscar("contalibra", &[SocketAddr::from(([127, 0, 0, 1], puerto))], Duration::from_millis(300))
            .is_empty());
        assert_eq!(destinos_broadcast(PUERTO)[0], SocketAddr::from((Ipv4Addr::BROADCAST, PUERTO)));
    }
}
