import ast
from pathlib import Path


SRC = Path("src/secondbrain")


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_app_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "app.py")


def test_worker_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "worker.py")


def test_worker_does_not_import_receipts():
    assert "secondbrain.receipts" not in imported_modules(SRC / "worker.py")


def test_discord_adapter_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "discord_capture.py")


def test_reconcile_helper_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "reconcile.py")


def test_only_capture_service_opens_ledger_in_production():
    ledger_openers = []
    for path in SRC.glob("*.py"):
        if path.name in {"ledger.py", "capture_service.py"}:
            continue
        if "Ledger" in call_names(path):
            ledger_openers.append(path.name)

    assert ledger_openers == []
