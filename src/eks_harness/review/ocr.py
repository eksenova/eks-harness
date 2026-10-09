from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SOURCE = Path(__file__).with_name("ocr_vision.swift")
_LOCK = threading.Lock()
_ENGINES: dict[str, "Engine"] = {}


class OcrError(RuntimeError):
    pass


@dataclass
class Line:
    text: str
    confidence: float = 1.0


def normalize(text: str) -> str:
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i")
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return " ".join(text.split())


def contains(haystack: str, needle: str) -> bool:
    wanted = normalize(needle)
    return bool(wanted) and wanted in normalize(haystack)


class Engine:
    name = "engine"

    def read(self, images: list[Path], languages: list[str]) -> list[list[Line]]:
        raise NotImplementedError


class VisionEngine(Engine):
    name = "vision"

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def read(self, images: list[Path], languages: list[str]) -> list[list[Line]]:
        codes = [_vision_language(code) for code in languages]
        result = subprocess.run([str(self.binary), "--lang", ",".join(codes), *map(str, images)],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise OcrError(f"vision OCR failed: {result.stderr.strip()[-400:]}")
        found: dict[str, list[Line]] = {}
        for row in result.stdout.splitlines():
            try:
                data = json.loads(row)
            except ValueError:
                continue
            found[data["file"]] = [Line(str(x.get("text") or ""), float(x.get("confidence") or 0))
                                   for x in data.get("lines") or []]
        return [found.get(str(path), []) for path in images]


class TesseractEngine(Engine):
    name = "tesseract"

    def __init__(self, binary: str) -> None:
        self.binary = binary
        listed = subprocess.run([binary, "--list-langs"], capture_output=True, text=True)
        self.available = {line.strip() for line in listed.stdout.splitlines()[1:] if line.strip()}

    def read(self, images: list[Path], languages: list[str]) -> list[list[Line]]:
        codes = [c for c in (_tesseract_language(code) for code in languages) if c in self.available] or ["eng"]
        out = []
        for image in images:
            result = subprocess.run([self.binary, str(image), "stdout", "-l", "+".join(codes), "--psm", "6"],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                raise OcrError(f"tesseract failed: {result.stderr.strip()[-400:]}")
            out.append([Line(line) for line in result.stdout.splitlines() if line.strip()])
        return out


def _vision_language(code: str) -> str:
    return {"tur": "tr-TR", "tr": "tr-TR", "eng": "en-US", "en": "en-US"}.get(code, code)


def _tesseract_language(code: str) -> str:
    return {"tr-TR": "tur", "tr": "tur", "en-US": "eng", "en": "eng"}.get(code, code)


def _vision_binary(cache: Path) -> Path | None:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return None
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]
    binary = cache / f"eks-ocr-vision-{digest}"
    if binary.is_file():
        return binary
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ehx-ocr-") as tmp:
        built = Path(tmp) / "ocr"
        result = subprocess.run(["swiftc", "-O", str(SOURCE), "-o", str(built)], capture_output=True, text=True,
                                timeout=600)
        if result.returncode != 0 or not built.is_file():
            return None
        shutil.move(str(built), binary)
    return binary


def _cache_dir() -> Path:
    try:
        from eks_harness.paths import resolve_paths

        return resolve_paths().cache_dir / "bin"
    except Exception:
        return Path(tempfile.gettempdir()) / "eks-harness-bin"


def engine(name: str = "auto") -> Engine:
    with _LOCK:
        if name in _ENGINES:
            return _ENGINES[name]
        chosen: Engine | None = None
        if name in ("auto", "vision"):
            binary = _vision_binary(_cache_dir())
            if binary is not None:
                chosen = VisionEngine(binary)
            elif name == "vision":
                raise OcrError("macOS Vision OCR needs macOS with swiftc (Xcode command line tools)")
        if chosen is None and name in ("auto", "tesseract"):
            binary = shutil.which("tesseract")
            if binary:
                chosen = TesseractEngine(binary)
            elif name == "tesseract":
                raise OcrError("tesseract is not on PATH")
        if chosen is None:
            raise OcrError("no local OCR engine: install tesseract, or use macOS with swiftc")
        _ENGINES[name] = chosen
        return chosen


def read_images(images: list[Any], *, languages: list[str] | None = None, name: str = "auto") -> list[list[Line]]:
    languages = languages or ["tr-TR", "en-US"]
    with tempfile.TemporaryDirectory(prefix="ehx-ocr-") as tmp:
        paths = []
        for index, image in enumerate(images):
            path = Path(tmp) / f"{index:04d}.png"
            image.save(path)
            paths.append(path)
        return engine(name).read(paths, languages)
