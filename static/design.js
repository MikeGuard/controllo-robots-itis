/*
 * Design tokens — shared by student.html and instructor.html.
 * Must load right after the Tailwind CDN script.
 *
 * Palette rules
 *   neutral   slate            surfaces, borders, text
 *   primary   blue             actions, selection, focus  (indigo / sky / purple / blue all resolve here)
 *   robot id  cyan             Niryo identity only (UR identity = primary blue)
 *   status    emerald/amber/rose   ok / caution / danger   (red resolves to rose)
 *
 * Type scale   11 / 12 / 13 / 14 / 16 / 18 / 20 / 24   (11px is the floor)
 * Radius       4px controls, 6px panels, 8px dialogs, full only for dots
 * Elevation    flat everywhere; one shadow for floating layers (menus, modals, toasts)
 */
// Tailwind v3 default scales. Dark theme uses them as-is; the light theme remaps (see LIGHT_STEP).
const SCALES = {
  blue:    { 50: '239 246 255', 100: '219 234 254', 200: '191 219 254', 300: '147 197 253', 400: '96 165 250', 500: '59 130 246', 600: '37 99 235', 700: '29 78 216', 800: '30 64 175', 900: '30 58 138', 950: '23 37 84' },
  emerald: { 50: '236 253 245', 100: '209 250 229', 200: '167 243 208', 300: '110 231 183', 400: '52 211 153', 500: '16 185 129', 600: '5 150 105', 700: '4 120 87', 800: '6 95 70', 900: '6 78 59', 950: '2 44 34' },
  amber:   { 50: '255 251 235', 100: '254 243 199', 200: '253 230 138', 300: '252 211 77', 400: '251 191 36', 500: '245 158 11', 600: '217 119 6', 700: '180 83 9', 800: '146 64 14', 900: '120 53 15', 950: '69 26 3' },
  cyan:    { 50: '236 254 255', 100: '207 250 254', 200: '165 243 252', 300: '103 232 249', 400: '34 211 238', 500: '6 182 212', 600: '8 145 178', 700: '14 116 144', 800: '21 94 117', 900: '22 78 99', 950: '8 51 68' },
  rose:    { 50: '255 241 242', 100: '255 228 230', 200: '254 205 211', 300: '253 164 175', 400: '251 113 133', 500: '244 63 94', 600: '225 29 72', 700: '190 18 60', 800: '159 18 57', 900: '136 19 55', 950: '76 5 25' },
};
// Light theme: text steps (200-400) go darker, tinted fills (800-950) go pale, solid fills (500-700) stay put.
const LIGHT_STEP = { 50: 950, 100: 900, 200: 800, 300: 700, 400: 600, 500: 500, 600: 600, 700: 700, 800: 200, 900: 100, 950: 50 };
const STEPS = Object.keys(SCALES.blue).map(Number);
const varName = (hue, n) => `--${hue}-${n}`;
const hueVars = (light) => Object.entries(SCALES).map(([h, sc]) =>
  STEPS.map(n => `${varName(h, n)}:${sc[light ? LIGHT_STEP[n] : n]};`).join('')).join('');
// Injected before first paint so the Tailwind CDN sees the variables.
(function () {
  const st = document.createElement('style');
  st.id = 'palette-vars';
  st.textContent = `:root{${hueVars(false)}}:root[data-theme="light"]{${hueVars(true)}}`;
  document.head.appendChild(st);
})();
const ramp = (hue) => Object.fromEntries(STEPS.map(n => [n, `rgb(var(${varName(hue, n)}) / <alpha-value>)`]));
const float = '0 8px 24px rgba(0,0,0,.4)';

tailwind.config = {
  theme: {
    borderRadius: {
      none: '0', sm: '2px', DEFAULT: '4px', md: '4px', lg: '6px', xl: '6px',
      '2xl': '8px', '3xl': '8px', full: '9999px',
    },
    boxShadow: {
      none: 'none', sm: 'none', DEFAULT: 'none', md: 'none', lg: 'none', inner: 'none',
      xl: float, '2xl': float,
    },
    fontSize: {
      xs: ['12px', '16px'], sm: ['13px', '18px'], base: ['14px', '20px'],
      lg: ['16px', '24px'], xl: ['18px', '26px'], '2xl': ['20px', '28px'], '3xl': ['24px', '32px'],
    },
    extend: {
      colors: {
        slate: Object.fromEntries([50,100,200,300,400,500,600,700,800,850,900,950].map(n => [n, `rgb(var(--slate-${n}) / <alpha-value>)`])),
        blue: ramp('blue'), indigo: ramp('blue'), sky: ramp('blue'), purple: ramp('blue'),
        emerald: ramp('emerald'), amber: ramp('amber'), cyan: ramp('cyan'),
        rose: ramp('rose'), red: ramp('rose'),
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
    },
  },
};

/* ---------------------------------------------------------------------------
 * Theme: Theme.get() / Theme.set('light'|'dark') / Theme.toggle()
 * Stored in localStorage, defaults to the OS preference, applied before first paint.
 * Fires a `themechange` event on window so editors / canvases can follow.
 * ------------------------------------------------------------------------- */
(function () {
  const KEY = 'ui-theme';
  const root = document.documentElement;
  const read = () => { try { return localStorage.getItem(KEY); } catch (_) { return null; } };
  const initial = () => read() || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  function apply(t) {
    root.setAttribute('data-theme', t);
    root.classList.toggle('dark', t === 'dark');
    root.style.colorScheme = t;
    document.querySelectorAll('[data-theme-toggle]').forEach(b => {
      b.setAttribute('aria-pressed', String(t === 'light'));
      b.textContent = t === 'light' ? 'Dark mode' : 'Light mode';
    });
    if (window.monaco && monaco.editor) monaco.editor.setTheme(t === 'light' ? 'vs' : 'vs-dark');
    window.dispatchEvent(new CustomEvent('themechange', { detail: t }));
  }
  window.Theme = {
    get: () => root.getAttribute('data-theme') || 'dark',
    set(t) { try { localStorage.setItem(KEY, t); } catch (_) {} apply(t); },
    toggle() { this.set(this.get() === 'light' ? 'dark' : 'light'); },
    monacoTheme: () => (root.getAttribute('data-theme') === 'light' ? 'vs' : 'vs-dark'),
  };
  root.setAttribute('data-theme', initial());       // before paint
  root.classList.toggle('dark', initial() === 'dark');
  document.addEventListener('DOMContentLoaded', () => apply(initial()));

})();
