from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BACKEND_FILES = [
    ROOT / "client_core.py",
    ROOT / "ansi_parser.py",
    ROOT / "automation.py",
    ROOT / "persistence.py",
    ROOT / "session_controller.py",
    ROOT / "command_pipeline.py",
    ROOT / "lifecycle.py",
    ROOT / "variables.py",
    ROOT / "mapper_adapter.py",
]


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_backend_does_not_depend_on_qt_or_pyside():
    offenders: list[tuple[str, str]] = []
    for path in BACKEND_FILES:
        for name in _imports(path):
            if name == "qt" or name.startswith("qt.") or name == "PySide6" or name.startswith("PySide6."):
                offenders.append((path.name, name))
    assert offenders == []


def test_session_controller_does_not_reach_into_qt_layer():
    imports = _imports(ROOT / "session_controller.py")
    assert all(not (name == "qt" or name.startswith("qt.")) for name in imports)


def test_architecture_contract_documents_core_layer_direction():
    text = (ROOT / "docs" / "architecture" / "ARCHITECTURE_INVARIANTS.md").read_text(encoding="utf-8")
    assert "session_controller" in text
    assert "Qt bridge / Qt widgets" in text
    assert "must not import PySide6" in text


def test_session_controller_uses_owned_task_creation_only():
    source = (ROOT / "session_controller.py").read_text(encoding="utf-8")
    assert "asyncio.create_task" not in source
    assert "self._task_owner.create" in source


def test_command_transport_send_is_confined_to_boundary_helper():
    tree = ast.parse((ROOT / "session_controller.py").read_text(encoding="utf-8"))
    offenders = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.function = None

        def visit_FunctionDef(self, node):
            previous = self.function
            self.function = node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "send_line":
                if self.function != "_send_transport_line":
                    offenders.append(self.function)
            self.generic_visit(node)

    Visitor().visit(tree)
    assert offenders == []


def test_root_roadmap_tracks_current_and_planned_features():
    roadmap = (ROOT / 'ROADMAP.md').read_text(encoding='utf-8')
    assert 'Current 1.0 feature set' in roadmap
    assert 'Current release: Mapper 6' in roadmap
    assert 'Planned next' in roadmap
    assert '1.0 release candidate work' in roadmap


def test_graphical_mapper_view_has_no_controller_or_transport_dependency():
    text = (ROOT / 'qt' / 'map_view.py').read_text(encoding='utf-8')
    assert 'MudSessionController' not in text
    assert 'MudConnection' not in text
    assert '.send_line(' not in text
    assert 'CommandPipeline' not in text


def test_mapper_adapter_layer_cannot_import_controller_transport_or_qt():
    imports = _imports(ROOT / 'mapper_adapter.py')
    forbidden = {'session_controller', 'client_core', 'command_pipeline', 'mapper', 'PySide6', 'qt'}
    assert all(name.split('.')[0] not in forbidden for name in imports)


def test_mapper_adapter_registration_is_explicit_and_capability_based():
    text = (ROOT / 'mapper_adapter.py').read_text(encoding='utf-8')
    controller = (ROOT / 'session_controller.py').read_text(encoding='utf-8')
    assert 'class MapperAdapterRegistry' in text
    assert 'register_mapper_adapter' in text
    assert 'MapperAdapterCapability.ROOM_IDENTITY' in controller
    assert 'mapper_adapter_key == "manual"' not in controller


def test_documentation_hierarchy_has_an_index_and_release_history():
    docs_index = ROOT / "docs" / "README.md"
    changelog = ROOT / "CHANGELOG.md"
    assert docs_index.exists()
    assert changelog.exists()
    text = docs_index.read_text(encoding="utf-8")
    assert "Architecture" in text
    assert "Release reports" in text
    assert "Reviews" in text



def test_gitignore_excludes_generated_python_artifacts():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("__pycache__/", "*.py[cod]", ".pytest_cache/", ".venv/", "venv/"):
        assert pattern in text
