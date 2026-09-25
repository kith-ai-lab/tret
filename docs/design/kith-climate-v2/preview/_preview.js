// Preview harness only — not app code.
// Loads the fonts (the app self-hosts them via @fontsource; the preview uses
// Google Fonts so it opens without an npm install), and adds a small toggle
// that flips <html data-brand="kith-climate"> on and off so each screen can be
// compared against the current open-source look. ?brand=oss opens it off.
(function () {
  var fonts = document.createElement('link')
  fonts.rel = 'stylesheet'
  fonts.href =
    'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Instrument+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap'
  document.head.appendChild(fonts)

  // The app's font stacks name the @fontsource families ("Inter Variable",
  // "Instrument Sans Variable"); point them at the Google families here.
  var shim = document.createElement('style')
  shim.textContent =
    ':root:not([data-brand]){--sans:"Instrument Sans",system-ui,sans-serif;--mono:"JetBrains Mono",ui-monospace,monospace}' +
    '.pv-toggle{position:fixed;z-index:100;right:14px;bottom:14px;display:flex;gap:2px;padding:3px;' +
    'background:#1a1d21;border-radius:8px;font:500 12px/1 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.25)}' +
    '.pv-toggle button{all:unset;cursor:pointer;padding:7px 11px;border-radius:6px;color:#c9ccce}' +
    '.pv-toggle button[aria-pressed="true"]{background:#5b9a8b;color:#10201b}' +
    '.pv-toggle a{color:#c9ccce;padding:7px 9px;text-decoration:none}.pv-toggle a:hover{color:#fff}'
  document.head.appendChild(shim)

  var html = document.documentElement
  if (new URLSearchParams(location.search).get('brand') === 'oss') html.removeAttribute('data-brand')

  function render() {
    var on = html.getAttribute('data-brand') === 'kith-climate'
    bar.querySelector('[data-v="kc"]').setAttribute('aria-pressed', String(on))
    bar.querySelector('[data-v="oss"]').setAttribute('aria-pressed', String(!on))
  }

  var bar = document.createElement('div')
  bar.className = 'pv-toggle'
  bar.innerHTML =
    '<a href="landing.html">Landing</a><a href="chat.html">Thread</a><a href="components.html">Views</a>' +
    '<button data-v="kc">Kith Climate</button><button data-v="oss">Open source (current)</button>'
  bar.addEventListener('click', function (e) {
    var v = e.target.getAttribute && e.target.getAttribute('data-v')
    if (!v) return
    if (v === 'kc') html.setAttribute('data-brand', 'kith-climate')
    else html.removeAttribute('data-brand')
    render()
  })
  document.addEventListener('DOMContentLoaded', function () {
    document.body.appendChild(bar)
    render()
  })
})()
