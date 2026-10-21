"""Los otros puestos del local encuentran la PC principal (ADR-017).

El que contesta (``libraedge/descubrimiento.py``) se prueba con sockets de verdad en loopback; la app de puesto
(``escritorio/src/descubrimiento.rs``) tiene sus pruebas de Rust y las dos se prueban juntas, de punta a punta, en el
workflow ``publicar-instalador.yml`` (el nodo instalado contesta y la app de puesto lo encuentra por broadcast). Aca,
ademas, que los dos lados digan lo mismo y que los instaladores lleven el servicio, el firewall y el puesto.
"""

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from libraedge import descubrimiento as desc
from libraedge import instalador as ins

RAIZ = Path(__file__).resolve().parent.parent
INSTALADOR = RAIZ / "instalador"
LINUX = INSTALADOR / "linux"
PUESTO = LINUX / "puesto"
ESCRITORIO = RAIZ / "escritorio"
PRODUCTOS = sorted(p.stem for p in (INSTALADOR / "productos").glob("*.json"))
WORKFLOW = RAIZ / ".github" / "workflows" / "publicar-instalador.yml"


def _texto(ruta: Path) -> str:
    return ruta.read_text(encoding="utf-8")


def _datos(**cambios) -> dict:
    datos = {"formato": desc.FORMATO_DATOS, "producto": "ventalibra", "nombre": "VentaLibra", "puerto": 8000,
             "nodo": "nodo-1-abc123", "sucursal": "Casa central", "negocio": "Comercio de Prueba S.A."}
    datos.update(cambios)
    return datos


@pytest.fixture
def archivo(tmp_path):
    ruta = tmp_path / "descubrimiento.json"
    ruta.write_text(json.dumps(_datos()), encoding="utf-8")
    return ruta


# ── el protocolo ──────────────────────────────────────────────────────────


def test_los_dos_lados_dicen_lo_mismo():
    rust = _texto(ESCRITORIO / "src" / "descubrimiento.rs")
    assert f"pub const PUERTO: u16 = {desc.PUERTO};" in rust
    assert f'pub const PEDIDO: &str = "{desc.PEDIDO}";' in rust
    assert f'pub const RESPUESTA: &str = "{desc.RESPUESTA}";' in rust
    assert f"pub const TAMANO_DEL_PEDIDO: usize = {desc.TAMANO_DEL_PEDIDO};" in rust
    assert ins.PUERTO_DESCUBRIMIENTO == desc.PUERTO
    assert ins.FORMATO_DESCUBRIMIENTO == desc.FORMATO_DATOS


def test_el_pedido_trae_relleno_y_se_lee():
    pedido = desc.armar_pedido("ventalibra")
    assert len(pedido) == desc.TAMANO_DEL_PEDIDO
    assert desc.leer_pedido(pedido) == "ventalibra"


@pytest.mark.parametrize("datagrama", [
    json.dumps({"libraedge": "descubrir/1", "producto": "ventalibra"}).encode(),   # corto: amplificaria
    b" " * 600,
    json.dumps({"libraedge": "otro/1", "producto": "ventalibra"}).encode().ljust(512),
    json.dumps({"libraedge": "descubrir/1"}).encode().ljust(512),
    json.dumps(["descubrir/1"]).encode().ljust(512),
    b"\xff" * 512,
    json.dumps({"libraedge": "descubrir/1", "producto": "ventalibra"}).encode().ljust(desc.TAMANO_MAXIMO + 1),
])
def test_un_pedido_que_no_sirve_no_se_lee(datagrama):
    assert desc.leer_pedido(datagrama) is None


