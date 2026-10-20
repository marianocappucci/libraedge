//! «Vincular con el sistema» desde la app (ADR-015): la direccion y el codigo de vinculacion (ADR-014) los
//! escribe el usuario en la app, que corre como el usuario; configurar el nodo es de root/Administrador. La app
//! no canjea nada ni ve la carga: le pasa direccion y codigo al MISMO configurador del instalador
//! (`instalar.sh` / `instalar.ps1`), lanzado con la elevacion del sistema, que pide la clave de un
//! administrador:
//!
//! - **Ubuntu:** `pkexec /opt/libraedge-nodo/instalador/instalar.sh --direccion D --codigo-entrada`, con el
//!   codigo por la entrada estandar (pkexec la pasa tal cual y limpia el entorno). La accion de polkit
//!   (`ar.com.librasuite.libraedge-nodo.policy`) pone el mensaje en castellano y pide `auth_admin_keep`.
//! - **Windows:** la app se relanza a si misma con el verbo `runas` (UAC muestra el nombre y el icono del
//!   producto) y `--vincular-elevado`; esa copia elevada corre `instalar.ps1 -Direccion D -ArchivoCodigo P`
//!   y sale con su codigo. El codigo viaja en un archivo del perfil del usuario (que UAC no hereda el entorno ni
//!   la entrada), que borran `instalar.ps1` apenas lo lee y la app al terminar.
//!
//! El codigo nunca va en una linea de comandos ni en un log; la direccion si (no es secreta).

use crate::producto::Producto;

/// El alfabeto del codigo (ADR-014): sin 0 O 1 I L.
const ALFABETO: &str = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

/// El codigo en mayusculas, sin espacios ni guiones, si tiene la forma de un codigo (8 del alfabeto). Como
/// el ayudante, no «corrige» nada: una `O` no pasa a `0`.
pub fn normalizar_codigo(texto: &str) -> Option<String> {
    let codigo: String =
        texto.chars().filter(|c| !matches!(c, ' ' | '-' | '\t')).map(|c| c.to_ascii_uppercase()).collect();
    (codigo.len() == 8 && codigo.chars().all(|c| ALFABETO.contains(c))).then_some(codigo)
}

/// La direccion va como argumento del configurador: solo letras, numeros y `. : / - _` (como el `.iss`).
pub fn direccion_valida(texto: &str) -> bool {
    !texto.is_empty()
        && texto.len() <= 253
        && texto.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | ':' | '/' | '-' | '_'))
}

#[derive(Debug, serde::Serialize)]
pub struct Resultado {
    pub ok: bool,
    /// Que paso, para la pantalla (sin secretos: es la salida del configurador, que no los imprime).
    pub detalle: String,
}

impl Resultado {
    fn error(detalle: impl Into<String>) -> Self {
        Resultado { ok: false, detalle: detalle.into() }
    }
}

/// Valida y lanza el configurador con privilegios. Bloquea hasta que termina (puede tardar minutos).
pub fn vincular(producto: &Producto, direccion: &str, codigo: &str) -> Resultado {
    let direccion = direccion.trim();
    if !direccion_valida(direccion) {
        return Resultado::error(
            "Escribir la dirección del sistema tal como la muestra «Sucursal sin internet» (por ejemplo sistema.contalibra.com.ar).",
        );
    }
    let Some(codigo) = normalizar_codigo(codigo) else {
        return Resultado::error("El código de vinculación tiene 8 letras y números, como K7Q4-M2XP.");
    };
    plataforma::vincular(producto, direccion, &codigo)
}

/// Las ultimas lineas de una salida, para mostrar por que fallo.
pub fn ultimas_lineas(texto: &str, cuantas: usize) -> String {
    let lineas: Vec<&str> = texto
        .lines()
        .map(|l| l.trim_matches(|c: char| c == '\u{feff}' || c == '\0' || c.is_whitespace()))
        .filter(|l| !l.is_empty() && !l.starts_with("**********"))
        .collect();
    lineas[lineas.len().saturating_sub(cuantas)..].join("\n")
}

#[cfg(not(windows))]
mod plataforma {
    use super::{ultimas_lineas, Resultado};
    use crate::producto::Producto;
    use std::io::Write;
    use std::process::{Command, Stdio};

    /// La ruta que nombra la accion de polkit (`org.freedesktop.policykit.exec.path`): tiene que ser esta,
    /// literal, para que pkexec use la accion con el mensaje propio.
    pub const CONFIGURADOR: &str = "/opt/libraedge-nodo/instalador/instalar.sh";

