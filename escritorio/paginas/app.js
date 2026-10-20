// Las pantallas propias de la app (ADR-015): «inicio», «espera» y «vincular». Cual se muestra lo decide la app
// (?p=...), que tambien vuelve al producto apenas responde.
(function () {
  'use strict';
  var invocar = function (comando, args) { return window.__TAURI_INTERNALS__.invoke(comando, args || {}); };
  var $ = function (id) { return document.getElementById(id); };
  var pantalla = new URLSearchParams(location.search).get('p') || 'inicio';

  function mostrar(id) {
    ['inicio', 'espera', 'vincular'].forEach(function (s) { $(s).hidden = s !== id; });
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
    $('direccion').placeholder = 'sistema.' + e.producto + '.com.ar';
    mostrar(pantalla === 'vincular' || pantalla === 'espera' ? pantalla : 'inicio');
    if (pantalla === 'vincular') { $('direccion').focus(); }
  }).catch(function () { mostrar(pantalla); });

  if (pantalla === 'espera') {
    setTimeout(function () { $('ayuda-espera').hidden = false; }, 45000);
  }

  $('formulario').addEventListener('submit', function (ev) {
    ev.preventDefault();
    var direccion = $('direccion').value.trim();
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