@pytest.mark.parametrize("ip, permitida", [
    ("192.168.0.10", True), ("10.1.0.4", True), ("172.20.3.4", True), ("169.254.1.2", True), ("127.0.0.1", True),
    ("::ffff:192.168.0.10", True),
    ("8.8.8.8", False), ("200.45.1.1", False), ("255.255.255.255", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("no-es-ip", False),
])
def test_solo_se_contesta_a_la_red_del_local(ip, permitida):
    assert desc.origen_permitido(ip) is permitida


def test_la_respuesta_nunca_pasa_del_pedido():
    largo = _datos(negocio="N" * 400, sucursal="S" * 400)
    respuesta = desc.armar_respuesta({k: v for k, v in largo.items() if k != "formato"})
    assert len(respuesta) <= desc.TAMANO_DEL_PEDIDO
    datos = json.loads(respuesta)
    assert datos["libraedge"] == desc.RESPUESTA and datos["producto"] == "ventalibra" and datos["puerto"] == 8000
    # Aun con los fijos larguisimos, sale algo valido y corto.
    enorme = {"producto": "ventalibra", "puerto": 8000, "nombre": "x" * 600, "nodo": "y" * 600}
    assert len(desc.armar_respuesta(enorme)) <= desc.TAMANO_DEL_PEDIDO
    assert desc.leer_respuesta(desc.armar_respuesta(enorme))["producto"] == "ventalibra"


@pytest.mark.parametrize("contenido", [
    "no es json", json.dumps(["x"]), json.dumps({**_datos(), "formato": "otro"}), json.dumps({**_datos(), "puerto": 0}),
    json.dumps({**_datos(), "puerto": True}), json.dumps({**_datos(), "producto": ""}),
])
def test_un_descubrimiento_json_que_no_sirve_no_se_contesta(tmp_path, contenido):
    ruta = tmp_path / "d.json"
    ruta.write_text(contenido, encoding="utf-8")
    assert desc.leer_datos(str(ruta)) is None
    assert desc.leer_datos(str(tmp_path / "no-esta.json")) is None


def test_contestar_solo_a_su_producto_desde_la_lan(archivo):
    datos = desc.leer_datos(str(archivo))
    limitador = desc.Limitador()
    pedido = desc.armar_pedido("ventalibra")
    respuesta = desc.contestar(pedido, "192.168.0.20", datos, limitador)
    assert json.loads(respuesta)["nodo"] == "nodo-1-abc123"
    assert desc.contestar(desc.armar_pedido("contalibra"), "192.168.0.20", datos, limitador) is None
    assert desc.contestar(pedido, "8.8.8.8", datos, limitador) is None
    assert desc.contestar(pedido, "192.168.0.20", None, limitador) is None


def test_el_limitador_por_origen_y_en_total():
    ahora = [0.0]
    limitador = desc.Limitador(reloj=lambda: ahora[0])
    assert all(limitador.permitir("10.0.0.1") for _ in range(desc.POR_ORIGEN))
    assert not limitador.permitir("10.0.0.1")
    assert limitador.permitir("10.0.0.2")
    ahora[0] += desc.VENTANA_POR_ORIGEN
    assert limitador.permitir("10.0.0.1")
    # En total, POR_SEGUNDO por segundo aunque vengan de muchos origenes.
    ahora[0] += 100
    dadas = sum(limitador.permitir(f"10.0.{i // 250}.{i % 250}") for i in range(desc.POR_SEGUNDO + 20))
    assert dadas == desc.POR_SEGUNDO
    # Con muchos origenes viejos, la memoria no crece.
    for i in range(1100):
        ahora[0] += 1.0
        limitador.permitir(f"10.1.{i // 250}.{i % 250}")
    assert len(limitador._por_origen) <= 1024


def test_los_datos_se_releen_cuando_cambian(archivo):
    datos = desc.Datos(str(archivo))
    assert datos.actuales()["nodo"] == "nodo-1-abc123"
    archivo.write_text(json.dumps(_datos(nodo="nodo-2-def456", sucursal="Otra sucursal")), encoding="utf-8")
    assert datos.actuales()["nodo"] == "nodo-2-def456"
    archivo.unlink()
    assert datos.actuales() is None


def _servidor(archivo, **kwargs):
    sock = desc.abrir_socket(0, "127.0.0.1")
    puerto = sock.getsockname()[1]
    parar = threading.Event()
    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.update(dadas=desc.responder(
        sock, desc.Datos(str(archivo)), seguir=lambda: not parar.is_set(), **kwargs)))
    hilo.start()
    return sock, puerto, parar, hilo, resultado


