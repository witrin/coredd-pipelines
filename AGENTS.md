# Agents Registry

## Agent Guidelines
- **Module/package naming**: Prefer atomic snake_case names and avoid compound names when a clear single-concept name is possible.
- **Variable naming**: Prefer atomic names and avoid compound names where possible; treat this as a guideline, not a strict rule.
- **Forbidden patterns**: Do not add defensive revalidation of already guaranteed upstream contracts; do not encode soft contracts with `| None` unless `None` is part of the explicit contract.
- **Other rules**: Run tests only through Docker-based pipelines in this repository; do not use host-local test execution.
- **Markdown formatting**: Keep sentences on a single line; insert blank lines only to separate paragraphs.
- **Lists**: Do not nest bullets or place CLI examples inside enumerations; keep lists flat and concise.
- **CLI snippets**: Place multi-line commands in fenced code blocks separated by blank lines; never embed them inside list items.

## Coding Guidelines

- **No module-level functions**: Always use classes, unless external API requires functions.
- **Private methods**: Calling `_private()` from outside the class is forbidden. Extend public interface, derive new class, or reconsider design.
- **No constants**: Avoid class-level or module-level constants.
- **Documentation**: No PyDoc in private methods. Always English.
- **Naming**: prefer atomic names for variables, methods, and module/package names; classes are exempt and can use descriptive PascalCase.
