from __future__ import annotations

import ctypes
import importlib
import os
import sys
from dataclasses import dataclass
from typing import Any

CGL_LIBRARY = "/System/Library/Frameworks/OpenGL.framework/OpenGL"
CGL_ATTRIBUTES = (73, 99, 0x3200, 8, 24, 11, 8, 0)


class GpuUnavailable(RuntimeError):
    pass


@dataclass
class GlContext:
    adapter: str
    driver: str
    vendor: str
    handle: Any = None

    def identity(self) -> dict[str, str]:
        return {"adapter": self.adapter, "driver": self.driver, "vendor": self.vendor}


def _strings() -> tuple[str, str, str]:
    try:
        gl: Any = importlib.import_module("OpenGL.GL")
    except ImportError as error:
        raise GpuUnavailable(f"PyOpenGL is not installed: {error}") from error
    values: list[str] = []
    for name in ("GL_RENDERER", "GL_VERSION", "GL_VENDOR"):
        raw = gl.glGetString(getattr(gl, name))
        if not raw:
            raise GpuUnavailable(f"the GL context reports no {name}")
        values.append(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw))
    return values[0], values[1], values[2]


def _cgl() -> GlContext:
    try:
        library = ctypes.CDLL(CGL_LIBRARY)
    except OSError as error:
        raise GpuUnavailable(f"OpenGL.framework cannot be loaded: {error}") from error
    attributes = (ctypes.c_int * (len(CGL_ATTRIBUTES) + 1))(*CGL_ATTRIBUTES, 0)
    pixel_format = ctypes.c_void_p()
    count = ctypes.c_int()
    library.CGLChoosePixelFormat.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.POINTER(ctypes.c_int)]
    library.CGLCreateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    library.CGLSetCurrentContext.argtypes = [ctypes.c_void_p]
    code = library.CGLChoosePixelFormat(attributes, ctypes.byref(pixel_format), ctypes.byref(count))
    if code != 0 or not pixel_format.value:
        raise GpuUnavailable(f"CGLChoosePixelFormat found no accelerated OpenGL 3.2 core pixel format (error {code})")
    context = ctypes.c_void_p()
    code = library.CGLCreateContext(pixel_format, None, ctypes.byref(context))
    if code != 0 or not context.value:
        raise GpuUnavailable(f"CGLCreateContext failed (error {code})")
    code = library.CGLSetCurrentContext(context)
    if code != 0:
        raise GpuUnavailable(f"CGLSetCurrentContext failed (error {code})")
    adapter, driver, vendor = _strings()
    return GlContext(adapter=adapter, driver=driver, vendor=vendor, handle=(library, pixel_format, context))


def _egl() -> GlContext:
    os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
    try:
        egl: Any = importlib.import_module("OpenGL.EGL")
    except Exception as error:
        raise GpuUnavailable(f"EGL is not available through PyOpenGL: {type(error).__name__}: {error}") from error
    try:
        display = egl.eglGetDisplay(egl.EGL_DEFAULT_DISPLAY)
        if not display:
            raise GpuUnavailable("eglGetDisplay returned no display")
        major, minor = egl.EGLint(), egl.EGLint()
        if not egl.eglInitialize(display, ctypes.pointer(major), ctypes.pointer(minor)):
            raise GpuUnavailable("eglInitialize failed")
        attributes = [
            egl.EGL_SURFACE_TYPE, egl.EGL_PBUFFER_BIT,
            egl.EGL_RENDERABLE_TYPE, egl.EGL_OPENGL_BIT,
            egl.EGL_RED_SIZE, 8, egl.EGL_GREEN_SIZE, 8, egl.EGL_BLUE_SIZE, 8, egl.EGL_ALPHA_SIZE, 8,
            egl.EGL_NONE,
        ]
        config = egl.EGLConfig()
        count = egl.EGLint()
        array = (egl.EGLint * len(attributes))(*attributes)
        if not egl.eglChooseConfig(display, array, ctypes.pointer(config), 1, ctypes.pointer(count)) or count.value < 1:
            raise GpuUnavailable("eglChooseConfig found no pbuffer config with OpenGL")
        surface_attributes = (egl.EGLint * 5)(egl.EGL_WIDTH, 1, egl.EGL_HEIGHT, 1, egl.EGL_NONE)
        surface = egl.eglCreatePbufferSurface(display, config, surface_attributes)
        if not surface:
            raise GpuUnavailable("eglCreatePbufferSurface failed")
        if not egl.eglBindAPI(egl.EGL_OPENGL_API):
            raise GpuUnavailable("eglBindAPI(EGL_OPENGL_API) failed")
        context = egl.eglCreateContext(display, config, egl.EGL_NO_CONTEXT, None)
        if not context:
            raise GpuUnavailable("eglCreateContext failed")
        if not egl.eglMakeCurrent(display, surface, surface, context):
            raise GpuUnavailable("eglMakeCurrent failed")
    except GpuUnavailable:
        raise
    except Exception as error:
        raise GpuUnavailable(f"EGL setup failed: {type(error).__name__}: {error}") from error
    adapter, driver, vendor = _strings()
    return GlContext(adapter=adapter, driver=driver, vendor=vendor, handle=(egl, display, surface, context))


def open_gl() -> GlContext:
    if sys.platform == "darwin":
        return _cgl()
    if sys.platform.startswith("linux"):
        return _egl()
    raise GpuUnavailable(f"GPU gfx renders need CGL (macOS) or EGL (Linux); this is {sys.platform}")


__all__ = ["CGL_ATTRIBUTES", "CGL_LIBRARY", "GlContext", "GpuUnavailable", "open_gl"]
