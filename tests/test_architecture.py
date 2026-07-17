from __future__ import annotations

import ast
import sys
from collections.abc import Iterable
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "evolvable_memory"
STANDARD_LIBRARY = sys.stdlib_module_names | {"__future__"}


def _resolved_imports(source_path: Path) -> Iterable[tuple[int, str]]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    relative_parent = source_path.relative_to(PACKAGE_ROOT).parent.parts
    current_package = ("evolvable_memory", *relative_parent)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level == 0:
            if node.module is not None:
                yield node.lineno, node.module
            continue

        parent_count = node.level - 1
        base = current_package[: len(current_package) - parent_count]
        suffix = tuple(node.module.split(".")) if node.module else ()
        yield node.lineno, ".".join((*base, *suffix))


def _is_allowed_internal(module: str, allowed_packages: tuple[str, ...]) -> bool:
    return any(
        module == package or module.startswith(f"{package}.") for package in allowed_packages
    )


def _layer_violations(layer: str, allowed_internal: tuple[str, ...]) -> list[str]:
    violations: list[str] = []
    for source_path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
        for line_number, module in _resolved_imports(source_path):
            if module == "evolvable_memory" or module.startswith("evolvable_memory."):
                if not _is_allowed_internal(module, allowed_internal):
                    violations.append(f"{source_path.name}:{line_number} imports {module}")
                continue

            if module.partition(".")[0] not in STANDARD_LIBRARY:
                violations.append(f"{source_path.name}:{line_number} imports third-party {module}")
    return violations


def test_domain_remains_framework_free_and_depends_only_on_domain() -> None:
    violations = _layer_violations("domain", ("evolvable_memory.domain",))

    assert violations == [], "Domain dependency boundary violated:\n" + "\n".join(violations)


def test_application_depends_only_on_application_and_domain() -> None:
    violations = _layer_violations(
        "application",
        ("evolvable_memory.application", "evolvable_memory.domain"),
    )

    assert violations == [], "Application dependency boundary violated:\n" + "\n".join(violations)
