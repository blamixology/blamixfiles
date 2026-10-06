"""The interface themes (colors only; no Qt), shared by the desktop app and the terminal UI.
The same set as BlamixShell: Midnight, Graphite, Nord, Solarized Dark, Light and High contrast."""
from __future__ import annotations

# `colors` are the base tokens; the app derives hover / selected / banner … from them.
THEMES: dict[str, dict] = {
    "Midnight": {"dark": True, "colors": {
        "bg": "#0b0d12", "sidebar": "#10131a", "surface": "#151923", "surface2": "#1b2030", "hover": "#222839",
        "border": "#232838", "text": "#e6e9f2", "muted": "#8a92a8", "faint": "#5b6378", "accent": "#7c8cff",
        "accent2": "#a78bfa", "ok": "#3ddc97", "warn": "#ffc857", "danger": "#ff5d73", "on_accent": "#0a0c11"}},
    "Graphite": {"dark": True, "colors": {
        "bg": "#121212", "sidebar": "#171717", "surface": "#1e1e1e", "surface2": "#262626", "hover": "#2f2f2f",
        "border": "#333333", "text": "#e8e8e8", "muted": "#a0a0a0", "faint": "#6b6b6b", "accent": "#5aa0ff",
        "accent2": "#b48ead", "ok": "#4cd38a", "warn": "#ffcb6b", "danger": "#ff6b6b", "on_accent": "#0a0a0a"}},
    "Nord": {"dark": True, "colors": {
        "bg": "#2e3440", "sidebar": "#2b303b", "surface": "#3b4252", "surface2": "#434c5e", "hover": "#4c566a",
        "border": "#58637a", "text": "#eceff4", "muted": "#bcc6d8", "faint": "#7f8aa0", "accent": "#88c0d0",
        "accent2": "#b48ead", "ok": "#a3be8c", "warn": "#ebcb8b", "danger": "#e07a85", "on_accent": "#2d3340"}},
    "Solarized Dark": {"dark": True, "colors": {
        "bg": "#002b36", "sidebar": "#00252e", "surface": "#073642", "surface2": "#0b4452", "hover": "#124b5a",
        "border": "#0f4a58", "text": "#eee8d5", "muted": "#a6b3b3", "faint": "#6f878f", "accent": "#4aa3e8",
        "accent2": "#8a8fe0", "ok": "#9db300", "warn": "#d4a017", "danger": "#ff6b66", "on_accent": "#012c37"}},
    "Light": {"dark": False, "colors": {
        "bg": "#f4f5f8", "sidebar": "#eceef3", "surface": "#ffffff", "surface2": "#f0f2f7", "hover": "#e4e8f0",
        "border": "#d8dce6", "text": "#1d2433", "muted": "#5b6478", "faint": "#8a92a6", "accent": "#4f5fe8",
        "accent2": "#7c5cf0", "ok": "#1a9f6a", "warn": "#b7791f", "danger": "#d63a4f", "on_accent": "#fefeff"}},
    "High contrast": {"dark": True, "colors": {
        "bg": "#000000", "sidebar": "#0a0a0a", "surface": "#111111", "surface2": "#1a1a1a", "hover": "#2a2a2a",
        "border": "#6b6b6b", "text": "#ffffff", "muted": "#d0d0d0", "faint": "#a0a0a0", "accent": "#ffd400",
        "accent2": "#00e5ff", "ok": "#00ff7f", "warn": "#ffb000", "danger": "#ff4d4d", "on_accent": "#010101"}},
}
DEFAULT_THEME = "Midnight"
SYSTEM = "System"                      # follow the OS light/dark setting (Midnight / Light)
LEGACY = {"dark": "Midnight", "light": "Light", "system": SYSTEM}
