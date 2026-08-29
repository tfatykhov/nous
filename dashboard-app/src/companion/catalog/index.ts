// F092: catalog registry — A2UI component name -> Svelte adapter.
//
// Registered by NAME, deliberately not keyed by catalogId: the server
// validates catalog membership, and the renderer supports exactly the two
// catalogs this deployment serves (basic subset + nous-core). Wave 2 adds:
// Image, Icon, CheckBox, ChoicePicker, Slider, DateTimeInput, Tabs, Modal,
// StatTile, KeyValueTable.
import type { Component } from 'svelte';

import Text from './Text.svelte';
import Column from './Column.svelte';
import Row from './Row.svelte';
import ListView from './ListView.svelte';
import CardView from './CardView.svelte';
import DividerView from './DividerView.svelte';
import ButtonView from './ButtonView.svelte';
import TextFieldView from './TextFieldView.svelte';
import ApprovalPanelView from './ApprovalPanelView.svelte';
import ActionReviewCardView from './ActionReviewCardView.svelte';

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const registry: Record<string, Component<any>> = {
  Text,
  Column,
  Row,
  List: ListView,
  Card: CardView,
  Divider: DividerView,
  Button: ButtonView,
  TextField: TextFieldView,
  ApprovalPanel: ApprovalPanelView,
  ActionReviewCard: ActionReviewCardView,
};
