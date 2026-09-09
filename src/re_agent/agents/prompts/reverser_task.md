Reverse the following function into clean ${language_standard}.

**Target:** ${class_name}::${function_name} at ${address}

**Ghidra Decompile:**
```
${decompiled}
```

**Cross-references (calls from this function):**
${xrefs}

**Struct/type context:**
${structs}

**Existing source context:**
${source_context}

**Structured reverse-engineering evidence:**
${investigation_context}

**Project-specific rules:**
${project_rules}

Requirements:
1. Match every branch and call from the decompile
2. Use member names only when supported by supplied type evidence. Otherwise preserve the exact byte offset and access width, and document the unresolved layout; do not invent structs or member names.
3. Preserve exact expression/operand order
4. Use existing project patterns and naming conventions
5. Output exactly one complete function implementation in a ```cpp block. Do not wrap it in namespace/class definitions or add helper type/function definitions; report missing declarations outside the code block.
6. End with: REVERSED_FUNCTION: ${class_name}::${function_name} (${address})
