"""La prueba de login real por http contra un nodo instalado, en el CI del instalador (ADR-016).

Lo corre ``publicar-instalador.yml`` con el **Python del nodo** (que trae libraauth y altcha), en Windows y en Ubuntu.
Hace las dos cosas que ``curl`` no puede: saber que libraauth quedo instalado y armar el cuerpo del login (la clave del
admin local, que sale del ``nodo.env``, y el captcha ALTCHA resuelto). El login y el pedido autenticado los hace
``curl -c/-b`` en el workflow.

    python -I tests/login_por_http.py version
        imprime "<version de libraauth> nueva|vieja": "nueva" si ya lee LIBRAAUTH_COOKIE_SECURE (v0.53.0 o mas).
    python -I tests/login_por_http.py cuerpo <nodo.env o -> <desafio.json> <salida.json>
        escribe el cuerpo de ``POST <prefijo>/login`` (usuario, clave y, si el nodo lo pide, captcha) en <salida.json>,
        solo para el dueño. Con libraauth >= 0.55.0 el nodo **no** tiene que pedir captcha: si lo pide, falla.
        Con ``-`` el nodo.env llega por la entrada (en Linux es de root: ``sudo cat nodo.env | python ...``).
    python -I tests/login_por_http.py sembrar <nodo.env o ->
        deja en la base del nodo el usuario de la prueba **como lo dejaria el espejo** (libraauth ADR-027: en un nodo
        los usuarios son los del central y no se crea un admin local). En el CI no hay central que los baje, asi que se
        escribe la fila con ``libraedge.aplicando`` prendido, la marca del ``MirrorApplier``. La clave es la de
        ``ADMIN_PASSWORD`` del ``nodo.env`` y el hash lo arma libraauth. Si el usuario ya esta, no lo toca.

🔴 La clave nunca pasa por la linea de comandos ni se imprime: entra por archivo o por la entrada y sale a un archivo
que el workflow borra apenas lo usa.
"""
import json
import os
import sys

#: Desde esta version libraauth lee LIBRAAUTH_COOKIE_SECURE (libraauth ADR-026).
LIBRAAUTH_CON_COOKIE_CONFIGURABLE = (0, 53, 0)
#: Desde esta version un nodo no pide captcha (libraauth ADR-028): desde un puesto se entra por http y el
#: navegador no da WebCrypto, asi que el widget ALTCHA no puede resolverse.
LIBRAAUTH_SIN_CAPTCHA_EN_EL_NODO = (0, 55, 0)


def version_como_tupla(version: str) -> tuple[int, ...]:
    """``"0.53.0"`` -> ``(0, 53, 0)``. Corta en lo primero que no es un numero (``0.53.0.dev3+g1234`` -> 0.53.0)."""
    partes = []
    for parte in version.split(".")[:3]:
        digitos = ""
        for caracter in parte:
            if not caracter.isdigit():
                break
            digitos += caracter
        if not digitos:
            break
        partes.append(int(digitos))
    return tuple(partes)


def es_nueva(version: str) -> bool:
    return version_como_tupla(version) >= LIBRAAUTH_CON_COOKIE_CONFIGURABLE


def clave_del_admin(texto_env: str) -> str:
    """La clave del admin local del ``nodo.env`` (``ADMIN_PASSWORD``, la misma que ``<PREFIJO>_ADMIN_PASSWORD``)."""
    for linea in texto_env.lstrip("﻿").splitlines():
        nombre, igual, valor = linea.strip().partition("=")
        if igual and nombre.strip() == "ADMIN_PASSWORD" and valor.strip():
            return valor.strip()
    raise SystemExit("El nodo.env no tiene ADMIN_PASSWORD.")


