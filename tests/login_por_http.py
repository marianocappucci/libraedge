"""La prueba de login real por http contra un nodo instalado, en el CI del instalador (ADR-016).

Lo corre ``publicar-instalador.yml`` con el **Python del nodo** (que trae libraauth y altcha), en Windows y en Ubuntu.
Hace las dos cosas que ``curl`` no puede: saber que libraauth quedo instalado y armar el cuerpo del login (la clave del
admin local, que sale del ``nodo.env``, y el captcha ALTCHA resuelto). El login y el pedido autenticado los hace
``curl -c/-b`` en el workflow.

    python -I tests/login_por_http.py version
        imprime "<version de libraauth> nueva|vieja": "nueva" si ya lee LIBRAAUTH_COOKIE_SECURE (v0.53.0 o mas).
    python -I tests/login_por_http.py cuerpo <nodo.env o -> <desafio.json> <salida.json>
        escribe el cuerpo de ``POST <prefijo>/login`` (usuario, clave y captcha) en <salida.json>, solo para el dueño.
        Con ``-`` el nodo.env llega por la entrada (en Linux es de root: ``sudo cat nodo.env | python ...``).

🔴 La clave nunca pasa por la linea de comandos ni se imprime: entra por archivo o por la entrada y sale a un archivo
que el workflow borra apenas lo usa.
"""
import json
import os
import sys

#: Desde esta version libraauth lee LIBRAAUTH_COOKIE_SECURE (libraauth ADR-026).
LIBRAAUTH_CON_COOKIE_CONFIGURABLE = (0, 53, 0)


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
    if len(argv) == 4 and argv[0] == "cuerpo":
        _, entorno, desafio, salida = argv
        if entorno == "-":
            texto = sys.stdin.read()
        else:
            with open(entorno, encoding="utf-8-sig") as archivo:
                texto = archivo.read()
        with open(desafio, encoding="utf-8") as archivo:
            captcha = resolver_captcha(json.load(archivo))
        cuerpo = {"username": os.environ.get("USUARIO_DE_PRUEBA", "admin"), "password": clave_del_admin(texto),
                  "captcha": captcha}
        escribir_privado(salida, json.dumps(cuerpo))
        print("Cuerpo del login escrito (sin mostrar la clave).")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