def test_de_punta_a_punta_en_loopback(archivo):
    sock, puerto, parar, hilo, resultado = _servidor(archivo)
    try:
        encontrados = desc.buscar("ventalibra", destinos=["127.0.0.1"], puerto=puerto, espera=1.0)
        assert encontrados == [{**{k: v for k, v in _datos().items() if k != "formato"},
                                "libraedge": desc.RESPUESTA, "ip": "127.0.0.1"}]
        # Otro producto no lo encuentra; un pedido corto no se contesta.
        assert desc.buscar("contalibra", destinos=["127.0.0.1"], puerto=puerto, espera=0.5) == []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as cliente:
            cliente.settimeout(0.5)
            cliente.sendto(json.dumps({"libraedge": "descubrir/1", "producto": "ventalibra"}).encode(),
                           ("127.0.0.1", puerto))
            with pytest.raises(TimeoutError):
                cliente.recv(4096)
    finally:
        parar.set()
        hilo.join(5)
        sock.close()
    assert resultado["dadas"] == 1


def test_el_que_contesta_respeta_el_limite(archivo):
    sock, puerto, parar, hilo, resultado = _servidor(archivo)
    try:
        respuestas = 0
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as cliente:
            cliente.settimeout(0.3)
            for _ in range(desc.POR_ORIGEN + 3):
                cliente.sendto(desc.armar_pedido("ventalibra"), ("127.0.0.1", puerto))
            try:
                while True:
                    cliente.recv(4096)
                    respuestas += 1
            except TimeoutError:
                pass
        assert respuestas == desc.POR_ORIGEN
    finally:
        parar.set()
        hilo.join(5)
        sock.close()


def test_buscar_ignora_respuestas_rotas_y_de_afuera(monkeypatch):
    # Un "nodo" que contesta basura, otro producto y una respuesta buena dos veces (como con dos broadcasts).
    servidor = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    servidor.bind(("127.0.0.1", 0))
    puerto = servidor.getsockname()[1]

    def contestar():
        _datos_, de = servidor.recvfrom(4096)
        servidor.sendto(b"basura", de)
        servidor.sendto(json.dumps({"libraedge": "nodo/1", "producto": "otro", "puerto": 8000}).encode(), de)
        servidor.sendto(json.dumps({"libraedge": "nodo/1", "producto": "ventalibra", "puerto": 0}).encode(), de)
        buena = desc.armar_respuesta({"producto": "ventalibra", "puerto": 8000, "nodo": "n1"})
        servidor.sendto(buena, de)
        servidor.sendto(buena, de)

    hilo = threading.Thread(target=contestar)
    hilo.start()
    try:
        encontrados = desc.buscar("ventalibra", destinos=["127.0.0.1"], puerto=puerto,
                                  espera=0.8)
    finally:
        hilo.join(5)
        servidor.close()
    assert [e["nodo"] for e in encontrados] == ["n1"]


def test_los_broadcasts_y_la_linea_de_comandos(monkeypatch, capsys):
    assert desc._broadcasts(None) == ["255.255.255.255"]
    assert desc._broadcasts("192.168.5.23") == ["255.255.255.255", "192.168.5.255"]
    llamado = {}

    def falso(producto, **kwargs):
        llamado.update(producto=producto, **kwargs)
        return [{"ip": "192.168.0.10", "puerto": 8000}] if producto == "ventalibra" else []

    monkeypatch.setattr(desc, "buscar", falso)
    assert desc.main(["buscar", "--producto", "ventalibra", "--a", "192.168.0.10"]) == 0
    assert json.loads(capsys.readouterr().out)["ip"] == "192.168.0.10"
    assert llamado["destinos"] == ["192.168.0.10"]
    assert desc.main(["buscar", "--producto", "contalibra"]) == 1


# ── el ayudante del instalador: descubrimiento.json ──────────────────────


def _definicion():
    return ins.leer_definicion(str(INSTALADOR / "productos" / "ventalibra.json"))


