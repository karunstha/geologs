"""The layers stay layered: each package imports only what lies beneath it."""
import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "geologs"
# What each layer may import from inside GeoLogs (besides itself).
# config (the environment, in one place) is open to every layer but the domain.
ALLOWED = {
    "domain": set(),
    "integrations": {"domain", "config"},
    "data": {"domain", "config"},
    "services": {"domain", "data", "integrations", "config"},
    "presentation": {"domain", "services", "config"},
}


def imports(path):
    """The geologs packages a module imports, anywhere in it (function-level imports too)."""
    layer = path.relative_to(PACKAGE).parts[0]
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                parts = (node.module or "").split(".")
                if parts[0] == "geologs" and len(parts) > 1:
                    found.add(parts[1])
            elif node.level == 1:
                found.add(layer)
            else:
                target = (node.module or "").split(".")[0]
                found.update([target] if target else [alias.name for alias in node.names])
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[1] for alias in node.names
                         if alias.name.startswith("geologs.") and "." in alias.name)
    return layer, found


class ArchitectureTests(unittest.TestCase):
    def test_layers_import_only_what_lies_beneath(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.parent == PACKAGE:
                continue  # geologs/__init__ and __main__ only start the command line
            layer, found = imports(path)
            with self.subTest(module=str(path.relative_to(PACKAGE))):
                self.assertLessEqual(found - {layer}, ALLOWED[layer])

    def test_sql_runs_only_in_the_data_package(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.relative_to(PACKAGE).parts[0] == "data":
                continue
            calls = [node for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr in ("execute", "executescript", "fetchone", "fetchall")]
            self.assertEqual(calls, [], str(path.relative_to(PACKAGE)))

    def test_only_config_reads_the_environment(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.name == "config.py" or path.relative_to(PACKAGE).parts[:2] == ("presentation", "cli.py"):
                continue  # the command line loads .env into the environment at start
            reads = [node for node in ast.walk(ast.parse(path.read_text()))
                     if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv")]
            self.assertEqual(reads, [], str(path.relative_to(PACKAGE)))

    def test_domain_uses_only_the_standard_library(self):
        for path in sorted((PACKAGE / "domain").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and not getattr(node, "level", 0):
                    names = [node.module] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
                    for name in names:
                        with self.subTest(module=path.name, imports=name):
                            self.assertNotIn(name.split(".")[0], ("geologs", "sqlite3", "psycopg", "urllib", "http"))


if __name__ == "__main__":
    unittest.main()