    pub fn vincular(_producto: &Producto, direccion: &str, codigo: &str) -> Resultado {
        let hijo = Command::new("pkexec")
            .arg(CONFIGURADOR)
            .arg("--direccion")
            .arg(direccion)
            .arg("--codigo-entrada")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn();
        let mut hijo = match hijo {
            Ok(h) => h,
            Err(e) => {
                return Resultado::error(format!(
                    "No se pudo pedir la clave de administrador (pkexec: {e}). Vincular desde una terminal: \
                     sudo libraedge-nodo-instalar --codigo <código> --direccion {direccion}"
                ))
            }
        };
        if let Some(mut entrada) = hijo.stdin.take() {
            let _ = writeln!(entrada, "{codigo}");
        }
        let salida = match hijo.wait_with_output() {
            Ok(s) => s,
            Err(e) => return Resultado::error(format!("El configurador no terminó bien: {e}")),
        };
        let texto = format!("{}{}", String::from_utf8_lossy(&salida.stdout), String::from_utf8_lossy(&salida.stderr));
        match salida.status.code() {
            Some(0) => Resultado { ok: true, detalle: ultimas_lineas(&texto, 6) },
            // pkexec: 126, se cerro el pedido de clave; 127, no autorizado o sin agente de autenticacion.
            Some(126) => Resultado::error("No se autorizó: hace falta la clave de un administrador de esta PC."),
            Some(127) if texto.trim().is_empty() || texto.contains("pkexec") || texto.contains("authoriz") => {
                Resultado::error(format!(
                    "No se pudo autorizar con la clave de administrador. Vincular desde una terminal: \
                     sudo libraedge-nodo-instalar --codigo <código> --direccion {direccion}\n{}",
                    ultimas_lineas(&texto, 4)
                ))
            }
            _ => Resultado::error(ultimas_lineas(&texto, 12)),
        }
    }
}

#[cfg(windows)]
pub mod plataforma {
    use super::{ultimas_lineas, Resultado};
    use crate::producto::Producto;
    use std::ffi::OsStr;
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::process::CommandExt;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use windows_sys::Win32::Foundation::{CloseHandle, GetLastError, ERROR_CANCELLED};
    use windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW;
    use windows_sys::Win32::System::Threading::{GetExitCodeProcess, WaitForSingleObject, INFINITE};
    use windows_sys::Win32::UI::Shell::{
        ShellExecuteExW, SEE_MASK_NOASYNC, SEE_MASK_NOCLOSEPROCESS, SHELLEXECUTEINFOW,
    };
    use windows_sys::Win32::UI::WindowsAndMessaging::SW_HIDE;

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    fn ancho(texto: &OsStr) -> Vec<u16> {
        texto.encode_wide().chain(std::iter::once(0)).collect()
    }

    pub fn vincular(producto: &Producto, direccion: &str, codigo: &str) -> Resultado {
        let Some(raiz) = producto.raiz.clone() else {
            return Resultado::error("No se encontró la carpeta del nodo.");
        };
        let exe = match std::env::current_exe() {
            Ok(e) => e,
            Err(e) => return Resultado::error(format!("No se encontró el ejecutable de la app: {e}")),
        };
        // El codigo, en un archivo nuevo de la carpeta temporal del usuario (su perfil: el usuario, SYSTEM y
        // Administradores). UAC no le pasa a la copia elevada ni el entorno ni la entrada estandar.
        let nanos =
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
        let archivo = std::env::temp_dir().join(format!("libraedge-vinculo-{}-{nanos}.txt", std::process::id()));
        if let Err(e) = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&archivo)
            .and_then(|mut f| std::io::Write::write_all(&mut f, codigo.as_bytes()))
        {
            return Resultado::error(format!("No se pudo preparar la vinculación: {e}"));
        }
        let parametros =
            format!("--vincular-elevado --direccion {direccion} --archivo-codigo \"{}\"", archivo.display());
        let resultado = elevar(&exe, &parametros);
        let _ = std::fs::remove_file(&archivo);
        match resultado {
            Err(Elevacion::Cancelada) => Resultado::error(
                "No se autorizó: hace falta aceptar el aviso de Windows (o la clave de un administrador).",
            ),
            Err(Elevacion::Fallo(e)) => Resultado::error(format!("No se pudo pedir permiso de administrador: {e}")),
            Ok(0) => Resultado { ok: true, detalle: cola_del_log(&raiz, 6) },
            Ok(codigo_salida) => Resultado::error(format!(
                "La configuración del nodo falló (código {codigo_salida}). Lo último del registro:\n{}",
                cola_del_log(&raiz, 12)
            )),
        }
    }

    fn cola_del_log(raiz: &Path, cuantas: usize) -> String {
        let bytes = std::fs::read(raiz.join("logs").join("instalacion.log")).unwrap_or_default();
        ultimas_lineas(&String::from_utf8_lossy(&bytes), cuantas)
    }

    enum Elevacion {
        Cancelada,
        Fallo(String),
    }

    /// Lanza `exe parametros` elevado (UAC) y espera su codigo de salida.
    fn elevar(exe: &Path, parametros: &str) -> Result<u32, Elevacion> {
        let verbo = ancho(OsStr::new("runas"));
        let archivo = ancho(exe.as_os_str());
        let params = ancho(OsStr::new(parametros));
        // SAFETY: estructura de C inicializada en cero y con punteros a buffers que viven hasta el final.
        unsafe {
            let mut info: SHELLEXECUTEINFOW = std::mem::zeroed();
            info.cbSize = std::mem::size_of::<SHELLEXECUTEINFOW>() as u32;
            info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC;
            info.lpVerb = verbo.as_ptr();
            info.lpFile = archivo.as_ptr();
            info.lpParameters = params.as_ptr();
            info.nShow = SW_HIDE;
            if ShellExecuteExW(&mut info) == 0 {
                let error = GetLastError();
                return Err(if error == ERROR_CANCELLED {
                    Elevacion::Cancelada
                } else {
                    Elevacion::Fallo(format!("error {error}"))
                });
            }
            if info.hProcess.is_null() {
                return Err(Elevacion::Fallo("sin proceso".into()));
            }
            WaitForSingleObject(info.hProcess, INFINITE);
            let mut codigo: u32 = 1;
            GetExitCodeProcess(info.hProcess, &mut codigo);
            CloseHandle(info.hProcess);
            Ok(codigo)
        }
    }

    fn powershell() -> PathBuf {
        let mut buf = [0u16; 260];
        // SAFETY: buffer propio con su largo.
        let n = unsafe { GetSystemDirectoryW(buf.as_mut_ptr(), buf.len() as u32) } as usize;
        let sistema = if n > 0 && n < buf.len() {
            PathBuf::from(String::from_utf16_lossy(&buf[..n]))
        } else {
            PathBuf::from(r"C:\Windows\System32")
        };
        sistema.join(r"WindowsPowerShell\v1.0\powershell.exe")
    }

    /// La copia elevada: corre el configurador del instalador y devuelve su codigo de salida. El archivo del
    /// codigo se borra siempre (instalar.ps1 ya lo borra al leerlo).
    pub fn vincular_elevado(producto: &Producto, direccion: &str, archivo_codigo: &Path) -> i32 {
        let codigo = (|| {
            if !super::direccion_valida(direccion) || !archivo_codigo.is_file() {
                return 2;
            }
            let Some(raiz) = producto.raiz.as_ref() else { return 2 };
            let script = raiz.join("instalador").join("instalar.ps1");
            if !script.is_file() {
                return 2;
            }
            Command::new(powershell())
                .args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"])
                .arg(&script)
                .arg("-RaizNodo")
                .arg(raiz)
                .arg("-Direccion")
                .arg(direccion)
                .arg("-ArchivoCodigo")
                .arg(archivo_codigo)
                .current_dir(raiz)
                .creation_flags(CREATE_NO_WINDOW)
                .status()
                .ok()
                .and_then(|s| s.code())
                .unwrap_or(1)
        })();
        let _ = std::fs::remove_file(archivo_codigo);
        codigo
    }
}

