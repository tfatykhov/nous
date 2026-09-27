package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject

/**
 * RFC 6901 JSON Pointer over kotlinx JsonElement trees — a port of the
 * web client's `pointer.ts`, which mirrors the server's `_pointer_set`
 * byte-for-byte in semantics (spec §3.2 R7):
 *
 * - unescape `~1` → `/` BEFORE `~0` → `~` (the classic RFC 6901 §4 footgun);
 * - a missing intermediate becomes an ARRAY when the next token is numeric,
 *   otherwise an OBJECT — a local convention shared with the server, not RFC;
 * - `null` deletes (object key removed, array element removed).
 */
object Pointer {
    fun tokens(path: String): List<String> {
        if (path.isEmpty() || path == "/") return emptyList()
        require(path.startsWith("/")) { "pointer must start with '/': $path" }
        return path.substring(1).split("/").map { it.replace("~1", "/").replace("~0", "~") }
    }

    fun get(model: JsonElement?, path: String): JsonElement? {
        var node: JsonElement? = model
        for (tok in tokens(path)) {
            node = when (node) {
                is JsonObject -> node[tok]
                is JsonArray -> tok.toIntOrNull()?.let { i -> node.getOrNull(i) }
                else -> null
            }
            if (node == null) return null
        }
        return node
    }

    /** Returns a NEW root with the value set (or deleted when [value] is null). */
    fun set(model: JsonElement?, path: String, value: JsonElement?): JsonElement? {
        val toks = tokens(path)
        if (toks.isEmpty()) return value
        return setIn(model, toks, 0, value)
    }

    private fun isIndex(tok: String) = tok.isNotEmpty() && tok.all { it.isDigit() }

    private fun setIn(node: JsonElement?, toks: List<String>, i: Int, value: JsonElement?): JsonElement? {
        val tok = toks[i]
        val last = i == toks.size - 1
        val nextIsIndex = !last && isIndex(toks[i + 1])
        return when {
            node is JsonArray && isIndex(tok) -> {
                val idx = tok.toInt()
                val list = node.toMutableList()
                if (last) {
                    if (value == null) { if (idx < list.size) list.removeAt(idx) }
                    else { while (list.size <= idx) list.add(JsonNull); list[idx] = value }
                } else {
                    while (list.size <= idx) list.add(JsonNull)
                    val child = list[idx].let { if (it is JsonNull) null else it }
                    list[idx] = setIn(child ?: fresh(nextIsIndex), toks, i + 1, value) ?: JsonNull
                }
                JsonArray(list)
            }
            else -> {
                val map = (node as? JsonObject)?.toMutableMap() ?: mutableMapOf()
                if (last) {
                    if (value == null) map.remove(tok) else map[tok] = value
                } else {
                    val child = map[tok]?.let { if (it is JsonNull) null else it }
                    map[tok] = setIn(child ?: fresh(nextIsIndex), toks, i + 1, value) ?: JsonNull
                }
                JsonObject(map)
            }
        }
    }

    private fun fresh(array: Boolean): JsonElement = if (array) JsonArray(emptyList()) else JsonObject(emptyMap())

    /**
     * Resolve a possibly-relative binding path against a template scope
     * (`Children.svelte`'s `absolute()`): absolute paths pass through; a
     * relative one resolves under the nearest collection scope's base, or
     * `/` + path with no scope.
     */
    fun absolute(path: String, scopeBase: String?): String =
        if (path.startsWith("/")) path
        else if (scopeBase != null) "$scopeBase/$path"
        else "/$path"
}
