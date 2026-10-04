"""
Window texts from plain text language files, loaded at startup:

    lang/<code>.ini
        [info]
        name = Magyar
        [strings]
        btn_connect = Csatlakozás
        banner_opening = {port} megnyitása…

Placeholders are {name}; write \\n for a line break. Keys missing from a
language fall back to English. A lang folder next to the exe is read after
the bundled one, so a new language (or a fix) is just a file dropped there.
"""

import configparser
import ctypes
import glob
import os

DEFAULT = "en"
# Windows UI language (primary language id) -> file code
WIN_LANGS = {0x09: "en", 0x0E: "hu", 0x0C: "fr", 0x07: "de", 0x15: "pl"}


def _read(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(path, encoding="utf-8")
    return cp


class Lang:
    def __init__(self, dirs):
        self.files = {}                     # code -> path, later dirs win
        for d in dirs:
            for path in sorted(glob.glob(os.path.join(d, "*.ini"))):
                self.files[os.path.splitext(os.path.basename(path))[0].lower()] = path
        self.code = DEFAULT
        self.fallback = self._strings(DEFAULT)
        self.strings = self.fallback

    def _strings(self, code: str) -> dict:
        path = self.files.get(code)
        if not path:
            return {}
        cp = _read(path)
        if not cp.has_section("strings"):
            return {}
        return {k: v.replace("\\n", "\n") for k, v in cp.items("strings")}

    def available(self) -> dict:
        """code -> language name, for the settings dialog."""
        out = {}
        for code, path in sorted(self.files.items()):
            out[code] = _read(path).get("info", "name", fallback=code)
        return out

    @staticmethod
    def detect() -> str:
        try:
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        except Exception:
            return DEFAULT
        return WIN_LANGS.get(lang_id, DEFAULT)

    def load(self, code: str):
        code = (code or "auto").strip().lower()
        if code == "auto":
            code = self.detect()
        if code not in self.files:
            code = DEFAULT
        self.code = code
        self.strings = self._strings(code)

    def __call__(self, key: str, **kw) -> str:
        text = self.strings.get(key) or self.fallback.get(key) or key
        if kw:
            try:
                return text.format(**kw)
            except (KeyError, IndexError, ValueError):
                return self.fallback.get(key, key).format(**kw)
        return text
