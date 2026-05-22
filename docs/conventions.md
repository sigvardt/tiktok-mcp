# Conventions

This file records intentional conventions that static analysis may flag.
They are real design choices, not missing cleanup.

## 1. MCP tool return values are plain dicts

- MCP tool bodies return `dict[str, Any]` on purpose.
  FastMCP serializes that dict directly into the JSON-RPC `result`.
- The JSON-RPC transport already requires JSON-serializable objects.
  A second wrapper type would not change runtime behavior.
- Pydantic validation owns the request-side schema checks.
  The response boundary stays intentionally plain.
- Error envelopes remain plain dicts too.
  Keep the keys explicit in the tool body.
- Do not add dataclass wrappers only for the type checker.
  They do not improve the runtime MCP contract.
- Do not narrow the return type to a bespoke response model.
  That would split the contract without new safety.
- The caller is the MCP transport, not a Python API consumer.
  JSON serializability is the real requirement.
- Reviewers should treat the broad annotation as intentional.
  It documents the transport contract instead of hiding it.

Reviewer note:
- Keep the annotation broad unless the wire contract changes.
- Look for request validation in the tool body, not in the return type.
- Prefer explicit envelopes over hidden wrapper classes.
- If a tool returns a `dict`, document its keys in the tool docstring.
- Do not widen this note into a generic static-typing rationale.
- This section exists only for MCP tool return values.

## 2. `prompts/templates.py` and `server.py` import each other by design

- Prompt registration lives in `src/tiktok_mcp/prompts/templates.py`.
  The module uses FastMCP decorators to register prompt handlers.
- `src/tiktok_mcp/server.py` imports that module for side effects.
  That import ensures the decorators execute during process startup.
- Static import graphs therefore show a cycle.
  The runtime path is still valid because registration is deferred.
- The integration test suite exercises the subprocess boot path.
  That test proves the registry is populated correctly.
- basedpyright reports the cycle because it follows imports statically.
  It does not model decorator-driven registration side effects.
- Do not "fix" the cycle with a wrapper module just to silence the analyzer.
  The runtime already works and the cycle is intentional.
- Keep the side-effect import in `server.py` in the existing registration block.
  Reordering can break the boot story or readability for no gain.
- If a future refactor changes how prompts register, update the integration test first.
  Then update this note to match the new boot path.

Reviewer note:
- A static cycle is acceptable when the runtime registry is proven.
- Look for FastMCP decorators and side-effect imports together.
- basedpyright noise here is expected and documented.
- Do not introduce extra indirection just to make the graph look acyclic.
- Keep import-order changes scoped to the actual registration block.
- If the boot path changes, the test becomes the source of truth.

## 3. `HashedAudienceCSVStream` needs a BinaryIO cast

- `HashedAudienceCSVStream` provides the file-like methods `read`, `seek`, and `tell`.
  `httpx` only needs a binary file-like object for multipart uploads.
- The stream is structurally a `BinaryIO` at runtime.
  The cast documents that contract next to the upload payload.
- basedpyright cannot infer the structural match from duck typing alone.
  That is why the call site keeps a local cast.
- The cast is not a conversion and it does not alter runtime behavior.
  It only records the intended file-like interface for readers and type checkers.
- Keep the explanatory comment at the call site.
  That keeps the intent close to the `files=` tuple that needs it.
- Do not widen the suppression to a module-level ignore.
  The issue is local and the rationale is local.
- Do not add a wrapper adapter unless the stream interface itself changes.
  The current stream already satisfies the upload contract.
- If the stream contract expands, update this note and the call-site comment together.
  The point of the cast is to explain the existing runtime contract.

Reviewer note:
- Keep the `BinaryIO` cast adjacent to the multipart payload.
- basedpyright cannot infer the duck-typed file interface here.
- The stream already behaves like a file object at runtime.
- Do not replace the cast with a broader ignore.
- Only revisit this section if the stream interface changes.
- Keep the explanation short enough to survive future review passes.
