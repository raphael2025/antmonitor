/* AntMonitor i18n — no framework */
(function (global) {
  const STORE_KEY = "antmonitor_lang";
  const SUPPORTED = ["zh", "en", "ru", "es", "de", "ar"];
  const RTL = new Set(["ar"]);

  const catalogs = Object.create(null);
  let lang = "zh";

  function register(code, dict) {
    catalogs[code] = Object.assign(catalogs[code] || {}, dict || {});
  }

  function detect() {
    try {
      const saved = localStorage.getItem(STORE_KEY);
      if (saved && SUPPORTED.includes(saved)) return saved;
    } catch (e) {}
    const nav = (navigator.language || navigator.userLanguage || "zh").toLowerCase();
    if (nav.startsWith("zh")) return "zh";
    if (nav.startsWith("ru")) return "ru";
    if (nav.startsWith("es")) return "es";
    if (nav.startsWith("de")) return "de";
    if (nav.startsWith("ar")) return "ar";
    if (nav.startsWith("en")) return "en";
    return "zh";
  }

  function interpolate(s, vars) {
    if (!vars) return s;
    return String(s).replace(/\{(\w+)\}/g, (_, k) =>
      vars[k] != null ? String(vars[k]) : "{" + k + "}");
  }

  function t(key, vars) {
    const d = catalogs[lang] || {};
    const en = catalogs.en || {};
    const zh = catalogs.zh || {};
    const raw = (d[key] != null ? d[key] : null)
      ?? (en[key] != null ? en[key] : null)
      ?? (zh[key] != null ? zh[key] : null)
      ?? key;
    return interpolate(raw, vars);
  }

  function apply(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (!key) return;
      const val = t(key);
      if (el.tagName === "TITLE") el.textContent = val;
      else if (el.childNodes.length === 1 && el.childNodes[0].nodeType === 3)
        el.textContent = val;
      else {
        // Prefer updating text of first text-ish content; keep child inputs
        let done = false;
        for (const n of el.childNodes) {
          if (n.nodeType === 3 && n.textContent.trim()) {
            n.textContent = val;
            done = true;
            break;
          }
        }
        if (!done) el.textContent = val;
      }
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    scope.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    });
    document.documentElement.lang = lang === "zh" ? "zh-CN" : lang;
    document.documentElement.dir = RTL.has(lang) ? "rtl" : "ltr";
    ["langSelect", "langSelectLogin"].forEach((id) => {
      const sel = document.getElementById(id);
      if (sel && sel.value !== lang) sel.value = lang;
    });
    document.title = t("doc.title");
    document.dispatchEvent(new CustomEvent("i18n:change", { detail: { lang } }));
  }

  function setLang(code, persist) {
    if (!SUPPORTED.includes(code)) code = "zh";
    lang = code;
    if (persist !== false) {
      try { localStorage.setItem(STORE_KEY, lang); } catch (e) {}
    }
    apply();
    return lang;
  }

  function init() {
    lang = detect();
    apply();
    ["langSelect", "langSelectLogin"].forEach((id) => {
      const sel = document.getElementById(id);
      if (!sel) return;
      sel.value = lang;
      if (!sel._i18nBound) {
        sel._i18nBound = true;
        sel.addEventListener("change", () => setLang(sel.value, true));
      }
    });
  }

  global.I18N = {
    SUPPORTED, register, t, apply, setLang, init,
    get lang() { return lang; },
  };
  global.t = function (key, vars) { return global.I18N.t(key, vars); };
})(window);
