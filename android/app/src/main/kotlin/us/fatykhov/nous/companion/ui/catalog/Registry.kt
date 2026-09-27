package us.fatykhov.nous.companion.ui.catalog

import us.fatykhov.nous.companion.ui.RendererFn

/**
 * A2UI component name → Compose renderer. `CoverageManifestTest` asserts
 * this set equals the manifest's `ported` set (spec §8.3); `:core.Registry`
 * mirrors it for the JVM-only sweep.
 */
object Registry {
    val renderers: Map<String, RendererFn> = mapOf(
        // basic
        "Text" to { n -> TextView(n) },
        "Image" to { n -> ImageView(n) },
        "Icon" to { n -> IconView(n) },
        "Row" to { n -> RowView(n) },
        "Column" to { n -> ColumnView(n) },
        "List" to { n -> ListView(n) },
        "Card" to { n -> CardView(n) },
        "Divider" to { n -> DividerView(n) },
        "Button" to { n -> ButtonView(n) },
        "TextField" to { n -> TextFieldView(n) },
        "CheckBox" to { n -> CheckBoxView(n) },
        "ChoicePicker" to { n -> ChoicePickerView(n) },
        "Slider" to { n -> SliderView(n) },
        "DateTimeInput" to { n -> DateTimeInputView(n) },
        "Modal" to { n -> ModalView(n) },
        "Tabs" to { n -> TabsView(n) },
        // nous-core (the six builders' vocabulary)
        "ApprovalPanel" to { n -> ApprovalPanelView(n) },
        "ActionReviewCard" to { n -> ActionReviewCardView(n) },
        "StatTile" to { n -> StatTileView(n) },
        "StatRow" to { n -> StatRowView(n) },
        "KeyValueTable" to { n -> KeyValueTableView(n) },
        "DecisionCard" to { n -> DecisionCardView(n) },
        "ConfidenceMeter" to { n -> ConfidenceMeterView(n) },
        "Timeline" to { n -> TimelineView(n) },
        "DagGraph" to { n -> DagGraphView(n) },
        "MemoryGraph" to { n -> MemoryGraphView(n) },
    )
    val names: Set<String> get() = renderers.keys
}
