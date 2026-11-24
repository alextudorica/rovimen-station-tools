"""Import every station script under the oldest Python the fleet runs.

Run with a bare 3.8 interpreter (no third-party packages):
    uv run --python 3.8 --no-project python tests/station_py38_import_check.py

gmnro05 and gmnro08 run Python 3.8.10 (Ubuntu 20.04). A 3.9+ construct
evaluated at import time -- a module-level alias like ``tuple[int, int]``,
an unquoted ``list[str]`` annotation without ``from __future__ import
annotations``, a ``match`` statement -- takes down every script that imports
the module on those stations, and CI's 3.13 never notices.

Third-party packages are not installed here, so any import the interpreter
cannot resolve is replaced by a permissive stub. Only failures raised by the
station code itself are reported. Exit status 1 lists the modules that failed.
"""
import importlib
import importlib.abc
import importlib.machinery
import sys
import tempfile
import traceback
import types
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / 'rovimen-scripts'


class _StubMeta(type):
    def __getattr__(cls, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return _Stub

    def __getitem__(cls, item):
        return _Stub

    def __call__(cls, *args, **kwargs):
        return _Stub

    def __or__(cls, other):
        return _Stub

    def __ror__(cls, other):
        return _Stub

    def __iter__(cls):
        return iter(())

    def __len__(cls):
        return 0

    def __contains__(cls, item):
        return False


class _Stub(metaclass=_StubMeta):
    pass


class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(name)
        return _Stub


# Standard-library names that are absent on a 3.8 Linux station and must stay
# absent: Windows/Jython-only modules (stubbing winreg makes mimetypes think it
# is on Windows) and modules added after 3.8, which station code has to guard.
_NEVER_STUB = {
    '_winapi', '_winreg', 'winreg', 'msvcrt', 'nt', '_overlapped', 'org', 'java',
    '_scproxy', 'zoneinfo', 'graphlib', 'tomllib',
}


class _StubFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Last-resort finder: anything no real finder resolves becomes a stub."""

    stubbed: set = set()

    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in _NEVER_STUB:
            return None
        self.stubbed.add(fullname.split('.')[0])
        return importlib.machinery.ModuleSpec(fullname, self, is_package=True)

    def create_module(self, spec):
        mod = _StubModule(spec.name)
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        pass


def main() -> int:
    assert sys.version_info[:2] == (3, 8), 'run this under Python 3.8'
    # Compile from source every time: a stale __pycache__ entry with a
    # matching mtime and size would otherwise hide an edit.
    sys.dont_write_bytecode = True
    sys.pycache_prefix = tempfile.mkdtemp(prefix='py38check-')
    sys.path.insert(0, str(SCRIPTS))
    sys.meta_path.append(_StubFinder())
    failed = []
    names = sorted(p.stem for p in SCRIPTS.glob('*.py') if p.stem != '__init__')
    for name in names:
        try:
            importlib.import_module(name)
        except SystemExit:
            pass  # a script that exits at import is not a 3.8 problem
        except BaseException:
            failed.append((name, traceback.format_exc()))
    for name, tb in failed:
        print(f'FAIL {name}\n{tb}')
    print(f'{len(names) - len(failed)}/{len(names)} station modules import '
          f'on Python {sys.version.split()[0]}; stubbed: '
          f'{", ".join(sorted(_StubFinder.stubbed)) or "none"}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
