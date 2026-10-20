"""Tests for media_core.core.blur.

The contract that matters is *soft failure*: on a box without KWindowSystem,
or under a non-KWin compositor, blur must be skipped silently and never take
the window down with it. These tests therefore concentrate on the degraded
paths, which are the ones that would otherwise only show up on someone
else's machine.
"""

from __future__ import annotations

import ctypes

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget

from media_core.core import blur


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def fresh_blur():
    """Yield the blur module with its dlopen cache cleared, and restore it.

    _resolve() caches both success and failure in module globals, so tests
    that simulate a missing library must reset them or they leak into
    later tests.
    """
    saved = (blur._resolved, blur._enable_blur_behind)
    blur._resolved = False
    blur._enable_blur_behind = None
    yield blur
    blur._resolved, blur._enable_blur_behind = saved


def test_is_available_returns_bool(qapp):
    assert isinstance(blur.is_available(), bool)


def test_unshown_widget_is_declined(qapp):
    """windowHandle() is None until first show — must return False, not raise."""
    w = QWidget()
    assert blur.set_blur_behind(w, True) is False


def test_missing_library_degrades(fresh_blur, monkeypatch, qapp):
    """A box without KWindowSystem must not raise."""
    def boom(_name):
        raise OSError("libKF6WindowSystem.so.6: cannot open shared object file")

    monkeypatch.setattr(ctypes, "CDLL", boom)
    assert fresh_blur.is_available() is False

    w = QWidget()
    w.show()
    assert fresh_blur.set_blur_behind(w, True) is False
    w.close()


def test_missing_symbol_degrades(fresh_blur, monkeypatch, qapp):
    """If KF ever renames/changes the signature, fail soft rather than crash."""
    class NoSymbol:
        def __getattr__(self, name):
            raise AttributeError(name)

    monkeypatch.setattr(ctypes, "CDLL", lambda _name: NoSymbol())
    assert fresh_blur.is_available() is False


def test_resolution_is_cached(fresh_blur, monkeypatch, qapp):
    """dlopen must happen at most once, including when it fails."""
    calls = []

    def counting_cdll(name):
        calls.append(name)
        raise OSError("nope")

    monkeypatch.setattr(ctypes, "CDLL", counting_cdll)
    fresh_blur.is_available()
    fresh_blur.is_available()
    fresh_blur.is_available()
    assert len(calls) == 1


def test_call_failure_is_contained(fresh_blur, monkeypatch, qapp):
    """An exception from the native call is swallowed and reported as False."""
    def exploding_fn(*_args):
        raise RuntimeError("native call blew up")

    monkeypatch.setattr(fresh_blur, "_resolve", lambda: exploding_fn)

    w = QWidget()
    w.show()
    assert fresh_blur.set_blur_behind(w, True) is False
    w.close()


@pytest.mark.skipif(not blur.is_available(), reason="KWindowSystem not present")
def test_shown_widget_is_accepted(qapp):
    """With KWindowSystem present, a shown widget round-trips both ways."""
    w = QWidget()
    w.show()
    assert blur.set_blur_behind(w, True) is True
    assert blur.set_blur_behind(w, False) is True
    w.close()
