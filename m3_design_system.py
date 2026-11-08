from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

M3_CSS: str = r"""/* ==========================================================================
   Google Material Design 3 (M3) & M3 Expressive Complete Design System
   School Hub Unified Design Tokens, Typography, Shapes, Elevation & Components
   ========================================================================== */

:root,
[data-theme="light"] {
  color-scheme: light;

  /* --- M3 Color Roles (Light Theme - Google Blue / Expressive Dynamic Palette) --- */
  --md-sys-color-primary: #0b57d0;
  --md-sys-color-on-primary: #ffffff;
  --md-sys-color-primary-container: #d3e3fd;
  --md-sys-color-on-primary-container: #041e49;

  --md-sys-color-secondary: #00639b;
  --md-sys-color-on-secondary: #ffffff;
  --md-sys-color-secondary-container: #c2e7ff;
  --md-sys-color-on-secondary-container: #001d35;

  --md-sys-color-tertiary: #146c2e;
  --md-sys-color-on-tertiary: #ffffff;
  --md-sys-color-tertiary-container: #c4eed0;
  --md-sys-color-on-tertiary-container: #072711;

  --md-sys-color-error: #b3261e;
  --md-sys-color-on-error: #ffffff;
  --md-sys-color-error-container: #f9dedc;
  --md-sys-color-on-error-container: #410e0b;

  --md-sys-color-surface: #f8fafd;
  --md-sys-color-surface-dim: #d8dae0;
  --md-sys-color-surface-bright: #ffffff;
  --md-sys-color-surface-container-lowest: #ffffff;
  --md-sys-color-surface-container-low: #f0f4f9;
  --md-sys-color-surface-container: #e9eef6;
  --md-sys-color-surface-container-high: #dde3ea;
  --md-sys-color-surface-container-highest: #d3d8e0;

  --md-sys-color-on-surface: #1f1f1f;
  --md-sys-color-on-surface-variant: #444746;

  --md-sys-color-outline: #747775;
  --md-sys-color-outline-variant: #c4c7c5;

  --md-sys-color-inverse-surface: #303030;
  --md-sys-color-inverse-on-surface: #f2f2f2;
  --md-sys-color-inverse-primary: #a8c7fa;

  --md-sys-color-scrim: rgba(0, 0, 0, 0.45);
  --md-sys-color-shadow: rgba(0, 0, 0, 0.18);

  /* Semantic Status Extensions mapped to M3 Roles */
  --md-sys-color-success: var(--md-sys-color-tertiary);
  --md-sys-color-on-success: var(--md-sys-color-on-tertiary);
  --md-sys-color-success-container: var(--md-sys-color-tertiary-container);
  --md-sys-color-on-success-container: var(--md-sys-color-on-tertiary-container);

  --md-sys-color-warning: #b06000;
  --md-sys-color-on-warning: #ffffff;
  --md-sys-color-warning-container: #ffdfbe;
  --md-sys-color-on-warning-container: #381c00;

  /* State Layer Opacity Helpers */
  --md-sys-state-hover-opacity: 0.08;
  --md-sys-state-focus-opacity: 0.12;
  --md-sys-state-pressed-opacity: 0.12;
  --md-sys-state-dragged-opacity: 0.16;

  /* M3 Elevation Levels (Surface Tint + Subtle Ambient/Key Shadows) */
  --md-sys-elevation-0: none;
  --md-sys-elevation-1: 0 1px 2px 0 rgba(0, 0, 0, 0.12), 0 1px 3px 1px rgba(11, 87, 208, 0.06);
  --md-sys-elevation-2: 0 1px 2px 0 rgba(0, 0, 0, 0.15), 0 2px 6px 2px rgba(11, 87, 208, 0.08);
  --md-sys-elevation-3: 0 4px 8px 3px rgba(0, 0, 0, 0.10), 0 1px 3px 0 rgba(0, 0, 0, 0.18);
  --md-sys-elevation-4: 0 6px 10px 4px rgba(0, 0, 0, 0.12), 0 2px 3px 0 rgba(0, 0, 0, 0.20);
  --md-sys-elevation-5: 0 8px 12px 6px rgba(0, 0, 0, 0.14), 0 4px 4px 0 rgba(0, 0, 0, 0.22);
}

[data-theme="dark"] {
  color-scheme: dark;

  /* --- M3 Color Roles (Dark Theme - Google Material You Dark Palette) --- */
  --md-sys-color-primary: #a8c7fa;
  --md-sys-color-on-primary: #062e6f;
  --md-sys-color-primary-container: #0842a0;
  --md-sys-color-on-primary-container: #d3e3fd;

  --md-sys-color-secondary: #7fcfff;
  --md-sys-color-on-secondary: #003355;
  --md-sys-color-secondary-container: #004a77;
  --md-sys-color-on-secondary-container: #c2e7ff;

  --md-sys-color-tertiary: #6dd58c;
  --md-sys-color-on-tertiary: #0a3818;
  --md-sys-color-tertiary-container: #0f5223;
  --md-sys-color-on-tertiary-container: #c4eed0;

  --md-sys-color-error: #f2b8b5;
  --md-sys-color-on-error: #601410;
  --md-sys-color-error-container: #8c1d18;
  --md-sys-color-on-error-container: #f9dedc;

  --md-sys-color-surface: #131314;
  --md-sys-color-surface-dim: #131314;
  --md-sys-color-surface-bright: #37393b;
  --md-sys-color-surface-container-lowest: #0e0e0f;
  --md-sys-color-surface-container-low: #1b1b1f;
  --md-sys-color-surface-container: #1e1f20;
  --md-sys-color-surface-container-high: #282a2c;
  --md-sys-color-surface-container-highest: #333537;

  --md-sys-color-on-surface: #e3e3e3;
  --md-sys-color-on-surface-variant: #c4c7c5;

  --md-sys-color-outline: #8e918f;
  --md-sys-color-outline-variant: #444746;

  --md-sys-color-inverse-surface: #e3e3e3;
  --md-sys-color-inverse-on-surface: #303030;
  --md-sys-color-inverse-primary: #0b57d0;

  --md-sys-color-scrim: rgba(0, 0, 0, 0.68);
  --md-sys-color-shadow: rgba(0, 0, 0, 0.55);

  --md-sys-color-success: var(--md-sys-color-tertiary);
  --md-sys-color-on-success: var(--md-sys-color-on-tertiary);
  --md-sys-color-success-container: var(--md-sys-color-tertiary-container);
  --md-sys-color-on-success-container: var(--md-sys-color-on-tertiary-container);

  --md-sys-color-warning: #ffb95f;
  --md-sys-color-on-warning: #462a00;
  --md-sys-color-warning-container: #653e00;
  --md-sys-color-on-warning-container: #ffdfbe;

  --md-sys-elevation-0: none;
  --md-sys-elevation-1: 0 1px 3px 1px rgba(0, 0, 0, 0.35), 0 1px 2px 0 rgba(0, 0, 0, 0.45);
  --md-sys-elevation-2: 0 2px 6px 2px rgba(0, 0, 0, 0.40), 0 1px 2px 0 rgba(0, 0, 0, 0.50);
  --md-sys-elevation-3: 0 4px 8px 3px rgba(0, 0, 0, 0.45), 0 1px 3px 0 rgba(0, 0, 0, 0.55);
  --md-sys-elevation-4: 0 6px 10px 4px rgba(0, 0, 0, 0.48), 0 2px 3px 0 rgba(0, 0, 0, 0.55);
  --md-sys-elevation-5: 0 8px 12px 6px rgba(0, 0, 0, 0.52), 0 4px 4px 0 rgba(0, 0, 0, 0.60);
}

/* --- M3 Typography Scale, Shape Scale, Spacing (4px Grid) & Motion Tokens --- */
:root {
  --md-sys-typescale-font-brand: "Google Sans", "Plus Jakarta Sans", "Noto Sans JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", system-ui, -apple-system, sans-serif;
  --md-sys-typescale-font-plain: "Roboto", "Inter", "Noto Sans JP", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic UI", "Meiryo", system-ui, -apple-system, sans-serif;
  --md-sys-typescale-font-mono: "Roboto Mono", "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;

  /* M3 Shape Scale */
  --md-sys-shape-corner-none: 0px;
  --md-sys-shape-corner-extra-small: 4px;
  --md-sys-shape-corner-small: 8px;
  --md-sys-shape-corner-medium: 12px;
  --md-sys-shape-corner-large: 16px;
  --md-sys-shape-corner-extra-large: 28px;
  --md-sys-shape-corner-full: 9999px;

  /* M3 4px Spacing System */
  --md-space-0: 0px;
  --md-space-1: 4px;
  --md-space-2: 8px;
  --md-space-3: 12px;
  --md-space-4: 16px;
  --md-space-5: 20px;
  --md-space-6: 24px;
  --md-space-7: 28px;
  --md-space-8: 32px;
  --md-space-10: 40px;
  --md-space-12: 48px;
  --md-space-14: 56px;
  --md-space-16: 64px;

  /* M3 Motion Easing & Duration */
  --md-sys-motion-easing-emphasized: cubic-bezier(0.2, 0, 0, 1);
  --md-sys-motion-easing-emphasized-decelerate: cubic-bezier(0.05, 0.7, 0.1, 1);
  --md-sys-motion-easing-emphasized-accelerate: cubic-bezier(0.3, 0, 0.8, 0.15);
  --md-sys-motion-easing-standard: cubic-bezier(0.2, 0, 0, 1);
  --md-sys-motion-duration-short1: 100ms;
  --md-sys-motion-duration-short2: 150ms;
  --md-sys-motion-duration-medium1: 250ms;
  --md-sys-motion-duration-medium2: 300ms;
  --md-sys-motion-duration-long1: 400ms;
}

/* --- M3 Typography Classes --- */
.md-typescale-display-large {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(2.5rem, 4.5vw, 3.5625rem);
  line-height: 1.12;
  font-weight: 400;
  letter-spacing: -0.25px;
}
.md-typescale-display-medium {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(2.125rem, 3.6vw, 2.8125rem);
  line-height: 1.15;
  font-weight: 400;
  letter-spacing: 0px;
}
.md-typescale-display-small {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(1.75rem, 3vw, 2.25rem);
  line-height: 1.22;
  font-weight: 400;
  letter-spacing: 0px;
}
.md-typescale-headline-large {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(1.625rem, 2.5vw, 2rem);
  line-height: 1.25;
  font-weight: 600;
  letter-spacing: 0px;
}
.md-typescale-headline-medium {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(1.4rem, 2.2vw, 1.75rem);
  line-height: 1.28;
  font-weight: 600;
  letter-spacing: 0px;
}
.md-typescale-headline-small {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 1.5rem;
  line-height: 1.33;
  font-weight: 600;
  letter-spacing: 0px;
}
.md-typescale-title-large {
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 1.375rem;
  line-height: 1.27;
  font-weight: 600;
  letter-spacing: 0px;
}
.md-typescale-title-medium {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 1rem;
  line-height: 1.5;
  font-weight: 600;
  letter-spacing: 0.15px;
}
.md-typescale-title-small {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.875rem;
  line-height: 1.43;
  font-weight: 600;
  letter-spacing: 0.1px;
}
.md-typescale-body-large {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 1rem;
  line-height: 1.5;
  font-weight: 400;
  letter-spacing: 0.5px;
}
.md-typescale-body-medium {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.875rem;
  line-height: 1.43;
  font-weight: 400;
  letter-spacing: 0.25px;
}
.md-typescale-body-small {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.75rem;
  line-height: 1.33;
  font-weight: 400;
  letter-spacing: 0.4px;
}
.md-typescale-label-large {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.875rem;
  line-height: 1.43;
  font-weight: 600;
  letter-spacing: 0.1px;
}
.md-typescale-label-medium {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.75rem;
  line-height: 1.33;
  font-weight: 600;
  letter-spacing: 0.5px;
}
.md-typescale-label-small {
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.6875rem;
  line-height: 1.45;
  font-weight: 600;
  letter-spacing: 0.5px;
}

/* --- Common M3 Buttons --- */
.md-btn {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--md-space-2);
  min-height: 40px;
  padding: 0 var(--md-space-6);
  border-radius: var(--md-sys-shape-corner-full);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.875rem;
  font-weight: 600;
  letter-spacing: 0.1px;
  text-decoration: none;
  cursor: pointer;
  border: 1px solid transparent;
  user-select: none;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    transform var(--md-sys-motion-duration-short1) var(--md-sys-motion-easing-standard);
}
.md-btn:focus-visible {
  outline: 2px solid var(--md-sys-color-primary);
  outline-offset: 2px;
}
.md-btn:active:not(:disabled) {
  transform: scale(0.98);
}
.md-btn:disabled,
.md-btn[aria-disabled="true"] {
  opacity: 0.38;
  cursor: not-allowed;
  pointer-events: none;
  box-shadow: none;
}

.md-btn-filled {
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
}
.md-btn-filled:hover:not(:disabled) {
  box-shadow: var(--md-sys-elevation-1);
  filter: brightness(1.06);
}

.md-btn-tonal {
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}
.md-btn-tonal:hover:not(:disabled) {
  box-shadow: var(--md-sys-elevation-1);
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}

.md-btn-outlined {
  background-color: transparent;
  color: var(--md-sys-color-primary);
  border-color: var(--md-sys-color-outline);
}
.md-btn-outlined:hover:not(:disabled) {
  background-color: var(--md-sys-color-surface-container-high);
  border-color: var(--md-sys-color-primary);
}

.md-btn-text {
  background-color: transparent;
  color: var(--md-sys-color-primary);
  padding: 0 var(--md-space-3);
}
.md-btn-text:hover:not(:disabled) {
  background-color: var(--md-sys-color-surface-container-high);
}

.md-btn-elevated {
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-primary);
  box-shadow: var(--md-sys-elevation-1);
}
.md-btn-elevated:hover:not(:disabled) {
  background-color: var(--md-sys-color-surface-container);
  box-shadow: var(--md-sys-elevation-2);
}

.md-icon-btn {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid transparent;
  background-color: transparent;
  color: var(--md-sys-color-on-surface-variant);
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}
.md-icon-btn:hover:not(:disabled) {
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}
.md-icon-btn:focus-visible {
  outline: 2px solid var(--md-sys-color-primary);
  outline-offset: 2px;
}
.md-icon-btn.tonal {
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}

/* --- M3 Segmented Button Group --- */
.md-segmented-group {
  display: inline-flex;
  align-items: center;
  border: 1px solid var(--md-sys-color-outline);
  border-radius: var(--md-sys-shape-corner-full);
  overflow: hidden;
  background-color: var(--md-sys-color-surface);
}
.md-segmented-btn {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--md-space-2);
  min-height: 36px;
  padding: 0 var(--md-space-4);
  border: none;
  border-right: 1px solid var(--md-sys-color-outline);
  background-color: transparent;
  color: var(--md-sys-color-on-surface);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.8125rem;
  font-weight: 600;
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}
.md-segmented-btn:last-child {
  border-right: none;
}
.md-segmented-btn:hover:not(.active) {
  background-color: var(--md-sys-color-surface-container-high);
}
.md-segmented-btn.active,
.md-segmented-btn[aria-selected="true"],
.md-segmented-btn[aria-pressed="true"] {
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}

/* --- M3 Chips (Filter / Assist / Suggestion) --- */
.md-chip {
  position: relative;
  overflow: hidden;
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
  height: 32px;
  padding: 0 var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-small);
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface-variant);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.8125rem;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}
.md-chip:hover {
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}
.md-chip.active,
.md-chip[aria-selected="true"] {
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  border-color: transparent;
}

/* --- M3 Cards (Elevated, Filled, Outlined) --- */
.md-card-elevated {
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface);
  border-radius: var(--md-sys-shape-corner-large);
  box-shadow: var(--md-sys-elevation-1);
  border: 1px solid var(--md-sys-color-outline-variant);
}
.md-card-filled {
  background-color: var(--md-sys-color-surface-container-highest);
  color: var(--md-sys-color-on-surface);
  border-radius: var(--md-sys-shape-corner-large);
}
.md-card-outlined {
  background-color: var(--md-sys-color-surface);
  color: var(--md-sys-color-on-surface);
  border-radius: var(--md-sys-shape-corner-large);
  border: 1px solid var(--md-sys-color-outline-variant);
}

/* --- M3 Search Bar & Text Fields --- */
.md-search-bar {
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  min-height: 48px;
  padding: 0 var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
  border: 1px solid transparent;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}
.md-search-bar:focus-within {
  background-color: var(--md-sys-color-surface-container-lowest);
  border-color: var(--md-sys-color-primary);
  box-shadow: var(--md-sys-elevation-2);
}

.md-text-field-outlined {
  width: 100%;
  min-height: 44px;
  padding: var(--md-space-2) var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-small);
  border: 1px solid var(--md-sys-color-outline);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.9375rem;
  transition: border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}
.md-text-field-outlined:focus {
  outline: none;
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}

/* --- M3 Ripple Effect --- */
.md-ripple-surface {
  position: relative;
  overflow: hidden;
}
.md-ripple-wave {
  position: absolute;
  border-radius: 50%;
  transform: scale(0);
  pointer-events: none;
  background-color: currentColor;
  opacity: 0.16;
  animation: md-ripple-anim 550ms var(--md-sys-motion-easing-emphasized-decelerate) forwards;
}
@keyframes md-ripple-anim {
  to {
    transform: scale(3.5);
    opacity: 0;
  }
}

/* --- M3 Linear & Circular Progress Indicators --- */
.md-linear-progress {
  position: relative;
  width: 100%;
  height: 4px;
  background-color: var(--md-sys-color-surface-container-highest);
  border-radius: var(--md-sys-shape-corner-full);
  overflow: hidden;
}
.md-linear-progress::after {
  content: "";
  position: absolute;
  inset: 0;
  width: 45%;
  background-color: var(--md-sys-color-primary);
  border-radius: var(--md-sys-shape-corner-full);
  animation: md-linear-indeterminate 1.4s infinite var(--md-sys-motion-easing-emphasized);
}
@keyframes md-linear-indeterminate {
  0% { transform: translateX(-110%); }
  100% { transform: translateX(240%); }
}

/* --- M3 Dialog / Modal & Snackbar --- */
.md-dialog-scrim {
  position: fixed;
  inset: 0;
  background-color: var(--md-sys-color-scrim);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10000;
  padding: var(--md-space-4);
}
.md-dialog-surface {
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
  border-radius: var(--md-sys-shape-corner-extra-large);
  box-shadow: var(--md-sys-elevation-3);
  padding: var(--md-space-6);
  max-width: 460px;
  width: 100%;
  border: 1px solid var(--md-sys-color-outline-variant);
}

.md-snackbar {
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  border-radius: var(--md-sys-shape-corner-small);
  box-shadow: var(--md-sys-elevation-3);
  padding: var(--md-space-3) var(--md-space-4);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 0.875rem;
  font-weight: 500;
}

/* --- Reduced Motion Compliance (WCAG 2.2 AA) --- */
@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
"""

