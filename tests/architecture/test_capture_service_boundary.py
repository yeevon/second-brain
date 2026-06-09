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


def assigned_call_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name):
                names.add(node.value.func.id)
            elif isinstance(node.value.func, ast.Attribute):
                names.add(node.value.func.attr)
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


def test_capture_api_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "capture_api.py")


def test_api_server_does_not_import_ledger():
    assert "secondbrain.ledger" not in imported_modules(SRC / "api_server.py")


def test_capture_api_does_not_open_sqlite():
    assert "Ledger" not in call_names(SRC / "capture_api.py")


def test_api_routes_delegate_to_capture_service():
    source = (SRC / "capture_api.py").read_text(encoding="utf-8")

    assert "capture_service.get_capture" in source
    assert "capture_service.acknowledge_delivery_forwarded" in source
    assert "capture_service.acknowledge_delivery_classifying" in source
    assert "capture_service.acknowledge_delivery_filed" in source
    assert "capture_service.acknowledge_delivery_inbox" in source
    assert "capture_service.acknowledge_delivery_failed" in source
    assert "capture_service.schedule_delivery_retry" in source
    assert "capture_service.renew_delivery_lease" in source
    assert "capture_service.edit_receipt" in source


def test_legacy_retry_route_not_registered():
    source = (SRC / "capture_api.py").read_text(encoding="utf-8")
    assert '"/internal/captures/{capture_id}/retry"' not in source


def test_no_module_global_capture_service_is_constructed_by_capture_api():
    assert "CaptureService" not in assigned_call_names(SRC / "capture_api.py")