def test_el_ayudante_escribe_lo_que_contesta_el_nodo_sin_secretos(tmp_path, capsys):
    carga = tmp_path / "carga.json"
    carga.write_text(_texto(INSTALADOR / "carga-de-prueba.json"), encoding="utf-8")
    salida = tmp_path / "estado" / "descubrimiento.json"
    assert ins.main(["descubrimiento", "--definicion", str(INSTALADOR / "productos" / "ventalibra.json"),
                     "--carga", str(carga), "--salida", str(salida)]) == 0
    assert "UDP 47823" in capsys.readouterr().out
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert datos == {"formato": desc.FORMATO_DATOS, "producto": "ventalibra", "nombre": "VentaLibra", "puerto": 8000,
                     "nodo": "nodo-vm-prueba", "sucursal": "Casa central", "negocio": "Comercio de Prueba S.A."}
    assert "prueba-prueba-prueba" not in salida.read_text(encoding="utf-8")
    assert (salida.stat().st_mode & 0o777) == 0o644
    # Y lo lee el que contesta.
    assert desc.leer_datos(str(salida))["nodo"] == "nodo-vm-prueba"


def test_sin_carga_sale_del_nodo_env_del_config_y_del_anterior(tmp_path):
    entorno = tmp_path / "nodo.env"
    entorno.write_text("LIBRAEDGE_NODE_ID=nodo-9-zzz\nLIBRAEDGE_NODE_SECRET=no-se-copia\nSECRET_KEY=tampoco\n")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"empresa_nombre": "  Otro   Comercio  ", "smtp_password": "no"}))
    salida = tmp_path / "descubrimiento.json"
    salida.write_text(json.dumps({"sucursal": "La de antes", "nodo": "viejo"}))
    assert ins.main(["descubrimiento", "--definicion", str(INSTALADOR / "productos" / "ventalibra.json"),
                     "--entorno", str(entorno), "--config", str(config), "--salida", str(salida)]) == 0
    texto = salida.read_text(encoding="utf-8")
    datos = json.loads(texto)
    assert (datos["nodo"], datos["sucursal"], datos["negocio"]) == ("nodo-9-zzz", "La de antes", "Otro Comercio")
    assert "no-se-copia" not in texto and "tampoco" not in texto and "smtp" not in texto


def test_sin_nada_igual_contesta_producto_y_puerto(tmp_path):
    salida = tmp_path / "descubrimiento.json"
    roto = tmp_path / "config.json"
    roto.write_text("{roto")
    assert ins.main(["descubrimiento", "--definicion", str(INSTALADOR / "productos" / "ventalibra.json"),
                     "--config", str(roto), "--salida", str(salida)]) == 0
    datos = json.loads(salida.read_text(encoding="utf-8"))
    assert (datos["nodo"], datos["sucursal"], datos["negocio"], datos["puerto"]) == ("", "", "", 8000)


def test_los_textos_libres_se_acortan():
    carga = ins.carga_desde_dict({**json.loads(_texto(INSTALADOR / "carga-de-prueba.json")),
                                  "sucursal": {"id": "1", "nombre": "S" * 300}}, "ventalibra")
    datos = ins.datos_de_descubrimiento(_definicion(), carga=carga)
    assert len(datos["sucursal"]) == 80


def test_escribir_no_deja_temporales_si_falla(tmp_path, monkeypatch):
    def falla(*_a, **_k):
        raise OSError("disco lleno")

    monkeypatch.setattr(ins.os, "replace", falla)
    with pytest.raises(OSError):
        ins.escribir_descubrimiento(str(tmp_path / "d.json"), {"a": 1})
    assert list(tmp_path.iterdir()) == []


# ── los instaladores del nodo: el servicio y el firewall ─────────────────