M3_JS: str = r"""/* ==========================================================================
   Material Design 3 Interactive Runtime (Theme Sync + M3 Ripple + A11y)
   ========================================================================== */
(function () {
  var THEME_KEY = "schoolhub.theme.v1";

  function getSavedTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") return saved;
    } catch (_) {}
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
      return "dark";
    }
    return "light";
  }

  function applyTheme(theme) {
    var resolved = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
    try {
      localStorage.setItem(THEME_KEY, resolved);
    } catch (_) {}
    window.dispatchEvent(new CustomEvent("m3-theme-change", { detail: { theme: resolved } }));
  }

  // Initialize theme immediately if not already set
  if (!document.documentElement.getAttribute("data-theme")) {
    document.documentElement.setAttribute("data-theme", getSavedTheme());
  }

  window.SchoolHubM3 = {
    getTheme: function () {
      return document.documentElement.getAttribute("data-theme") || getSavedTheme();
    },
    setTheme: applyTheme,
    toggleTheme: function () {
      var next = (document.documentElement.getAttribute("data-theme") === "dark") ? "light" : "dark";
      applyTheme(next);
      return next;
    }
  };

  // Cross-tab theme synchronization
  window.addEventListener("storage", function (ev) {
    if (ev && ev.key === THEME_KEY && (ev.newValue === "dark" || ev.newValue === "light")) {
      document.documentElement.setAttribute("data-theme", ev.newValue);
    }
  });

  // Material 3 Ripple Effect delegation
  var reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reducedMotion) {
    document.addEventListener("pointerdown", function (ev) {
      if (ev.button !== 0) return;
      var target = ev.target && ev.target.closest
        ? ev.target.closest(
            ".md-btn, .md-icon-btn, .md-chip, .md-segmented-btn, .md-nav-item, .tab-btn, .range-btn, .shortcut-btn, .theme-btn, .card.service, .md-ripple-surface"
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
  }
})();
"""


@router.get("/design-system/m3.css", include_in_schema=False)
def serve_m3_css() -> Response:
    return Response(
        content=M3_CSS,
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/design-system/m3.js", include_in_schema=False)
def serve_m3_js() -> Response:
    return Response(
        content=M3_JS,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )
