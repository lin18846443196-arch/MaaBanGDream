from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar

from maa.context import Context


class ScreenRefreshCancelled(RuntimeError):
    pass


class ScreenRefreshInterrupted(RuntimeError):
    """A modal invalidated the caller's page; do not retry stale UI inputs."""


_frame_observer = ContextVar('screen_refresh_observer', default=None)
_capture_filter = ContextVar('screen_refresh_filter', default=None)


@contextmanager
def filter_captured_images(handler):
    """Task-local modal handler; unlike diagnostics, failures must propagate.

    Temporarily use None inside the handler to capture after dismissing a modal
    without recursion. Other tasks and native high-rate capture stay unchanged.
    """
    token = _capture_filter.set(handler)
    try:
        yield
    finally:
        _capture_filter.reset(token)


@contextmanager
def observe_captured_images(observer):
    """Observe existing captures in this task without a second controller thread."""
    token = _frame_observer.set(observer)
    try:
        yield
    finally:
        _frame_observer.reset(token)


def capture_image(context: Context, *, node: str = "CommonRefreshScreen") -> Any:
    """Ask MaaFramework to refresh once, then read the cached screenshot.

    Agent callbacks must not call ``post_screencap`` directly on this runtime:
    that reverse controller call can remain unresolved and prevent task stop.
    The dedicated pipeline node has no ``max_hit``, so nested runs cannot
    consume a business node's retained hit counter.
    """

    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task is stopping")
    detail = context.run_task(node)
    if context.tasker.stopping:
        raise ScreenRefreshCancelled("task stopped during screen refresh")
    if not detail or not detail.status.succeeded:
        raise RuntimeError("CommonRefreshScreen did not complete")
    image = context.tasker.controller.cached_image
    if image is None or getattr(image, "size", 1) == 0:
        raise RuntimeError("CommonRefreshScreen returned an empty image")
    observer = _frame_observer.get()
    if observer is not None:
        try:
            observer(image, node)
        except Exception as exc:
            print(f'ScreenRefresh diagnostic_warning={type(exc).__name__}: {exc}', flush=True)
    handler = _capture_filter.get()
    if handler is not None:
        with filter_captured_images(None):
            image = handler(image)
    return image
