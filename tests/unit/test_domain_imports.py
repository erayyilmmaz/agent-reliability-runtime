import ast
from pathlib import Path


def test_domain_does_not_depend_on_delivery_frameworks() -> None:
    domain_root = Path("src/agent_runtime/domain")
    forbidden = {"fastapi", "aio_pika", "sqlalchemy", "redis"}

    for source_file in domain_root.rglob("*.py"):
        tree = ast.parse(source_file.read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        assert forbidden.isdisjoint(imported), f"{source_file} imports a delivery dependency"
