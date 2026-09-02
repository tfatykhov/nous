// F092: catalog registry — A2UI component name -> Svelte adapter.
//
// Registered by NAME, deliberately not keyed by catalogId: the server
// validates catalog membership, and the renderer supports exactly the two
// catalogs this deployment serves (basic subset + nous-core).
//
// Video and AudioPlayer are the only basic-catalog components left
// unimplemented — deliberately out of scope (plan §4, spec §8.1). They fall
// through to the Renderer's unknown-component placeholder.
import type { Component } from 'svelte';

import Text from './Text.svelte';
import Column from './Column.svelte';
import Row from './Row.svelte';
import ListView from './ListView.svelte';
import CardView from './CardView.svelte';
import DividerView from './DividerView.svelte';
import ButtonView from './ButtonView.svelte';
import TextFieldView from './TextFieldView.svelte';
import ImageView from './ImageView.svelte';
import IconView from './IconView.svelte';
import CheckBoxView from './CheckBoxView.svelte';
import ChoicePickerView from './ChoicePickerView.svelte';
import SliderView from './SliderView.svelte';
import DateTimeInputView from './DateTimeInputView.svelte';
import TabsView from './TabsView.svelte';
import ModalView from './ModalView.svelte';
import ApprovalPanelView from './ApprovalPanelView.svelte';
import ActionReviewCardView from './ActionReviewCardView.svelte';
import StatTileView from './StatTileView.svelte';
import KeyValueTableView from './KeyValueTableView.svelte';
import DecisionCardView from './DecisionCardView.svelte';
import ConfidenceMeterView from './ConfidenceMeterView.svelte';
import MemoryGraphView from './MemoryGraphView.svelte';
import DagGraphView from './DagGraphView.svelte';
import AppHeaderView from './AppHeaderView.svelte';
import AppFooterView from './AppFooterView.svelte';
import SectionView from './SectionView.svelte';
import StatRowView from './StatRowView.svelte';
import TimelineView from './TimelineView.svelte';
import SparklineView from './SparklineView.svelte';
import LineChartView from './LineChartView.svelte';
import BarChartView from './BarChartView.svelte';
import MetricCardView from './MetricCardView.svelte';
import ScoreCardView from './ScoreCardView.svelte';
import DeltaListView from './DeltaListView.svelte';
import DataTableView from './DataTableView.svelte';
import ChipRowView from './ChipRowView.svelte';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const registry: Record<string, Component<any>> = {
  // basic catalog
  Text,
  Image: ImageView,
  Icon: IconView,
  Row,
  Column,
  List: ListView,
  Card: CardView,
  Tabs: TabsView,
  Modal: ModalView,
  Divider: DividerView,
  Button: ButtonView,
  CheckBox: CheckBoxView,
  TextField: TextFieldView,
  DateTimeInput: DateTimeInputView,
  ChoicePicker: ChoicePickerView,
  Slider: SliderView,
  // nous-core catalog
  ApprovalPanel: ApprovalPanelView,
  ActionReviewCard: ActionReviewCardView,
  StatTile: StatTileView,
  KeyValueTable: KeyValueTableView,
  DecisionCard: DecisionCardView,
  ConfidenceMeter: ConfidenceMeterView,
  MemoryGraph: MemoryGraphView,
  DagGraph: DagGraphView,
  // F092.1 micro-apps
  AppHeader: AppHeaderView,
  AppFooter: AppFooterView,
  Section: SectionView,
  StatRow: StatRowView,
  Timeline: TimelineView,
  // F094 charts
  Sparkline: SparklineView,
  LineChart: LineChartView,
  BarChart: BarChartView,
  // F096 report vocabulary
  MetricCard: MetricCardView,
  ScoreCard: ScoreCardView,
  DeltaList: DeltaListView,
  DataTable: DataTableView,
  ChipRow: ChipRowView,
};
