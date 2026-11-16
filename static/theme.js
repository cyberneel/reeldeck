// Loaded before the stylesheet paints: data-theme is always light or dark, so the
// CSS has one token block per theme. "auto" (no saved choice) follows the system.
(() => {
  const html = document.documentElement;
  const sys = matchMedia("(prefers-color-scheme: light)");
  const saved = () => { try { return localStorage.getItem("theme"); } catch { return null; } };
  const apply = () => { html.dataset.theme = saved() || (sys.matches ? "light" : "dark"); };
  apply();
  sys.addEventListener("change", apply);
  // the topbar's toggle: auto -> light -> dark -> auto
  window.reeldeckTheme = {
    get: () => saved() || "auto",
    cycle() {
      const next = { auto: "light", light: "dark", dark: "auto" }[this.get()];
      try { next === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch { /* private window */ }
      apply();
      return next;
    },
  };
})();