def test_linux_tiene_el_servicio_sin_privilegios():
    unidad = _texto(LINUX / "systemd" / "libraedge-descubrimiento.service")
    assert "DynamicUser=yes" in unidad and "\nUser=" not in unidad
    assert "libraedge_descubrimiento.py responder --datos /var/lib/libraedge-nodo/descubrimiento.json" in unidad
    assert "EnvironmentFile" not in unidad  # no ve el nodo.env
    assert "ConditionPathExists=/var/lib/libraedge-nodo/descubrimiento.json" in unidad
    for proteccion in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes", "CapabilityBoundingSet=",
                       "RestrictAddressFamilies=AF_INET AF_UNIX"):
        assert proteccion in unidad
    armar = _texto(LINUX / "armar_deb.sh")
    assert '"$instalador/../libraedge/descubrimiento.py" "$opt/instalador/libraedge_descubrimiento.py"' in armar
    preparar = _texto(LINUX / "preparar_nodo.sh")
    assert "ayudante descubrimiento" in preparar and 'chmod 644 "$DESCUBRIMIENTO"' in preparar
    assert 'ufw allow proto udp from "$red" to any port "$PUERTO_DESCUBRIMIENTO"' in preparar
    comun = _texto(LINUX / "comun.sh")
    assert f"PUERTO_DESCUBRIMIENTO={desc.PUERTO}" in comun
    postrm = _texto(LINUX / "debian" / "postrm")
    assert f"ufw delete allow proto udp from \"$red\" to any port {desc.PUERTO}" in postrm
    assert "rm -f /var/lib/libraedge-nodo/descubrimiento.json" in postrm
    assert "libraedge-descubrimiento.service" in _texto(LINUX / "debian" / "prerm")
    assert "$SERVICIO_DESCUBRIMIENTO" in _texto(LINUX / "verificar_nodo.sh")


def test_windows_tiene_el_servicio_como_localservice_y_su_regla():
    preparar = _texto(INSTALADOR / "preparar_nodo.ps1")
    assert '"descubrimiento", "--definicion"' in preparar
    assert 'ObjectName "NT AUTHORITY\\LocalService"' in preparar
    assert "-Protocol UDP -LocalPort 47823" in preparar and "-Profile Private, Domain" in preparar
    iss = _texto(INSTALADOR / "nodo.iss")
    assert 'Source: "..\\libraedge\\descubrimiento.py"' in iss and 'DestName: "libraedge_descubrimiento.py"' in iss
    assert "remove LibraEdgeDescubrimiento confirm" in iss
    assert 'delete rule name=""LibraEdge descubrimiento"""' in iss
    assert "stop LibraEdgeDescubrimiento /y" in iss and "start LibraEdgeDescubrimiento" in iss
    assert '{commonappdata}\\LibraEdge\\descubrimiento.json' in iss
    assert "LibraEdgeDescubrimiento" in _texto(INSTALADOR / "verificar_nodo.ps1")


# ── los instaladores de el puesto ───────────────────────────────────────────


def test_el_iss_de_la_puesto_tiene_la_misma_tabla_que_las_definiciones():
    iss = _texto(INSTALADOR / "puesto.iss")
    patron = (r'#(?:el)?if Producto == "(\w+)"\s+#define NombreProducto "([^"]+)"\s+#define AppIdPuesto\s+"([^"]+)"'
              r'\s+#define AppIdNodo\s+"([^"]+)"')
    filas = {p: (n, puesto, nodo) for p, n, puesto, nodo in re.findall(patron, iss)}
    assert sorted(filas) == PRODUCTOS
    puestos = set()
    for producto, (nombre, app_puesto, app_nodo) in filas.items():
        definicion = json.loads(_texto(INSTALADOR / "productos" / f"{producto}.json"))
        assert nombre == definicion["nombre"]
        assert app_nodo == definicion["app_id"]
        assert app_puesto != app_nodo and re.fullmatch(r"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}", app_puesto)
        puestos.add(app_puesto)
    assert len(puestos) == len(PRODUCTOS)


def test_el_iss_de_la_puesto_es_solo_la_app():
    iss = _texto(INSTALADOR / "puesto.iss")
    fuentes = re.findall(r'^Source: "([^"]+)"', iss, re.M)
    assert fuentes == ["paquete\\escritorio\\{#Producto}.exe"]
    assert "OutputBaseFilename=libraedge-puesto-{#Producto}-{#Version}" in iss
    assert "DefaultDirName={autopf}\\{#NombreProducto} (puesto)" in iss
    assert 'Name: "{commondesktop}\\{#NombreProducto}"' in iss and 'Name: "{commonprograms}\\{#NombreProducto}"' in iss
    assert "HayWebView2" in iss and "EsLaPrincipal" in iss
    assert iss.isascii()
    lineas = iss[iss.index("[Code]"):].splitlines()[1:]
    assert not [linea for linea in lineas if linea.startswith("[")]


