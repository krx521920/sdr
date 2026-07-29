"""Architecture checks for the framework-independent SDR domain."""

import ast
from pathlib import Path

DOMAIN_ROOT = Path(__file__).resolve().parents[1] / "domain"
SDR_ROOT = DOMAIN_ROOT.parent
FORBIDDEN_IMPORT_ROOTS = {
    "accounts",
    "automation",
    "cases",
    "contacts",
    "django",
    "integrations",
    "invoices",
    "leads",
    "opportunity",
    "orders",
    "tasks",
}


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_domain_does_not_depend_on_framework_or_adapters():
    violations = {
        path.name: sorted(_import_roots(path) & FORBIDDEN_IMPORT_ROOTS)
        for path in DOMAIN_ROOT.glob("*.py")
        if _import_roots(path) & FORBIDDEN_IMPORT_ROOTS
    }
    assert violations == {}


def test_sdr_does_not_import_concrete_integrations():
    violations = {
        str(path.relative_to(SDR_ROOT)): sorted(_import_roots(path) & {"integrations"})
        for path in SDR_ROOT.rglob("*.py")
        if "tests" not in path.parts and _import_roots(path) & {"integrations"}
    }
    assert violations == {}
