// Apply the saved choice before styles or React render; new visitors start in light mode.
      (function () {
        var theme = 'light';
        try { if (localStorage.getItem('secforge-theme') === 'dark') theme = 'dark'; } catch (_) {}
        document.documentElement.dataset.theme = theme;
        document.documentElement.style.colorScheme = theme;
        document.querySelector('meta[name="theme-color"]').content = theme === 'dark' ? '#121821' : '#F7F8FA';
        document.querySelector('link[rel="icon"]').href = '/favicon-' + theme + '.svg';
      })();
// Fonts are optional and never block interactive page startup. No inline event handlers.
document.addEventListener('DOMContentLoaded', function () {
  var fonts = document.querySelector('link[data-secforge-fonts]');
  if (fonts) fonts.media = 'all';
}, { once: true });
