/* ==========================================================================
   Google Material Design 3 (M3) & M3 Expressive — Interactive Runtime
   Theme & Material You Dynamic Color Sync, M3 Ripple Effect, Global Settings
   Dialog / Bottom Sheet, and M3 Snackbar System
   ========================================================================== */
(function () {
  var THEME_KEY = "schoolhub.theme.v1";
  var SEED_KEY = "schoolhub.m3.seed.v1";
  var MOTION_KEY = "schoolhub.m3.motion.v1";
  var DENSITY_KEY = "schoolhub.m3.density.v1";
  var SCALE_KEY = "schoolhub.m3.scale.v1";

  function getSavedTheme() {
    try {
      var qTheme = new URLSearchParams(window.location.search).get("theme");
      if (qTheme === "dark" || qTheme === "light") return qTheme;
    } catch (_) {}
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") return saved;
    } catch (_) {}
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function getSavedSeed() {
    try {
      var s = localStorage.getItem(SEED_KEY);
      if (["blue", "emerald", "violet", "rose", "amber"].indexOf(s) !== -1) return s;
    } catch (_) {}
    return "blue";
  }

  function getSavedMotion() {
    try {
      var m = localStorage.getItem(MOTION_KEY);
      if (m === "reduced" || m === "standard") return m;
    } catch (_) {}
    return "standard";
  }

  function getSavedDensity() {
    try {
      var d = localStorage.getItem(DENSITY_KEY);
      if (d === "compact" || d === "standard") return d;
    } catch (_) {}
    return "standard";
  }

  function getSavedScale() {
    try {
      var sc = parseInt(localStorage.getItem(SCALE_KEY) || "100", 10);
      if (!isNaN(sc) && sc >= 85 && sc <= 115) return sc;
    } catch (_) {}
    return 100;
  }

  function applyTheme(theme) {
    var resolved = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
    try {
      localStorage.setItem(THEME_KEY, resolved);
    } catch (_) {}
    window.dispatchEvent(new CustomEvent("m3-theme-change", { detail: { theme: resolved } }));
    syncSettingsModalUI();
  }

  function applySeed(seed) {
    var valid = ["blue", "emerald", "violet", "rose", "amber"].indexOf(seed) !== -1 ? seed : "blue";
    document.documentElement.setAttribute("data-seed", valid);
    try {
      localStorage.setItem(SEED_KEY, valid);
    } catch (_) {}
    window.dispatchEvent(new CustomEvent("m3-seed-change", { detail: { seed: valid } }));
    syncSettingsModalUI();
  }

  function applyMotion(motion) {
    var valid = motion === "reduced" ? "reduced" : "standard";
    document.documentElement.setAttribute("data-motion", valid);
    try {
      localStorage.setItem(MOTION_KEY, valid);
    } catch (_) {}
    syncSettingsModalUI();
  }

  function applyDensity(density) {
    var valid = density === "compact" ? "compact" : "standard";
    document.documentElement.setAttribute("data-density", valid);
    try {
      localStorage.setItem(DENSITY_KEY, valid);
    } catch (_) {}
    syncSettingsModalUI();
  }

  function applyScale(scalePct) {
    var num = Math.max(85, Math.min(115, parseInt(scalePct || 100, 10) || 100));
    document.documentElement.style.fontSize = num + "%";
    try {
      localStorage.setItem(SCALE_KEY, String(num));
    } catch (_) {}
    syncSettingsModalUI();
  }

  // Initialize attributes immediately
  if (!document.documentElement.getAttribute("data-theme")) {
    document.documentElement.setAttribute("data-theme", getSavedTheme());
  }
  document.documentElement.setAttribute("data-seed", getSavedSeed());
  document.documentElement.setAttribute("data-motion", getSavedMotion());
  document.documentElement.setAttribute("data-density", getSavedDensity());
  var initScale = getSavedScale();
  if (initScale !== 100) {
    document.documentElement.style.fontSize = initScale + "%";
  }

  // Global Snackbar Helper
  var snackbarEl = null;
  var snackbarTimer = null;
  function showSnackbar(message, opts) {
    opts = opts || {};
    if (!document.body) return;
    if (!snackbarEl) {
      snackbarEl = document.createElement("div");
      snackbarEl.className = "md-snackbar";
      snackbarEl.setAttribute("role", "status");
      snackbarEl.setAttribute("aria-live", "polite");
      snackbarEl.style.cssText =
        "position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);z-index:20000;opacity:0;pointer-events:none;transition:opacity 0.2s ease,transform 0.2s ease;";
      document.body.appendChild(snackbarEl);
    }
    snackbarEl.textContent = String(message || "");
    snackbarEl.style.opacity = "1";
    snackbarEl.style.transform = "translateX(-50%) translateY(0)";
    if (snackbarTimer) clearTimeout(snackbarTimer);
    snackbarTimer = setTimeout(function () {
      if (snackbarEl) {
        snackbarEl.style.opacity = "0";
        snackbarEl.style.transform = "translateX(-50%) translateY(20px)";
      }
    }, opts.duration || 2600);
  }

  // Global M3 Settings Modal / Bottom Sheet
  var settingsScrim = null;
  function ensureSettingsModal() {
    if (settingsScrim || !document.body) return settingsScrim;
    settingsScrim = document.createElement("div");
    settingsScrim.id = "m3GlobalSettingsScrim";
    settingsScrim.className = "md-dialog-scrim";
    settingsScrim.setAttribute("role", "dialog");
    settingsScrim.setAttribute("aria-modal", "true");
    settingsScrim.setAttribute("aria-labelledby", "m3GlobalSettingsTitle");
    settingsScrim.innerHTML = [
      '<div class="md-dialog-surface" id="m3GlobalSettingsSurface">',
      '  <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;">',
      '    <div>',
      '      <h2 class="md-typescale-title-large" id="m3GlobalSettingsTitle" style="margin:0;display:flex;align-items:center;gap:8px;"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/></svg>Material Design 3 表示・操作設定</h2>',
      '      <p class="md-typescale-body-small" style="margin:4px 0 0;color:var(--md-sys-color-on-surface-variant);">サイト全体のテーマ・Material You ダイナミックカラー・アクセシビリティを即座に切り替えます</p>',
      '    </div>',
      '    <button type="button" class="md-icon-btn" id="m3SettingsCloseBtn" aria-label="設定を閉じる"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>',
      '  </div>',
      '  <hr class="md-divider">',
      '  <div style="display:flex;flex-direction:column;gap:16px;">',
      '    <div>',
      '      <label class="md-typescale-label-large" style="display:block;margin-bottom:8px;">1. カラーテーマ (ライト / ダーク)</label>',
      '      <div class="md-segmented-group" role="group" aria-label="カラーテーマ選択">',
      '        <button type="button" class="md-segmented-btn" data-m3-theme-set="light"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/><path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/><path d="m19.07 4.93-1.41 1.41"/></svg>ライトテーマ</button>',
      '        <button type="button" class="md-segmented-btn" data-m3-theme-set="dark"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.85" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>ダークテーマ</button>',
      '      </div>',
      '    </div>',
      '    <div>',
      '      <label class="md-typescale-label-large" style="display:block;margin-bottom:8px;">2. Material You ダイナミックカラーパレット</label>',
      '      <div style="display:flex;flex-wrap:wrap;gap:8px;" role="radiogroup" aria-label="ダイナミックカラー選択">',
      '        <button type="button" class="md-chip" data-m3-seed-set="blue"><span style="width:10px;height:10px;border-radius:50%;background:#0b57d0;display:inline-block;"></span>Google Blue</button>',
      '        <button type="button" class="md-chip" data-m3-seed-set="emerald"><span style="width:10px;height:10px;border-radius:50%;background:#006c4c;display:inline-block;"></span>Emerald</button>',
      '        <button type="button" class="md-chip" data-m3-seed-set="violet"><span style="width:10px;height:10px;border-radius:50%;background:#6750a4;display:inline-block;"></span>Violet</button>',
      '        <button type="button" class="md-chip" data-m3-seed-set="rose"><span style="width:10px;height:10px;border-radius:50%;background:#984061;display:inline-block;"></span>Rose</button>',
      '        <button type="button" class="md-chip" data-m3-seed-set="amber"><span style="width:10px;height:10px;border-radius:50%;background:#825500;display:inline-block;"></span>Amber</button>',
      '      </div>',
      '    </div>',
      '    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">',
      '      <div>',
      '        <div class="md-typescale-label-large">3. コンパクト密度表示 (Compact Density)</div>',
      '        <div class="md-typescale-body-small" style="color:var(--md-sys-color-on-surface-variant);">カード・ボタン・テーブルの余白を引き締めて情報密度を高めます</div>',
      '      </div>',
      '      <label class="md-switch" aria-label="コンパクト密度表示">',
      '        <input type="checkbox" id="m3DensitySwitch">',
      '        <span class="md-switch-track"><span class="md-switch-thumb"></span></span>',
      '      </label>',
      '    </div>',
      '    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;">',
      '      <div>',
      '        <div class="md-typescale-label-large">4. 視差・アニメーション軽減 (Reduced Motion)</div>',
      '        <div class="md-typescale-body-small" style="color:var(--md-sys-color-on-surface-variant);">リップルや画面遷移アニメーションを最小化します</div>',
      '      </div>',
      '      <label class="md-switch" aria-label="アニメーション軽減">',
      '        <input type="checkbox" id="m3MotionSwitch">',
      '        <span class="md-switch-track"><span class="md-switch-thumb"></span></span>',
      '      </label>',
      '    </div>',
      '    <div>',
      '      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">',
      '        <label class="md-typescale-label-large" for="m3ScaleSlider">5. UI・フォントサイズ倍率</label>',
      '        <span class="md-badge primary" id="m3ScaleValueLabel">100%</span>',
      '      </div>',
      '      <input type="range" class="md-slider" id="m3ScaleSlider" min="85" max="115" step="5" value="100">',
      '    </div>',
      '  </div>',
      '  <hr class="md-divider">',
      '  <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">',
      '    <button type="button" class="md-btn md-btn-text" id="m3SettingsResetBtn">初期値に戻す</button>',
      '    <button type="button" class="md-btn md-btn-filled" id="m3SettingsDoneBtn">完了</button>',
      '  </div>',
      '</div>'
    ].join("\n");
    document.body.appendChild(settingsScrim);

    settingsScrim.addEventListener("click", function (ev) {
      if (ev.target === settingsScrim) closeSettings();
    });
    var closeBtn = document.getElementById("m3SettingsCloseBtn");
    var doneBtn = document.getElementById("m3SettingsDoneBtn");
    var resetBtn = document.getElementById("m3SettingsResetBtn");
    if (closeBtn) closeBtn.addEventListener("click", closeSettings);
    if (doneBtn) doneBtn.addEventListener("click", function () {
      closeSettings();
      showSnackbar("Material Design 3 設定を保存しました");
    });
    if (resetBtn) resetBtn.addEventListener("click", function () {
      applyTheme("light");
      applySeed("blue");
      applyDensity("standard");
      applyMotion("standard");
      applyScale(100);
      showSnackbar("設定を初期値にリセットしました");
    });

    settingsScrim.querySelectorAll("[data-m3-theme-set]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyTheme(btn.getAttribute("data-m3-theme-set"));
      });
    });
    settingsScrim.querySelectorAll("[data-m3-seed-set]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applySeed(btn.getAttribute("data-m3-seed-set"));
      });
    });
    var densitySwitch = document.getElementById("m3DensitySwitch");
    if (densitySwitch) {
      densitySwitch.addEventListener("change", function () {
        applyDensity(densitySwitch.checked ? "compact" : "standard");
      });
    }
    var motionSwitch = document.getElementById("m3MotionSwitch");
    if (motionSwitch) {
      motionSwitch.addEventListener("change", function () {
        applyMotion(motionSwitch.checked ? "reduced" : "standard");
      });
    }
    var scaleSlider = document.getElementById("m3ScaleSlider");
    if (scaleSlider) {
      scaleSlider.addEventListener("input", function () {
        applyScale(scaleSlider.value);
      });
    }

    syncSettingsModalUI();
    return settingsScrim;
  }

  function syncSettingsModalUI() {
    if (!settingsScrim) return;
    var curTheme = document.documentElement.getAttribute("data-theme") || "light";
    var curSeed = document.documentElement.getAttribute("data-seed") || "blue";
    var curDensity = document.documentElement.getAttribute("data-density") || "standard";
    var curMotion = document.documentElement.getAttribute("data-motion") || "standard";
    var curScale = getSavedScale();

    settingsScrim.querySelectorAll("[data-m3-theme-set]").forEach(function (btn) {
      var active = btn.getAttribute("data-m3-theme-set") === curTheme;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });
    settingsScrim.querySelectorAll("[data-m3-seed-set]").forEach(function (btn) {
      var active = btn.getAttribute("data-m3-seed-set") === curSeed;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    var densitySwitch = document.getElementById("m3DensitySwitch");
    if (densitySwitch) densitySwitch.checked = curDensity === "compact";
    var motionSwitch = document.getElementById("m3MotionSwitch");
    if (motionSwitch) motionSwitch.checked = curMotion === "reduced";
    var scaleSlider = document.getElementById("m3ScaleSlider");
    if (scaleSlider) scaleSlider.value = String(curScale);
    var scaleLabel = document.getElementById("m3ScaleValueLabel");
    if (scaleLabel) scaleLabel.textContent = curScale + "%";
  }

  function openSettings() {
    var modal = ensureSettingsModal();
    if (modal) {
      syncSettingsModalUI();
      modal.classList.add("open");
    }
  }

  function closeSettings() {
    if (settingsScrim) {
      settingsScrim.classList.remove("open");
    }
  }

  window.SchoolHubM3 = {
    getTheme: function () {
      return document.documentElement.getAttribute("data-theme") || getSavedTheme();
    },
    setTheme: applyTheme,
    toggleTheme: function () {
      var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next);
      return next;
    },
    getSeed: function () {
      return document.documentElement.getAttribute("data-seed") || getSavedSeed();
    },
    setSeed: applySeed,
    openSettings: openSettings,
    closeSettings: closeSettings,
    showSnackbar: showSnackbar
  };

  // Cross-tab synchronization
  window.addEventListener("storage", function (ev) {
    if (!ev) return;
    if (ev.key === THEME_KEY && (ev.newValue === "dark" || ev.newValue === "light")) {
      document.documentElement.setAttribute("data-theme", ev.newValue);
      syncSettingsModalUI();
    } else if (ev.key === SEED_KEY && ev.newValue) {
      document.documentElement.setAttribute("data-seed", ev.newValue);
      syncSettingsModalUI();
    }
  });

  // Material 3 Ripple Effect delegation
  document.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0) return;
    if (document.documentElement.getAttribute("data-motion") === "reduced") return;
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    var target = ev.target && ev.target.closest
      ? ev.target.closest(
          ".md-btn, .md-icon-btn, .md-chip, .md-segmented-btn, .md-nav-item, .md-tab, .tab-btn, .range-btn, .shortcut-btn, .theme-btn, .card.service, .md-ripple-surface"
        )
      : null;
    if (!target || target.disabled || target.getAttribute("aria-disabled") === "true") return;

    var rect = target.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    var computedPos = window.getComputedStyle(target).position;
    if (computedPos === "static") {
      target.style.position = "relative";
    }
    var computedOverflow = window.getComputedStyle(target).overflow;
    if (computedOverflow === "visible") {
      target.style.overflow = "hidden";
    }

    var wave = document.createElement("span");
    wave.className = "md-ripple-wave";
    var size = Math.max(rect.width, rect.height);
    wave.style.width = size + "px";
    wave.style.height = size + "px";
    wave.style.left = (ev.clientX - rect.left - size / 2) + "px";
    wave.style.top = (ev.clientY - rect.top - size / 2) + "px";
    target.appendChild(wave);
    setTimeout(function () {
      if (wave.parentNode) wave.parentNode.removeChild(wave);
    }, 600);
  }, { passive: true });
})();
