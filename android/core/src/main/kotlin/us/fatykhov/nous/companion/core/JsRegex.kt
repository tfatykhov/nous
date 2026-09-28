package us.fatykhov.nous.companion.core

/**
 * Compile a regex written with JavaScript semantics so it behaves the same on
 * the JVM AND on Android — the two disagree with each other and with JS:
 *
 * - JS `\d` and `\w` are ASCII whether or not the `u` flag is set; JS `\s` is
 *   the Unicode WhiteSpace + LineTerminator set (NBSP, NNBSP, BOM, Zs…) whether
 *   or not the flag is set.
 * - The JVM's `\d`/`\w`/`\s` are ASCII unless `UNICODE_CHARACTER_CLASS` is set.
 * - Android's ICU-backed `Pattern` REJECTS that flag (`IllegalArgumentException:
 *   UNICODE_CHARACTER_CLASS flag not supported` — found on the emulator, where
 *   it crashed the first ScoreCard) and its `\d`/`\s`/`\w` are Unicode by default.
 *
 * So the classes are spelled out and the pattern is compiled with no flag on
 * either platform. `\p{…}` property classes are portable and pass through.
 * Every regex in `:core` goes through here (a test scans the sources).
 */
object JsRegex {
    /** JS `\s`, as the BODY of a character class (no brackets). */
    private const val WS = """ \t\n\u000B\f\r   -     　﻿"""
    private const val DIGIT = "0-9"
    private const val WORD = "A-Za-z0-9_"

    /** Rewrite `\d` `\w` `\s` (and negations outside a class) into explicit classes; everything else is copied. */
    fun translate(pattern: String): String {
        val sb = StringBuilder(pattern.length + 64)
        var i = 0
        var inClass = false
        while (i < pattern.length) {
            val c = pattern[i]
            if (c == '\\' && i + 1 < pattern.length) {
                val n = pattern[i + 1]
                val body = when (n) { 'd' -> DIGIT; 'w' -> WORD; 's' -> WS; else -> null }
                val negated = when (n) { 'D' -> DIGIT; 'W' -> WORD; 'S' -> WS; else -> null }
                when {
                    body != null -> sb.append(if (inClass) body else "[$body]")
                    negated != null && !inClass -> sb.append("[^$negated]")
                    else -> sb.append(c).append(n)   // a negated class INSIDE a class has no bracket form; left to the engine
                }
                i += 2
                continue
            }
            if (c == '[' && !inClass) inClass = true
            else if (c == ']' && inClass) inClass = false
            sb.append(c)
            i += 1
        }
        return sb.toString()
    }

    fun compile(pattern: String, vararg options: RegexOption): Regex = Regex(translate(pattern), options.toSet())
}
