package us.fatykhov.nous.companion.core

/**
 * The components `:core` knows how to WALK. `:app`'s `ui.catalog.Registry`
 * owns the renderers; `catalog-coverage.json` is the authority both the
 * JUnit manifest test and the always-on Python ratchet check against
 * (spec §8.3). Keep the three in step — the tests fail otherwise.
 *
 * PR 3: the six template builders' vocabulary + basic layout/display.
 * PR 4: the basic-catalog inputs, Modal and Tabs (two-way binding).
 * PR 5: micro-app chrome, charts and the F096 report vocabulary. Only
 * Video/AudioPlayer remain unsupported (they are unimplemented on the web too).
 */
object Registry {
    val ported: Set<String> = setOf(
        "Text", "Image", "Icon", "Row", "Column", "List", "Card", "Divider", "Button",
        "TextField", "CheckBox", "ChoicePicker", "Slider", "DateTimeInput", "Modal", "Tabs",
        "ApprovalPanel", "ActionReviewCard", "StatTile", "StatRow", "KeyValueTable",
        "DecisionCard", "ConfidenceMeter", "Timeline", "DagGraph", "MemoryGraph",
        "AppHeader", "AppFooter", "Section", "Sparkline", "LineChart", "BarChart",
        "MetricCard", "ScoreCard", "DeltaList", "DataTable", "ChipRow",
    )
}
