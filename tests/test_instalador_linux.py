"""El instalador del nodo en Linux (ADR-012): lo que se puede probar sin instalar nada.

El ``.deb`` se instala y se verifica de verdad en los runners ``ubuntu-22.04`` y ``ubuntu-24.04`` del workflow
``publicar-instalador.yml``. Aca: lo nuevo del ayudante (``ejecutar --entorno --como``), la forma de los scripts y de
las unidades de systemd, y la bandeja de Linux (que corre con el python3 del sistema, 3.10 en Ubuntu 22.04).
"""

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from libraedge import instalador as ins

RAIZ = Path(__file__).resolve().parent.parent
LINUX = RAIZ / "instalador" / "linux"
SCRIPTS = sorted(LINUX.glob("*.sh"))
UNIDADES = sorted((LINUX / "systemd").iterdir())
DEBIAN = sorted((LINUX / "debian").iterdir())
PRODUCTOS = sorted(p.stem for p in (RAIZ / "instalador" / "productos").glob("*.json"))


def _texto(ruta: Path) -> str:
    return ruta.read_text(encoding="utf-8")


# ── el ayudante: ejecutar con el entorno del nodo y como el usuario del servicio ──


def test_cargar_entorno_no_toca_nada_si_falta_el_archivo(tmp_path):
    destino: dict = {}
    with pytest.raises(ins.ErrorDeInstalacion, match="No esta el archivo de entorno"):
        ins.cargar_entorno(str(tmp_path / "no-esta.env"), destino)
    assert destino == {}


def test_cargar_entorno_lee_el_nodo_env(tmp_path):
    env = tmp_path / "nodo.env"
    env.write_text("A=1\nSECRET_KEY=x=y\n# comentario\n", encoding="utf-8")
    destino: dict = {}
    assert ins.cargar_entorno(str(env), destino) == 2
    assert destino == {"A": "1", "SECRET_KEY": "x=y"}
    env.write_text("a minuscula=1\n", encoding="utf-8")
    with pytest.raises(ins.ErrorDeInstalacion, match="no validos"):
        ins.cargar_entorno(str(env), {})


def test_ejecutar_con_el_entorno_del_nodo(tmp_path, monkeypatch, capsys):
    """Asi corre `libraedge-nodo estado` en verificar_nodo.sh: el entorno lo lee el proceso, no la linea de comandos."""
    estado = tmp_path / "estado.json"
    env = tmp_path / "nodo.env"
    env.write_text(f"LIBRAEDGE_ESTADO={estado}\n", encoding="utf-8")
    monkeypatch.setenv("LIBRAEDGE_ESTADO", "otro")  # monkeypatch lo restaura al terminar
    assert ins.main(["ejecutar", "--entorno", str(env), "libraedge-nodo", "estado"]) == 1
    assert "sin sincronizar" in capsys.readouterr().out
    assert os.environ["LIBRAEDGE_ESTADO"] == str(estado)
    assert ins.main(["ejecutar", "--entorno", str(tmp_path / "no.env"), "libraedge-nodo", "estado"]) == 2


def test_el_entorno_recupera_lo_conservado_al_desinstalar(tmp_path, capsys):
    """apt remove borra el nodo.env (tiene el secreto del nodo) y deja SECRET_KEY y la clave del admin en
    conservado.env: reinstalar con la carga vuelve a abrir lo que libraauth cifro (ADR-012)."""
    definicion = tmp_path / "producto.json"
    definicion.write_text(_texto(RAIZ / "instalador" / "productos" / "ventalibra.json"), encoding="utf-8")
    carga = tmp_path / "carga.json"
    datos = json.loads(_texto(RAIZ / "instalador" / "carga-de-prueba.json"))
    carga.write_text(json.dumps(datos), encoding="utf-8")
    clave = tmp_path / "postgres.clave"
    clave.write_text("clave-del-pg\n", encoding="utf-8")
    env = tmp_path / "nodo.env"
    env.write_text("", encoding="utf-8")  # recien creado, vacio (lo crea instalar.sh con 600)
    conservado = tmp_path / "conservado.env"
    conservado.write_text("SECRET_KEY=el-de-antes\nADMIN_PASSWORD=admin-de-antes\n"
                          "VENTALIBRA_ADMIN_PASSWORD=admin-de-antes\n", encoding="utf-8")
    argumentos = ["entorno", "--definicion", str(definicion), "--carga", str(carga), "--clave-pg", str(clave),
                  "--estado", "/e.json", "--data-dir", "/app", "--salida", str(env), "--previo", str(conservado)]
    assert ins.main(argumentos) == 0
    escrito = ins.leer_entorno(env.read_text(encoding="utf-8"))
    assert escrito["SECRET_KEY"] == "el-de-antes" and escrito["VENTALIBRA_ADMIN_PASSWORD"] == "admin-de-antes"
    # Si el nodo.env tiene uno, manda el del nodo.env.
    env.write_text("SECRET_KEY=el-del-nodo-env\n", encoding="utf-8")
    assert ins.main(argumentos) == 0
    assert ins.leer_entorno(env.read_text(encoding="utf-8"))["SECRET_KEY"] == "el-del-nodo-env"
    # Sin conservado (o uno que no existe) genera uno nuevo, como antes.
    env.write_text("", encoding="utf-8")
    argumentos[-1] = str(tmp_path / "no-esta.env")
    assert ins.main(argumentos) == 0
    assert ins.leer_entorno(env.read_text(encoding="utf-8"))["SECRET_KEY"] not in ("el-de-antes", "el-del-nodo-env")
    assert "el-de-antes" not in capsys.readouterr().out


