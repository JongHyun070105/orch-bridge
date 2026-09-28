from pathlib import Path
import py_compile

def test_all_python_sources_compile():
    root=Path(__file__).resolve().parents[1]/'app'
    for p in root.glob('*.py'):
        py_compile.compile(str(p),doraise=True)
