package us.fatykhov.nous.companion.core

/**
 * The components `:core` knows how to WALK. `:app`'s `ui.catalog.Registry`
 * owns the renderers; `catalog-coverage.json` is the authority both the
 * JUnit manifest test and the always-on Python ratchet check against
 * (spec §8.3). Keep the three in step — the tests fail otherwise.
 *
 * PR 3: the vocabulary the six template builders emit, plus the basic
 * layout/display components they use.
 */
object Registry {
    val ported: Set<String> = setOf(
        "Text", "Image", "Icon", "Row", "Column", "List", "Card", "Divider", "Button",
        "ApprovalPanel", "ActionReviewCard", "StatTile", "StatRow", "KeyValueTable",
        "DecisionCard", "ConfidenceMeter", "Timeline", "DagGraph", "MemoryGraph",
    )
}
