package us.fatykhov.nous.companion.core

/**
 * The components `:core` knows how to WALK. `:app` owns the renderers and
 * registers each one here as it lands; `catalog-coverage.json` is the
 * authority the JUnit manifest test checks this set against (spec §8.3).
 *
 * PR 2 ships it empty: every catalog component is `unsupported`, so every
 * fixture renders the §8.2 fallback card and the sweep still proves the
 * walker, the store and the binding engine handle every real tree.
 */
object Registry {
    val ported: Set<String> = emptySet()
}