def test_bajar_a_usuario_en_orden(monkeypatch):
    import pwd

    llamadas = []
    monkeypatch.setattr(pwd, "getpwnam", lambda nombre: pwd.struct_passwd(
        (nombre, "x", 990, 991, "", "/var/lib/libraedge-nodo", "/usr/sbin/nologin")))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "initgroups", lambda nombre, gid: llamadas.append(("initgroups", nombre, gid)))
    monkeypatch.setattr(os, "setgid", lambda gid: llamadas.append(("setgid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: llamadas.append(("setuid", uid)))
    for variable in ("HOME", "USER", "LOGNAME"):
        monkeypatch.setenv(variable, "root")
    ins.bajar_a_usuario("libraedge")
    # El usuario al final: despues de setuid ya no se pueden cambiar los grupos.
    assert llamadas == [("initgroups", "libraedge", 991), ("setgid", 991), ("setuid", 990)]
    assert os.environ["HOME"] == "/var/lib/libraedge-nodo" and os.environ["USER"] == "libraedge"


def test_bajar_a_usuario_que_no_existe_o_en_windows(monkeypatch):
    import pwd

    def no_esta(nombre):
        raise KeyError(nombre)

    monkeypatch.setattr(pwd, "getpwnam", no_esta)
    with pytest.raises(ins.ErrorDeInstalacion, match="No existe el usuario"):
        ins.bajar_a_usuario("nadie")
    monkeypatch.delattr(os, "setuid")
    with pytest.raises(ins.ErrorDeInstalacion, match="solo existe en Linux"):
        ins.bajar_a_usuario("libraedge")


def test_ejecutar_lee_el_entorno_antes_de_bajar_de_usuario(tmp_path, monkeypatch):
    """El nodo.env es root 600: leido despues de bajar, no se podria abrir."""
    orden = []
    monkeypatch.setattr(ins, "cargar_entorno", lambda ruta: orden.append(("entorno", ruta)))
    monkeypatch.setattr(ins, "bajar_a_usuario", lambda usuario: orden.append(("como", usuario)))
    monkeypatch.setattr(ins, "ejecutar", lambda nombre, argumentos: orden.append(("ejecutar", nombre, argumentos)) or 0)
    assert ins.main(["ejecutar", "--entorno", "/etc/x.env", "--como", "libraedge", "alembic", "upgrade", "head"]) == 0
    assert orden == [("entorno", "/etc/x.env"), ("como", "libraedge"), ("ejecutar", "alembic", ["upgrade", "head"])]


# ── los scripts ─────────────────────────────────────────────────────────────


def test_estan_los_scripts_del_nodo_en_linux():
    assert [s.name for s in SCRIPTS] == [
        "abrir_bandeja.sh", "armar_deb.sh", "comun.sh", "instalar-nodo.sh", "instalar.sh", "preparar_nodo.sh",
        "preparar_postgres.sh", "respaldo.sh", "verificar_nodo.sh"]
    assert [d.name for d in DEBIAN] == ["postinst", "postrm", "preinst", "prerm"]


def test_armar_deb_copia_todos_los_scripts_y_las_unidades():
    armar = _texto(LINUX / "armar_deb.sh")
    copiados = re.search(r"for script in ([^;]+); do", armar).group(1).split()
    # instalar-nodo.sh no va en el paquete: va en el Release, al lado de los .deb.
    sueltos = ("armar_deb.sh", "abrir_bandeja.sh", "instalar-nodo.sh")
    assert sorted(copiados) == sorted(s.name for s in SCRIPTS if s.name not in sueltos)
    assert '"$linux"/systemd/*' in armar and "for script in preinst postinst prerm postrm" in armar
    assert '"$ayudante" "$opt/instalador/libraedge_instalador.py"' in armar
    # El mismo arreglo que en Windows: los modulos sueltos de la raiz del producto (plans.py).
    assert "ls-tree --name-only HEAD" in armar and '"${sueltos[@]}"' in armar
    # El Python propio y ruedas que corren en la glibc de 22.04.
    assert "uv python install" in armar and "x86_64-manylinux_2_35" in armar
    assert "uv export --frozen" in armar


@pytest.mark.parametrize("script", [s for s in SCRIPTS if s.name not in ("comun.sh",)], ids=lambda s: s.name)
def test_cada_script_es_bash_estricto(script):
    texto = _texto(script)
    assert texto.startswith("#!/usr/bin/env bash\n")
    assert re.search(r"^set -[eu]+o pipefail$|^set -uo pipefail$|^set -u$", texto, re.M), script.name
    if script.name not in ("armar_deb.sh", "abrir_bandeja.sh", "instalar-nodo.sh"):
        assert '/comun.sh"' in texto


def test_los_scripts_de_linux_son_ejecutables():
    for script in SCRIPTS:
        if script.name != "comun.sh":
            assert os.access(script, os.X_OK), script.name


def test_ningun_secreto_va_en_la_linea_de_comandos():
    """La linea de comandos de un proceso la lee cualquier usuario (/proc/<pid>/cmdline)."""
    for script in SCRIPTS:
        texto = "\n".join(linea for linea in _texto(script).splitlines() if not linea.lstrip().startswith("#"))
        assert not re.search(r"^\s*(source|\.)\s+.*(nodo\.env|ARCHIVO_ENV)", texto, re.M), script.name
        assert not re.search(r"\benv\s+\w+=", texto), script.name
        assert "PGPASSWORD" not in texto, script.name
        assert "--pwfile" not in texto, script.name
    postgres = _texto(LINUX / "preparar_postgres.sh")
    # La clave se lee una sola vez, adentro de un printf (builtin de bash) cuya salida va a la ENTRADA de psql.
    usos = [linea.strip() for linea in postgres.splitlines() if 'cat "$CLAVE_PG"' in linea]
    assert usos == ["printf \"ALTER ROLE postgres PASSWORD '%s';\\n\" \"$(cat \"$CLAVE_PG\")\""]
    assert "| psql_pg -d postgres -v ON_ERROR_STOP=1" in postgres


def test_el_cluster_tiene_zona_checksums_e_icu_desde_el_initdb():
    postgres = _texto(LINUX / "preparar_postgres.sh")
    assert 'TZ="$ZONA" pg_createcluster' in postgres
    for opcion in ("--data-checksums", "--locale-provider=icu", "--icu-locale=en-US", "--auth-host=scram-sha-256"):
        assert opcion in postgres
    assert "listen_addresses = '127.0.0.1'" in postgres
    comun = _texto(LINUX / "comun.sh")
    assert "PG_PUERTO=55432" in comun and f"PG_PUERTO={ins.PUERTO_POSTGRES}" in comun
    assert 'ZONA="America/Argentina/Buenos_Aires"' in comun


def test_la_carga_se_valida_antes_de_tocar_nada():
    instalar = _texto(LINUX / "instalar.sh")
    assert instalar.index("ayudante validar") < instalar.index("tocado=1") < instalar.index("preparar_postgres.sh")
    # Si falla antes de tocar la base, los servicios habilitados se vuelven a arrancar.
    assert "trap al_salir EXIT" in instalar and '[ "$tocado" = 0 ]' in instalar
    assert "LIBRAEDGE_CARGA" in instalar


def test_el_nodo_env_se_crea_600_antes_de_escribirlo():
    nodo = _texto(LINUX / "preparar_nodo.sh")
    crear = nodo.index('install -m 600 -o root -g root /dev/null "$ARCHIVO_ENV"')
    assert crear < nodo.index("ayudante entorno")
    assert '"root:root 600"' in nodo
    # Migraciones y primera sincronizacion: como el usuario del servicio, con el entorno cargado en el proceso.
    assert 'ejecutar --entorno "$ARCHIVO_ENV" --como "$USUARIO"' in nodo
    assert nodo.index("Migraciones:") < nodo.index("libraedge-nodo sincronizar") < nodo.index("systemctl restart")


def test_dos_respaldos_a_la_vez_no_comparten_la_base_de_verificacion():
    respaldo = _texto(LINUX / "respaldo.sh")
    assert 'verificacion="libraedge_verificar_respaldo_${BASHPID}"' in respaldo
    # La rotacion va despues de verificar el nuevo.
    assert respaldo.index("verificado: el respaldo restaura") < respaldo.index("Rotacion")


def test_postinst_sin_carga_no_configura_y_con_carga_si():
    postinst = _texto(LINUX / "debian" / "postinst")
    assert 'if [ -n "${LIBRAEDGE_CARGA:-}" ]' in postinst
    assert 'instalar.sh "$LIBRAEDGE_CARGA"' in postinst
    assert "elif [ -s /etc/libraedge-nodo/nodo.env ]" in postinst  # actualizar un nodo ya configurado
    assert "sudo libraedge-nodo-instalar /ruta/carga-nodo.json" in postinst


def test_desinstalar_borra_el_secreto_y_conserva_los_datos():
    """apt remove borra el nodo.env como Windows; guarda SECRET_KEY y la clave del admin (no el secreto del nodo)."""
    postrm = _texto(LINUX / "debian" / "postrm").split("set -e", 1)[1]
    al_quitar = postrm[postrm.index("remove|purge)"):postrm.index(";;")]
    assert 'rm -f "$env"' in al_quitar
    guardado = re.search(r"grep -E '([^']+)'", al_quitar).group(1)
    assert re.match(guardado, "SECRET_KEY=x") and re.match(guardado, "VENTALIBRA_ADMIN_PASSWORD=x")
    assert not re.match(guardado, "LIBRAEDGE_NODE_SECRET=x") and not re.match(guardado, "VENTALIBRA_DATABASE_URL=x")
    assert "umask 077" in al_quitar
    borra = re.search(r"rm -rf ([^\n]+)", postrm).group(1).split()
    assert borra == ["/etc/libraedge-nodo", "/var/lib/libraedge-nodo/estado", "/var/log/libraedge-nodo"]
    assert "pg_dropcluster" not in postrm  # solo en el comentario: es a mano
    # Reinstalar con la carga recupera lo conservado y despues lo borra.
    nodo = _texto(LINUX / "preparar_nodo.sh")
    assert 'previo=(--previo "$CONSERVADO")' in nodo and 'rm -f "$CONSERVADO"' in nodo


def test_un_nodo_por_pc_antes_de_desempaquetar():
    preinst = _texto(LINUX / "debian" / "preinst")
    assert "/var/lib/libraedge-nodo/producto.txt" in preinst and "@OTROS@" in preinst
    assert "Conflicts" not in _texto(LINUX / "armar_deb.sh")  # apt sacaria el otro nodo sin preguntar


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck no esta (en el CI si)")
def test_shellcheck():
    resultado = subprocess.run(["shellcheck", "-x", "-P", "SCRIPTDIR", *map(str, SCRIPTS), *map(str, DEBIAN)],
                               capture_output=True, text=True, cwd=LINUX)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr


# ── systemd ─────────────────────────────────────────────────────────────────


def test_estan_las_unidades():
    assert [u.name for u in UNIDADES] == [
        "libraedge-estado.path", "libraedge-estado.service", "libraedge-nodo.service", "libraedge-producto.service",
        "libraedge-respaldo.service", "libraedge-respaldo.timer"]


@pytest.mark.parametrize("nombre", ["libraedge-producto.service", "libraedge-nodo.service"])
def test_los_servicios_del_nodo(nombre):
    unidad = _texto(LINUX / "systemd" / nombre)
    assert "User=libraedge" in unidad and "EnvironmentFile=/etc/libraedge-nodo/nodo.env" in unidad
    assert "Requires=postgresql@16-libraedge.service" in unidad
    assert "Restart=always" in unidad and "WantedBy=multi-user.target" in unidad
    assert "ConditionPathExists=/etc/libraedge-nodo/nodo.env" in unidad
    assert "SECRET" not in unidad and "postgresql://" not in unidad


def test_el_producto_escucha_en_el_puerto_de_la_definicion():
    unidad = _texto(LINUX / "systemd" / "libraedge-producto.service")
    assert "-m uvicorn @ASGI@ --host 0.0.0.0 --port @PUERTO@" in unidad


def test_el_respaldo_corre_cada_hora_como_postgres():
    assert "OnCalendar=hourly" in _texto(LINUX / "systemd" / "libraedge-respaldo.timer")
    servicio = _texto(LINUX / "systemd" / "libraedge-respaldo.service")
    assert "User=postgres" in servicio and "respaldo.sh @BASES@ @BASE_NODO@" in servicio


def test_el_estado_lo_corrige_su_dueno_y_no_root():
    servicio = _texto(LINUX / "systemd" / "libraedge-estado.service")
    assert "User=libraedge" in servicio and "-type f" in servicio


def test_las_unidades_que_nombran_los_scripts_existen():
    nombres = {u.name for u in UNIDADES}
    for archivo in [*SCRIPTS, *DEBIAN]:
        for unidad in re.findall(r"libraedge-[a-z]+\.(?:service|timer|path)", _texto(archivo)):
            assert unidad in nombres, f"{archivo.name}: {unidad}"


# ── la bandeja de Linux ─────────────────────────────────────────────────────


def _bandeja():
    spec = importlib.util.spec_from_file_location("bandeja_linux", LINUX / "bandeja_linux.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_la_bandeja_corre_con_el_python_de_ubuntu_22_04():
    """Corre con el python3 del sistema (3.10 en 22.04) y su python3-gi: nada de 3.11+ ni de libraedge."""
    fuente = _texto(LINUX / "bandeja_linux.py")
    ast.parse(fuente, feature_version=(3, 10))
    arbol = ast.parse(fuente)
    de_arriba = [n for n in arbol.body if isinstance(n, ast.Import | ast.ImportFrom)]
    modulos = {a.name.split(".")[0] for n in de_arriba if isinstance(n, ast.Import) for a in n.names}
    modulos |= {n.module.split(".")[0] for n in de_arriba if isinstance(n, ast.ImportFrom) and n.module}
    assert modulos <= {"__future__", "json", "os", "subprocess", "sys"}
    assert "libraedge" not in re.findall(r"^\s*(?:from|import)\s+(\w+)", fuente, re.M)
    assert "datetime.UTC" not in fuente and "from datetime import UTC" not in fuente


@pytest.mark.parametrize("salida, codigo, esperado", [
    ("[ok] Al dia\n  Ultima sincronizacion hace menos de un minuto\n", 0,
     ("ok", "Al dia", "Ultima sincronizacion hace menos de un minuto")),
    ("[atencion] Sin conexion con el central\n", 1, ("atencion", "Sin conexion con el central", "")),
    ("[problema] El nodo no esta sincronizando\n  a\n  b\n", 1, ("problema", "El nodo no esta sincronizando", "a b")),
    ("Traceback...\nModuleNotFoundError: No module named 'libraedge'\n", 1,
     ("problema", "No se pudo leer el estado del nodo", "ModuleNotFoundError: No module named 'libraedge'")),
    ("[raro] algo\n", 0, ("problema", "No se pudo leer el estado del nodo", "[raro] algo")),
    ("", 127, ("problema", "No se pudo leer el estado del nodo", "salio con codigo 127")),
])
def test_la_bandeja_interpreta_lo_que_dice_el_nodo(salida, codigo, esperado):
    assert _bandeja().interpretar(salida, codigo) == esperado


def test_la_bandeja_le_pregunta_al_python_del_nodo():
    bandeja = _bandeja()
    vistos = []

    class Resultado:
        stdout, stderr, returncode = "[ok] Al dia\n", "", 0

    def correr(comando, **opciones):
        vistos.append((comando, opciones))
        return Resultado()

    assert bandeja.consultar(correr=correr) == ("ok", "Al dia", "")
    comando, opciones = vistos[0]
    assert comando[:4] == ["/opt/libraedge-nodo/python/bin/python3", "-I", "-B", "-m"]
    assert comando[4:7] == ["libraedge", "bandeja", "--una-vez"]
    assert "/var/lib/libraedge-nodo/estado/estado.json" in comando and opciones["timeout"] == 30

    def falla(comando, **opciones):
        raise FileNotFoundError(comando[0])

    assert bandeja.consultar(correr=falla) == ("problema", "No se pudo leer el estado del nodo", "FileNotFoundError")


def test_la_bandeja_dibuja_tres_iconos_y_lee_el_producto(tmp_path):
    bandeja = _bandeja()
    bandeja.escribir_iconos(str(tmp_path / "iconos"))
    assert sorted(p.name for p in (tmp_path / "iconos").iterdir()) == [
        "libraedge-atencion.svg", "libraedge-ok.svg", "libraedge-problema.svg"]
    assert 'fill="#228b22"' in (tmp_path / "iconos" / "libraedge-ok.svg").read_text(encoding="utf-8")
    definicion = tmp_path / "producto.json"
    definicion.write_text(json.dumps({"nombre": "VentaLibra", "puerto": 8000}), encoding="utf-8")
    assert bandeja.producto(str(definicion)) == ("VentaLibra", 8000)
    assert bandeja.producto(str(tmp_path / "no-esta.json")) == ("LibraEdge", 8000)


def test_el_autostart_abre_la_bandeja_con_el_python_del_sistema():
    desktop = _texto(LINUX / "libraedge-nodo-bandeja.desktop")
    assert "Exec=/usr/bin/python3 /opt/libraedge-nodo/bandeja/bandeja_linux.py" in desktop
    assert "Type=Application" in desktop
    armar = _texto(LINUX / "armar_deb.sh")
    assert "gnome-shell-extension-appindicator" in armar and "gir1.2-ayatanaappindicator3-0.1" in armar
    assert "python3-gi" in armar.split("Recommends:")[1].split("\n")[0]  # Recommends: un servidor no la necesita


def test_el_workflow_prueba_el_deb_en_las_dos_versiones_de_ubuntu():
    workflow = _texto(RAIZ / ".github" / "workflows" / "publicar-instalador.yml")
    assert "ubuntu-22.04" in workflow and "ubuntu-24.04" in workflow
    assert "verificar_nodo.sh --sin-central" in workflow or "libraedge-nodo-verificar --sin-central" in workflow
    assert "prueba-prueba-prueba-prueba-prueba" in workflow
    assert "libraedge-nodo-${{ matrix.producto }}_amd64.deb" in workflow
    for producto in PRODUCTOS:
        assert producto in workflow


# ── el script de una linea (instalar-nodo.sh, en el Release) ────────────────


def test_instalar_nodo_controla_el_sistema_y_no_usa_apt_key():
    script = _texto(LINUX / "instalar-nodo.sh")
    assert '[ "${ID:-}" = ubuntu ]' in script and "22.04 | 24.04)" in script
    assert "dpkg --print-architecture)\" = amd64" in script and '[ "$(id -u)" = 0 ]' in script
    assert "apt-key" not in script.replace("sin apt-key", "")
    # La forma oficial de PGDG: el script de postgresql-common (llave en un archivo y signed-by).
    assert "/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y" in script
    for producto in PRODUCTOS:
        assert producto in script


def test_instalar_nodo_verifica_el_deb_contra_el_mismo_release():
    script = _texto(LINUX / "instalar-nodo.sh")
    assert 'origen="$REPO/releases/latest/download"' in script
    assert 'origen="$REPO/releases/download/v${LIBRAEDGE_VERSION#v}"' in script
    assert 'for archivo in SHA256SUMS.txt "$deb"' in script
    assert "sha256sum --check --strict" in script
    assert script.index("sha256sum --check") < script.index('LIBRAEDGE_CARGA="$carga" apt-get install')
    assert "--no-install-recommends" not in script  # con las recomendadas: la bandeja


def test_instalar_nodo_lee_todo_antes_de_correr():
    """Con curl | bash el script llega por la entrada: todo va en una funcion y apt no lee esa entrada."""
    script = _texto(LINUX / "instalar-nodo.sh")
    assert script.rstrip().endswith('principal "$@"')
    assert "    exec < /dev/null" in script


@pytest.mark.parametrize("argumentos, codigo", [([], 2), (["otro", "/x"], 1), (["ventalibra", "/x"], 1)])
def test_instalar_nodo_sale_con_error(argumentos, codigo):
    if os.geteuid() == 0:
        pytest.skip("corre como root")
    resultado = subprocess.run(["bash", str(LINUX / "instalar-nodo.sh"), *argumentos], capture_output=True, text=True)
    assert resultado.returncode == codigo, resultado.stderr


def test_el_release_publica_el_script_y_su_suma():
    workflow = _texto(RAIZ / ".github" / "workflows" / "publicar-instalador.yml")
    assert "sha256sum libraedge-nodo-*.exe libraedge-nodo-*.deb instalar-nodo.sh > SHA256SUMS.txt" in workflow
    assert "libraedge-nodo-*.deb instalar-nodo.sh SHA256SUMS.txt" in workflow
    assert "probar-instalar-nodo" in workflow and "LIBRAEDGE_DEB_DIR" in workflow