def url_de_la_base(texto_env: str) -> str:
    """La base del producto en el ``nodo.env`` (``<PREFIJO>_DATABASE_URL``; no la de libraedge)."""
    for linea in texto_env.lstrip("\ufeff").splitlines():
        nombre, igual, valor = linea.strip().partition("=")
        nombre = nombre.strip()
        if igual and nombre.endswith("_DATABASE_URL") and not nombre.startswith("LIBRAEDGE_") and valor.strip():
            return valor.strip()
    raise SystemExit("El nodo.env no tiene la base del producto (<PREFIJO>_DATABASE_URL).")


def sembrar_usuario(texto_env: str, usuario: str) -> bool:
    """Escribe el usuario de la prueba en ``usuarios`` como el espejo (ver ``sembrar``). ``True`` si lo creo."""
    import psycopg
    from libraauth.hashing import hash_password

    url = url_de_la_base(texto_env)
    if url.startswith("postgresql+psycopg://"):
        url = "postgresql://" + url[len("postgresql+psycopg://"):]
    with psycopg.connect(url) as conn:
        conn.execute("SELECT set_config('libraedge.aplicando', '1', true)")
        fila = conn.execute(
            "INSERT INTO usuarios (username, nombre, email, password_hash, role, activo) "
            "VALUES (%s, %s, '', %s, 'admin', true) ON CONFLICT (username) DO NOTHING RETURNING id",
            (usuario, "Admin de la prueba (espejo)", hash_password(clave_del_admin(texto_env))),
        ).fetchone()
    return fila is not None


def version_de_libraauth() -> str:
    """La version instalada, o ``"0"`` si no esta (los tests de este script corren sin libraauth)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("libraauth")
    except PackageNotFoundError:
        return "0"


def pide_captcha(desafio: dict) -> bool:
    """``False`` si el producto contesto ``{"requerido": false}`` (un nodo con libraauth >= 0.55.0, ADR-028)."""
    return desafio.get("requerido") is not False


def resolver_captcha(desafio: dict) -> str:
    from altcha import Challenge, Payload, solve_challenge

    ch = Challenge.from_dict(desafio)
    return Payload(ch, solve_challenge(ch)).to_base64()


def escribir_privado(ruta: str, contenido: str) -> None:
    descriptor = os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as archivo:
        archivo.write(contenido)


def main(argv: list[str]) -> int:
    if argv[:1] == ["version"]:
        from importlib.metadata import version

        v = version("libraauth")
        print(f"{v} {'nueva' if es_nueva(v) else 'vieja'}")
        return 0
    if len(argv) == 2 and argv[0] == "sembrar":
        texto = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8-sig").read()
        creado = sembrar_usuario(texto, os.environ.get("USUARIO_DE_PRUEBA", "admin"))
        print("Usuario de la prueba sembrado como el espejo." if creado else "El usuario de la prueba ya estaba.")
        return 0
    if len(argv) == 4 and argv[0] == "cuerpo":
        _, entorno, desafio, salida = argv
        if entorno == "-":
            texto = sys.stdin.read()
        else:
            with open(entorno, encoding="utf-8-sig") as archivo:
                texto = archivo.read()
        with open(desafio, encoding="utf-8") as archivo:
            datos_del_desafio = json.load(archivo)
        cuerpo = {"username": os.environ.get("USUARIO_DE_PRUEBA", "admin"), "password": clave_del_admin(texto)}
        if pide_captcha(datos_del_desafio):
            # 🔴 El bug del 2026-10-21: con captcha en el nodo, desde un puesto no se puede iniciar sesion.
            if version_como_tupla(version_de_libraauth()) >= LIBRAAUTH_SIN_CAPTCHA_EN_EL_NODO:
                raise SystemExit("El nodo pide captcha: desde un puesto (http por la red del local) no se podria "
                                 "entrar.")
            cuerpo["captcha"] = resolver_captcha(datos_del_desafio)
        else:
            print("El nodo no pide captcha (libraauth ADR-028).")
        escribir_privado(salida, json.dumps(cuerpo))
        print("Cuerpo del login escrito (sin mostrar la clave).")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
