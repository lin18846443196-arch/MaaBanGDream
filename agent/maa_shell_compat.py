"""Complete the Shell ctypes declarations missing from the bundled SDK.

Keep vendor files intact. Bind against the active framework/AgentServer DLL;
never open a second framework or change an existing controller handle.
"""
from ctypes import c_char_p, c_int64

from maa.controller import Controller
from maa.define import MaaBool, MaaControllerHandle, MaaCtrlId, MaaStringBufferHandle
from maa.library import Library


def ensure_shell_api(controller):
    if not isinstance(controller, Controller):
        return  # Test doubles and other controller implementations own their API.
    library = Library.framework()
    post = library.MaaControllerPostShell
    if post.argtypes is None:
        post.argtypes = [MaaControllerHandle, c_char_p, c_int64]
        post.restype = MaaCtrlId
    output = library.MaaControllerGetShellOutput
    if output.argtypes is None:
        output.argtypes = [MaaControllerHandle, MaaStringBufferHandle]
        output.restype = MaaBool


def shell_output(controller, command: str, timeout: int = 8000) -> str:
    ensure_shell_api(controller)
    job = controller.post_shell(command, timeout).wait()
    if not job.succeeded:
        raise RuntimeError(f'Maa Shell job failed: status={job.status}')
    return str(job.get() or '')
