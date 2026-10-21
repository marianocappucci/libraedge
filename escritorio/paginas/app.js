// Las pantallas propias de la app (ADR-015): «inicio», «espera» y «vincular»; en un puesto secundario (ADR-017),
// tambien «buscando», «elegir» y «direccion» (la PC principal). Cual se muestra lo decide la app (?p=...), que
// tambien vuelve al producto apenas responde.
(function () {
  'use strict';
  var invocar = function (comando, args) { return window.__TAURI_INTERNALS__.invoke(comando, args || {}); };
  var $ = function (id) { return document.getElementById(id); };
  var pantalla = new URLSearchParams(location.search).get('p') || 'inicio';

  var PANTALLAS = ['inicio', 'espera', 'vincular', 'buscando', 'elegir', 'direccion'];
  function mostrar(id) {
    PANTALLAS.forEach(function (s) { $(s).hidden = s !== id; });
  }
  function cada(selector, f) { Array.prototype.forEach.call(document.querySelectorAll(selector), f); }

  function lugar(n) {
    return [n.sucursal, n.negocio].filter(function (x) { return x; }).join(' · ');
  }

  function armarLista(encontrados) {
    var lista = $('lista-principales');
    lista.textContent = '';
    encontrados.forEach(function (n, i) {
      var li = document.createElement('li');
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = lugar(n) || ('PC ' + n.ip);
      var d = document.createElement('span');
      d.className = 'detalle';
      d.textContent = 'http://' + n.ip + ':' + n.puerto + '/';
      b.appendChild(d);
      b.addEventListener('click', function () {
        cada('#lista-principales button', function (x) { x.disabled = true; });
        invocar('elegir', { indice: i }).catch(function () {
          cada('#lista-principales button', function (x) { x.disabled = false; });
        });
      });
      li.appendChild(b);
      lista.appendChild(li);
    });
  }

  invocar('estado').then(function (e) {
    document.title = e.nombre;
    Array.prototype.forEach.call(document.querySelectorAll('.nombre'), function (n) { n.textContent = e.nombre; });
    Array.prototype.forEach.call(document.querySelectorAll('.url'), function (n) { n.textContent = e.url; });
    $('version').textContent = e.version;
    var img = document.createElement('img');
    img.alt = '';
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(e.marca);
    $('marca').appendChild(img);
    $('direccion-sistema').placeholder = 'sistema.' + e.producto + '.com.ar';
    var puesto = e.modo === 'puesto';
    cada('.solo-puesto', function (n) { n.hidden = !puesto; });
    cada('.solo-nodo', function (n) { n.hidden = puesto || n.id === 'ayuda-espera'; });
    if (puesto && e.principal) {
      var donde = lugar(e.principal);
      cada('.donde-principal', function (n) {
        n.textContent = ' (' + (donde ? donde + ', ' : '') + e.principal.ip + ')';
      });
    }
    if (pantalla === 'elegir') { armarLista(e.encontrados || []); }
    mostrar(PANTALLAS.indexOf(pantalla) >= 0 ? pantalla : 'inicio');
    if (pantalla === 'vincular') { $('direccion-sistema').focus(); }
    if (pantalla === 'direccion') { $('ip').focus(); }
    if (pantalla === 'espera') {
      setTimeout(function () { $(puesto ? 'ayuda-espera-puesto' : 'ayuda-espera').hidden = false; }, 45000);
    }
  }).catch(function () { mostrar(pantalla); });

  cada('.buscar-otra', function (b) {
    b.addEventListener('click', function () { b.disabled = true; invocar('buscar_otra'); });
  });

  $('form-direccion').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var ip = $('ip').value.trim();
    $('error-ip').hidden = true;
    if (!ip) { return; }
    $('boton-ip').disabled = true;
    $('boton-ip').textContent = 'Conectando…';
    invocar('usar_direccion', { direccion: ip }).catch(function (detalle) {
      $('boton-ip').disabled = false;
      $('boton-ip').textContent = 'Conectar';
      $('error-ip').textContent = String(detalle || 'No se pudo conectar.');
      $('error-ip').hidden = false;
      $('ip').select();
    });
  });

  $('formulario').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var direccion = $('direccion-sistema').value.trim();
    var codigo = $('codigo').value.trim();
    $('error').hidden = true;
    if (!direccion || !codigo) {
      $('error').textContent = 'Faltan la dirección del sistema o el código.';
      $('error').hidden = false;
      return;
    }
    $('boton').disabled = true;
    $('formulario').hidden = true;
    $('progreso').hidden = false;
    invocar('vincular', { direccion: direccion, codigo: codigo }).then(function () {
      $('texto-progreso').textContent = 'Listo: esta PC quedó vinculada. Conectando con ' + document.title + '…';
      $('codigo').value = '';
    }).catch(function (detalle) {
      $('progreso').hidden = true;
      $('formulario').hidden = false;
      $('boton').disabled = false;
      $('error').textContent = String(detalle || 'No se pudo vincular.');
      $('error').hidden = false;
      $('codigo').select();
    });
  });
})();
