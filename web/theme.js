/* Shared day/night preference. Load before CSS to avoid a flash of the wrong theme. */
(function () {
  "use strict";
  const root = document.documentElement;
  const key = document.currentScript.getAttribute("data-storage-key") || "antmonitor_theme";
  const modes = ["system", "time", "light", "dark"];
  const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  const labels = {
    zh: { label: "界面主题", system: "跟随系统", time: "按时间自动", light: "日间", dark: "夜间", hint: "自动时间使用本机时间：07:00–19:00 日间，其余时间夜间" },
    en: { label: "Theme", system: "Follow system", time: "Automatic by time", light: "Day", dark: "Night", hint: "Local device time: day from 07:00 to 19:00, night otherwise" },
    ru: { label: "Тема", system: "Как в системе", time: "По времени", light: "Дневная", dark: "Ночная", hint: "Местное время устройства: дневная тема с 07:00 до 19:00, иначе ночная" },
    es: { label: "Tema", system: "Seguir sistema", time: "Automático por hora", light: "Día", dark: "Noche", hint: "Hora local del dispositivo: día de 07:00 a 19:00, noche el resto" },
    de: { label: "Design", system: "System folgen", time: "Nach Uhrzeit", light: "Tag", dark: "Nacht", hint: "Lokale Gerätezeit: Tag von 07:00 bis 19:00, sonst Nacht" },
    ar: { label: "المظهر", system: "حسب النظام", time: "تلقائي حسب الوقت", light: "نهاري", dark: "ليلي", hint: "التوقيت المحلي للجهاز: نهاري من 07:00 إلى 19:00، وليلي باقي الوقت" }
  };
  const palettes = {
    light: { grid: "#e0e6ed", axis: "#c7d0dc", tick: "#596677", label: "#334155", bg: "#ffffff", text: "#202c3c" },
    dark: { grid: "#21262d", axis: "#30363d", tick: "#8b949e", label: "#adbac7", bg: "#161b22", text: "#e6e6e6" }
  };
  function normalize(value) { return modes.includes(value) ? value : "system"; }
  function read() {
    try { return normalize(localStorage.getItem(key)); } catch (_) { return "system"; }
  }
  let mode = read();
  let applied = "";
  function resolve() {
    if (mode === "light" || mode === "dark") return mode;
    if (mode === "system" && media) return media.matches ? "dark" : "light";
    const hour = new Date().getHours();
    return hour >= 7 && hour < 19 ? "light" : "dark";
  }
  function palette() { return palettes[applied || resolve()]; }
  function controls() {
    const lang = (root.lang || "zh").split("-")[0];
    const copy = labels[lang] || labels.en;
    document.querySelectorAll(".theme-select").forEach((select) => {
      select.value = mode;
      select.setAttribute("aria-label", copy.label);
      select.title = copy.hint;
      Array.from(select.options).forEach((option) => { option.textContent = copy[option.value]; });
    });
    document.querySelectorAll(".theme-label").forEach((label) => { label.textContent = copy.label; });
  }
  function repaintCharts() {
    if (!window.Chart) return;
    Object.values(window.Chart.instances || {}).forEach((chart) => chart.update("none"));
  }
  function apply() {
    const theme = resolve();
    root.setAttribute("data-theme-mode", mode);
    controls();
    if (theme === applied) return;
    applied = theme;
    root.setAttribute("data-theme", theme);
    repaintCharts();
    document.dispatchEvent(new CustomEvent("theme:change", { detail: { mode, theme } }));
  }
  function setMode(value) {
    mode = normalize(value);
    try { localStorage.setItem(key, mode); } catch (_) { /* Still usable without browser storage. */ }
    apply();
  }
  // Chart axes, legends and tooltips must change with the surrounding page too.
  function styleChart(chart) {
    const colors = palette();
    const opts = chart.options;
    opts.color = colors.label;
    Object.values(opts.scales || {}).forEach((scale) => {
      if (scale.ticks) scale.ticks.color = colors.tick;
      if (scale.grid) scale.grid.color = colors.grid;
      if (scale.border) scale.border.color = colors.axis;
      if (scale.title) scale.title.color = colors.tick;
    });
    const plugins = opts.plugins || {};
    if (plugins.legend && plugins.legend.labels) plugins.legend.labels.color = colors.label;
    if (plugins.tooltip) {
      Object.assign(plugins.tooltip, { backgroundColor: colors.bg, titleColor: colors.text,
        bodyColor: colors.text, borderColor: colors.axis, borderWidth: 1 });
    }
  }
  function init() {
    document.querySelectorAll(".theme-select").forEach((select) => {
      select.addEventListener("change", () => setMode(select.value));
    });
    if (window.Chart) {
      window.Chart.register({ id: "antmonitorTheme", beforeUpdate: styleChart });
      repaintCharts();
    }
    controls();
  }
  window.AntTheme = { setMode, palette, get mode() { return mode; } };
  apply();
  if (media) {
    const changed = () => { if (mode === "system") apply(); };
    if (media.addEventListener) media.addEventListener("change", changed);
    else if (media.addListener) media.addListener(changed);
  }
  // Recheck on return from sleep/background; periodic checks also handle clock changes.
  window.setInterval(() => { if (mode === "time" || !media) apply(); }, 60000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) apply(); });
  window.addEventListener("pageshow", apply);
  window.addEventListener("storage", (event) => {
    if (event.key === key || event.key === null) { mode = read(); apply(); }
  });
  document.addEventListener("i18n:change", controls);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
