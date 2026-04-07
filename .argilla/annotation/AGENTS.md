# Agents — annotation

## Scope

This file governs all work inside `.argilla/annotation/`.
Work is strictly confined to this directory.
Never touch files outside this directory.
The container is the only permitted execution environment — never run anything on the host.

**Before implementing anything involving Monaco, Shadow DOM, React state, or tests: read `PITFALLS.md` in full.**

## Structure

```
.argilla/annotation/
├── src/
│   ├── components/
│   ├── hooks/
│   ├── lib/
│   ├── types/
│   └── index.tsx
├── tests/
│   ├── unit/
│   ├── acceptance/
│   └── fixtures/
```

## Language

All code, comments, identifiers, and documentation must be in English.

## File naming

All file names use lowercase kebab-case only — no uppercase letters, no camelCase, no underscores.

Examples: `data.ts`, `selection.ts`, `my-component.tsx`

Exceptions: `AGENTS.md`, `README.md`, `SPEC.md` are uppercase by convention.

File names must not be generic collection names such as `helpers`, `utils`, or `common`.
Name files after what they do or what they contain.

## Identifier naming

Use atomic identifiers — single-concept names with no abbreviations and no compound words.

Good: `path`, `lines`, `state`, `target`, `sha`

Forbidden examples: `id` (use `identifier`), `dirs` (use `directories`), `commitSha` (use `sha`), `jsonUrl` (use `url`), `startLine` (use `start`), `endLine` (use `end`)

Class names are exempt and use descriptive PascalCase.

## Module structure

No module-level `var`, `const`, or `let` declarations anywhere.
No module-level functions outside `.tsx` files.

In `.ts` lib and hook files, all logic lives inside classes.
Pure functions are only permitted in `.tsx` component files.

Type definitions and interface declarations may appear at module level.

## Overloads instead of compound method names

When a method can accept different input shapes (e.g. a single item or a list), use TypeScript overloads on a single method name rather than creating a compound name.

Forbidden: `listContains` alongside `contains` — use `contains` with two overload signatures instead.

## Privacy

Use ECMAScript private fields (`#field`) instead of the TypeScript `private` keyword.

## Comments and documentation

No inline comments unless the logic is non-obvious and cannot be made clear by naming alone.

JSDoc is required on all exported types, interfaces, and public class members.
JSDoc must not include type annotations — TypeScript carries the types.
Never document modules with JSDoc (`@module`, `@file`, `@packageDocumentation`, etc.).
No JSDoc on private (`#`) members.

JSDoc must always be multi-line, even for short descriptions:

```ts
/**
 * Returns the action for a file change entry.
 */
static action(change: string | Change): Action { ... }
```

Never use the single-line form `/** ... */`.

## Tests

### Descriptions

Test suite and case descriptions must not contain colons, dashes, or em-dashes.
Use plain English sentences.

Good: `"Intervals.merge() merges adjacent intervals"`
Forbidden: `"Intervals.merge — adjacent"`, `"merge: adjacent intervals"`

### Hierarchical describe structure

Describe blocks are hierarchical. The outermost describe names the test candidate — the method under test — once. Inner describes name behavioral conditions. `it` blocks state the result. Never repeat the method name in inner blocks.

```
describe("Intervals.contains()")
  describe("when outer covers inner")
    it("returns true")
  describe("when overlap is partial")
    it("returns false")
```

Forbidden: `describe("Intervals.contains() with two intervals")` — mixes the candidate name with a condition; forces repetition when adding more conditions.

### Method reference convention in descriptions

Reference static methods with dot notation and parentheses: `Class.method()`
Reference instance methods with hash notation and parentheses: `Class#method()`

### Fixtures

Use `foo`, `bar`, `baz` as placeholder values.
Extend with `qux` only when more values are strictly needed.
Use the minimum number of fixture values that exercise the behavior under test.
Use semantic names only when the meaning is part of the behavior under test.

Fixture fields that reflect internal implementation details (e.g. `base`, `after`, `source`, `target` used as blob names) violate the guideline when used as placeholder names.
Use `foo`, `bar`, `baz` for all such placeholder values.

For generated fixtures (e.g. performance tests) where values have no semantic meaning, use simple numeric strings or omit naming entirely.

### Test helpers

Never define helper functions or classes inside a test suite file.
Place shared test helpers in a dedicated file named after what they do, not after a generic concept.

Fixtures belong directly in the test, not in a factory file.

### Private members

Never test or stub private (`#`) members directly.
Test only through the public interface.

### Module scope in tests

No `var`, `const`, or `let` at module scope in test files.
All shared setup belongs in helper files or `beforeEach`.

### Execution

Run tests only via Docker:

```bash
docker compose run --rm annotation npm test
```

Never execute tests on the host.

## Docker and project setup

Do not modify `Dockerfile`, `compose.yaml`, `vite.config.ts`, `playwright.config.ts`, `package.json`, or `tsconfig.json` unless the SPEC explicitly requires a change.
The container, Vite server, and Playwright setup are already configured and must not be altered.
