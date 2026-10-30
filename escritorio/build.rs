//! El producto se elige al compilar (ADR-015): `LIBRAEDGE_PRODUCTO=contalibra cargo build --release`, o
//! `python escritorio/armar.py contalibra`. Lo propio del producto sale de la MISMA definicion que usa el
//! instalador (`instalador/productos/<producto>.json`): nombre, puerto y ruta de salud; el icono, de
//! `instalador/iconos/<producto>.{ico,png}`; la marca de las pantallas propias, de `instalador/marca/<producto>.svg`.
//!
//! **Marca blanca (ADR-029):** con `LIBRAEDGE_MARCA_DIR=<carpeta que arma instalador/marca.py>` el nombre, el icono, la marca de las
//! pantallas y el identificador salen de esa carpeta (`producto.json`, `icono.{png,ico}`, `marca.svg`, `marca.json`) y no de
//! `instalador/`. Sin la variable, todo es como siempre.
//!
//! La configuracion de Tauri del producto (nombre, identificador, version, iconos) se arma aca y se le pasa a
//! `tauri_build` y a `generate_context!` por `TAURI_CONFIG`, que los dos fusionan con `tauri.conf.json`.

use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=LIBRAEDGE_PRODUCTO");
    println!("cargo:rerun-if-env-changed=LIBRAEDGE_VERSION");
    println!("cargo:rerun-if-env-changed=LIBRAEDGE_MARCA_DIR");
    let manifiesto = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let instalador = manifiesto.join("..").join("instalador");
    // Sin LIBRAEDGE_PRODUCTO (cargo check / cargo test a mano) se compila ContaLibra.
    let producto = env::var("LIBRAEDGE_PRODUCTO").unwrap_or_else(|_| "contalibra".into());
    assert!(
        !producto.is_empty() && producto.chars().all(|c| c.is_ascii_lowercase()),
        "LIBRAEDGE_PRODUCTO tiene que ser el nombre de un productos/<producto>.json"
    );
    // La marca blanca (ADR-029), si se pidio: una carpeta con lo que arma `instalador/marca.py`.
    let marca_dir = env::var("LIBRAEDGE_MARCA_DIR").ok().filter(|d| !d.is_empty()).map(PathBuf::from);
    let ruta_def = match &marca_dir {
        Some(dir) => dir.join("producto.json"),
        None => instalador.join("productos").join(format!("{producto}.json")),
    };
    println!("cargo:rerun-if-changed={}", ruta_def.display());
    let definicion: serde_json::Value = serde_json::from_str(
        &fs::read_to_string(&ruta_def).unwrap_or_else(|e| panic!("no se pudo leer {}: {e}", ruta_def.display())),
    )
    .expect("la definicion del producto no es JSON");
    let texto = |clave: &str| definicion[clave].as_str().unwrap_or_else(|| panic!("falta {clave}")).to_string();
    let nombre = texto("nombre");
    let salud = texto("salud");
    let puerto = definicion["puerto"].as_u64().expect("falta puerto");
    let version = env::var("LIBRAEDGE_VERSION").unwrap_or_else(|_| "0.0.0".into());

    let icono = |ext: &str| {
        let ruta = match &marca_dir {
            Some(dir) => dir.join(format!("icono.{ext}")),
            None => instalador.join("iconos").join(format!("{producto}.{ext}")),
        };
        assert!(ruta.is_file(), "falta {}", ruta.display());
        println!("cargo:rerun-if-changed={}", ruta.display());
        ruta.to_string_lossy().replace('\\', "/")
    };
    let marca = match &marca_dir {
        Some(dir) => dir.join("marca.svg"),
        None => instalador.join("marca").join(format!("{producto}.svg")),
    };
    println!("cargo:rerun-if-changed={}", marca.display());
    let salida = PathBuf::from(env::var("OUT_DIR").unwrap());
    fs::copy(&marca, salida.join("marca.svg")).expect("falta la marca del producto");

    // Con marca blanca el identificador es el de la marca (`nodo.<slug>`): no dice Libra y sigue siendo uno por marca.
    let identificador = match &marca_dir {
        Some(dir) => {
            let ruta = dir.join("marca.json");
            println!("cargo:rerun-if-changed={}", ruta.display());
            let marca_json: serde_json::Value = serde_json::from_str(
                &fs::read_to_string(&ruta).unwrap_or_else(|e| panic!("no se pudo leer {}: {e}", ruta.display())),
            )
            .expect("marca.json no es JSON");
            marca_json["identificador"].as_str().expect("falta identificador en marca.json").to_string()
        }
        None => format!("ar.com.librasuite.{producto}"),
    };

    let config = serde_json::json!({
        "productName": nombre,
        "version": version,
        // Un identificador por producto: separa la sesion del navegador embebido (WebView2 la guarda en
        // %LOCALAPPDATA%\<identificador>), las preferencias y la instancia unica.
        "identifier": identificador,
        "bundle": { "icon": [icono("png"), icono("ico")] },
    });
    env::set_var("TAURI_CONFIG", config.to_string());
    // generate_context! (en el rustc del crate) lee la misma variable.
    println!("cargo:rustc-env=TAURI_CONFIG={config}");
    println!("cargo:rustc-env=LIBRAEDGE_PRODUCTO={producto}");
    println!("cargo:rustc-env=LIBRAEDGE_NOMBRE={nombre}");
    println!("cargo:rustc-env=LIBRAEDGE_PUERTO={puerto}");
    println!("cargo:rustc-env=LIBRAEDGE_SALUD={salud}");
    println!("cargo:rustc-env=LIBRAEDGE_VERSION={version}");

    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&[
        "estado",
        "vincular",
        "alternar_pantalla_completa",
        "buscar_otra",
        "elegir",
        "usar_direccion",
    ])))
    .expect("tauri_build");
}
