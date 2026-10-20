//! La PC principal que eligio esta caja secundaria (ADR-017), recordada en `principal.json` (carpeta de
//! configuracion del usuario, la de la app en modo caja). No tiene secretos: la IP, el puerto y lo que contesto el
//! nodo (su identificador, la sucursal y el negocio). El identificador sirve para reconocer la misma PC principal si
//! cambio de IP: la caja vuelve a buscar y se queda solo con ese nodo.

use crate::descubrimiento::Nodo;
use crate::navegacion::Origen;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Principal {
    pub ip: String,
    pub puerto: u16,
    /// El identificador del nodo; vacio si se escribio la IP a mano y el nodo no contesto el descubrimiento.
    #[serde(default)]
    pub nodo: String,
    #[serde(default)]
    pub sucursal: String,
    #[serde(default)]
    pub negocio: String,
}

impl Principal {
    pub fn origen(&self) -> Origen {
        Origen { host: self.ip.clone(), puerto: self.puerto }
    }

    /// ¿Es esta PC principal la que contesto (la misma, aunque tenga otra IP)? Sin identificador guardado, se
    /// reconoce solo por la direccion.
    pub fn es(&self, nodo: &Nodo) -> bool {
        if self.nodo.is_empty() || nodo.nodo.is_empty() {
            self.ip == nodo.ip && self.puerto == nodo.puerto
        } else {
            self.nodo == nodo.nodo
        }
    }
}

impl From<&Nodo> for Principal {
    fn from(n: &Nodo) -> Self {
        Principal {
            ip: n.ip.clone(),
            puerto: n.puerto,
            nodo: n.nodo.clone(),
            sucursal: n.sucursal.clone(),
            negocio: n.negocio.clone(),
        }
    }
}

pub fn leer(archivo: &Path) -> Option<Principal> {
    let p: Principal = serde_json::from_str(&std::fs::read_to_string(archivo).ok()?).ok()?;
    // Lo que se lee de disco se valida como lo escrito a mano: una IPv4 de la red del local.
    crate::descubrimiento::parsear_direccion(&p.ip).filter(|_| p.puerto != 0).map(|_| p)
}

pub fn guardar(archivo: &Path, principal: &Principal) {
    if let Some(dir) = archivo.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    let _ = crate::preferencias::escribir_privado(archivo, &serde_json::to_string_pretty(principal).unwrap_or_default());
}

pub fn olvidar(archivo: &Path) {
    let _ = std::fs::remove_file(archivo);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn nodo(ip: &str, id: &str) -> Nodo {
        Nodo { ip: ip.into(), puerto: 8000, nodo: id.into(), sucursal: "Centro".into(), negocio: "N".into() }
    }

    #[test]
    fn se_recuerda_y_se_olvida() {
        let dir = std::env::temp_dir().join(format!("libraedge-principal-test-{}", std::process::id()));
        let archivo = dir.join("principal.json");
        let p = Principal::from(&nodo("192.168.0.10", "nodo-1-abc"));
        guardar(&archivo, &p);
        assert_eq!(leer(&archivo), Some(p.clone()));
        assert_eq!(p.origen().url(), "http://192.168.0.10:8000/");
        olvidar(&archivo);
        assert_eq!(leer(&archivo), None);
        // Una IP de afuera en el archivo no se usa.
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(&archivo, r#"{"ip": "8.8.8.8", "puerto": 8000}"#).unwrap();
        assert_eq!(leer(&archivo), None);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn la_misma_pc_principal_con_otra_ip() {
        let p = Principal::from(&nodo("192.168.0.10", "nodo-1-abc"));
        assert!(p.es(&nodo("192.168.0.23", "nodo-1-abc")));
        assert!(!p.es(&nodo("192.168.0.10", "nodo-2-def")));
        let a_mano = Principal { nodo: String::new(), ..p };
        assert!(a_mano.es(&nodo("192.168.0.10", "nodo-9")));
        assert!(!a_mano.es(&nodo("192.168.0.11", "nodo-9")));
    }
}