def test_el_deb_de_la_puesto_es_solo_la_app():
    armar = _texto(PUESTO / "armar_deb.sh")
    assert 'install -m 755 "$app" "$opt/$paquete"' in armar
    assert 'opt="$raiz/opt/libraedge-puesto/$producto"' in armar
    assert "Depends: libwebkit2gtk-4.1-0, libgtk-3-0" in armar
    assert "postgresql" not in re.search(r"Depends: (.*)", armar).group(1)
    assert "python" not in re.search(r"Recommends: (.*)", armar).group(1)
    desktop = _texto(PUESTO / "libraedge-puesto.desktop")
    assert "Exec=/opt/libraedge-puesto/@PRODUCTO@/libraedge-puesto-@PRODUCTO@" in desktop
    assert "StartupWMClass=libraedge-puesto-@PRODUCTO@" in desktop
    dock = _texto(PUESTO / "dock.sh")
    assert "91_libraedge-puesto.gschema.override" in dock and "python" not in dock
    assert "libraedge-nodo-*.desktop" in dock  # no pisa el favorito del nodo si estan los dos


@pytest.mark.parametrize("producto", PRODUCTOS)
def test_armar_el_deb_de_la_puesto(tmp_path, producto):
    import shutil
    import subprocess
    if not shutil.which("dpkg-deb"):
        pytest.skip("sin dpkg-deb")
    escritorio = tmp_path / "esc"
    escritorio.mkdir()
    app = escritorio / f"libraedge-nodo-{producto}"
    app.write_text("#!/bin/sh\n")
    app.chmod(0o755)
    salida = tmp_path / "salida"
    subprocess.run(["bash", str(PUESTO / "armar_deb.sh"), producto, "--escritorio", str(escritorio),
                    "--version", "1.2.3",
                    "--salida", str(salida)], check=True, capture_output=True)
    deb = salida / f"libraedge-puesto-{producto}_1.2.3_amd64.deb"
    contenido = subprocess.run(["dpkg-deb", "-c", str(deb)], check=True, capture_output=True, text=True).stdout
    assert f"./opt/libraedge-puesto/{producto}/libraedge-puesto-{producto}" in contenido
    assert f"./usr/share/applications/libraedge-puesto-{producto}.desktop" in contenido
    control = subprocess.run(["dpkg-deb", "-f", str(deb)], check=True, capture_output=True, text=True).stdout
    assert f"Package: libraedge-puesto-{producto}" in control
    for script in ("postinst", "postrm"):
        texto = subprocess.run(["dpkg-deb", "--ctrl-tarfile", str(deb)], check=True, capture_output=True).stdout
        assert script.encode() in texto
    assert subprocess.run(["bash", str(PUESTO / "armar_deb.sh"), producto], capture_output=True).returncode == 2


def test_la_app_detecta_el_modo_por_la_definicion_instalada():
    producto = _texto(ESCRITORIO / "src" / "producto.rs")
    assert 'r.join("instalador").join("producto.json")' in producto and "Modo::Nodo" in producto
    main = _texto(ESCRITORIO / "src" / "main.rs")
    assert 'format!("ar.com.librasuite.{PRODUCTO}.puesto")' in main
    assert "En los otros puestos no se vincula" in main


def test_el_workflow_arma_prueba_y_publica_la_puesto():
    workflow = _texto(WORKFLOW)
    assert "instalador/linux/puesto/armar_deb.sh" in workflow and "puesto.iss" in workflow
    assert "libraedge-puesto-${{ matrix.producto }}_amd64.deb" in workflow
    assert "libraedge-puesto-${{ matrix.producto }}.exe" in workflow
    assert "libraedge-puesto-*.exe libraedge-puesto-*.deb" in workflow
    assert "--buscar" in workflow and "cambio de direccion" in workflow
    assert '"libraedge/descubrimiento.py"' in workflow


def test_un_reloj_real_no_rompe_el_limitador():
    limitador = desc.Limitador()
    assert limitador.permitir("10.0.0.1")
    time.sleep(0.01)
    assert limitador.permitir("10.0.0.1")