#[cfg(windows)]
pub use plataforma::vincular_elevado;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn el_codigo_se_acepta_como_lo_tipean() {
        assert_eq!(normalizar_codigo("K7Q4-M2XP").as_deref(), Some("K7Q4M2XP"));
        assert_eq!(normalizar_codigo(" k7q4 m2xp ").as_deref(), Some("K7Q4M2XP"));
        assert_eq!(normalizar_codigo("k7q4m2xp").as_deref(), Some("K7Q4M2XP"));
    }

    #[test]
    fn el_codigo_no_se_corrige() {
        assert_eq!(normalizar_codigo("K7Q4-M2X0"), None); // 0 no esta en el alfabeto
        assert_eq!(normalizar_codigo("K7Q4-M2XO"), None); // O tampoco
        assert_eq!(normalizar_codigo("K7Q4-M2X"), None);
        assert_eq!(normalizar_codigo("K7Q4-M2XPP"), None);
        assert_eq!(normalizar_codigo(""), None);
    }

    #[test]
    fn la_direccion_no_puede_romper_la_linea_de_comandos() {
        assert!(direccion_valida("sistema.contalibra.com.ar"));
        assert!(direccion_valida("https://demo.contalibra.com.ar/"));
        assert!(direccion_valida("localhost:8443"));
        assert!(!direccion_valida(""));
        assert!(!direccion_valida("a b"));
        assert!(!direccion_valida("x\" -Carga \"y"));
        assert!(!direccion_valida("x;rm -rf /"));
        assert!(!direccion_valida("$(id)"));
        assert!(!direccion_valida(&"a".repeat(300)));
    }

    #[test]
    fn primero_se_valida_y_no_se_lanza_nada() {
        let p = Producto { puerto: 8000, salud: "/health".into(), raiz: None };
        assert!(!vincular(&p, "a b", "K7Q4-M2XP").ok);
        assert!(!vincular(&p, "sistema.contalibra.com.ar", "K7Q4-M2X0").ok);
    }

    #[test]
    fn las_ultimas_lineas_sin_vacias_ni_cabeceras_del_transcript() {
        let texto = "\u{feff}**********************\nuno\n\n  dos  \r\ntres\n**********************\n";
        assert_eq!(ultimas_lineas(texto, 2), "dos\ntres");
        assert_eq!(ultimas_lineas(texto, 10), "uno\ndos\ntres");
    }
}
