package us.fatykhov.nous.companion.ui.catalog

import android.app.DatePickerDialog
import android.app.TimePickerDialog
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CheckboxDefaults
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.RadioButtonDefaults
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.window.Dialog
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.Inputs
import us.fatykhov.nous.companion.core.Node
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.LocalSurfaceHost
import us.fatykhov.nous.companion.ui.Render
import us.fatykhov.nous.companion.ui.str
import java.util.Calendar

@Composable
private fun Failures(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    for (f in host.functions.runChecks(node.props["checks"] as? JsonArray, host.ctx(node.scope)))
        Text(f.message ?: "Invalid.", color = t.crit, fontSize = 12.sp, modifier = Modifier.semantics { contentDescription = "check-failure" })
}

@Composable
private fun FieldLabel(text: String) {
    if (text.isNotEmpty()) Text(text, color = LocalNousTheme.current.muted, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
}

// -------------------------------------------------------------- TextField

@Composable
fun TextFieldView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val variant = node.props.str("variant") ?: "shortText"
    val label = host.text(node.props["label"], node.scope)
    val placeholder = host.text(node.props["placeholder"], node.scope)
    val bound = Inputs.boundPath(node.props, node.scope) != null
    // The model is the source of truth; the field re-reads it on every recomposition (store version bumps).
    val current = Inputs.textValue(Inputs.read(host.store, host.functions, host.surfaceId, node.props, node.scope))
    Column(verticalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.fillMaxWidth()) {
        FieldLabel(label)
        OutlinedTextField(
            value = current,
            onValueChange = { v -> Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonPrimitive(v)) },
            readOnly = !bound,
            placeholder = { Text(placeholder, color = t.muted) },
            singleLine = variant != "longText",
            minLines = if (variant == "longText") 3 else 1,
            visualTransformation = if (variant == "obscured") PasswordVisualTransformation() else VisualTransformation.None,
            keyboardOptions = KeyboardOptions(keyboardType = when (variant) { "number" -> KeyboardType.Number; "obscured" -> KeyboardType.Password; else -> KeyboardType.Text }),
            modifier = Modifier.fillMaxWidth().semantics { contentDescription = "textfield:${node.componentId}" },
            colors = us.fatykhov.nous.companion.ui.fieldColors(),
        )
        Failures(node)
    }
}

// --------------------------------------------------------------- CheckBox

@Composable
fun CheckBoxView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val checked = Inputs.checkValue(Inputs.read(host.store, host.functions, host.surfaceId, node.props, node.scope))
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.clickable { Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonPrimitive(!checked)) }) {
            Checkbox(checked = checked, onCheckedChange = { v -> Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonPrimitive(v)) },
                colors = CheckboxDefaults.colors(checkedColor = t.accent), modifier = Modifier.semantics { contentDescription = "checkbox:${node.componentId}" })
            Text(host.text(node.props["label"], node.scope), color = t.text, fontSize = 15.sp)
        }
        Failures(node)
    }
}

// ----------------------------------------------------------- ChoicePicker

@Composable
fun ChoicePickerView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val multiple = node.props.str("variant") == "multipleSelection"
    val chips = node.props.str("displayStyle") == "chips"
    val filterable = (node.props["filterable"] as? JsonPrimitive)?.content == "true"
    var filter by remember { mutableStateOf("") }
    val options = Inputs.options(node.props, host.functions, host.ctx(node.scope))
    val visible = Inputs.filtered(options, filter, filterable)
    val selected = Inputs.selected(Inputs.read(host.store, host.functions, host.surfaceId, node.props, node.scope))
    fun choose(value: String, checked: Boolean) {
        val next = Inputs.choose(selected, value, checked, multiple)
        Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonArray(next.map { JsonPrimitive(it) }))
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.fillMaxWidth()) {
        FieldLabel(host.text(node.props["label"], node.scope))
        if (filterable) OutlinedTextField(value = filter, onValueChange = { filter = it }, placeholder = { Text("Filter options", color = t.muted) }, singleLine = true, modifier = Modifier.fillMaxWidth(), colors = us.fatykhov.nous.companion.ui.fieldColors())
        if (chips) androidx.compose.foundation.layout.FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            for (o in visible) {
                val on = o.value in selected
                Text(o.label, color = if (on) t.onAccent else t.text, fontSize = 14.sp,
                    modifier = Modifier.background(if (on) t.accent else t.surfaceHover, RoundedCornerShape(999.dp)).border(1.dp, if (on) t.accent else t.border, RoundedCornerShape(999.dp))
                        .clickable { choose(o.value, !on) }.padding(horizontal = 12.dp, vertical = 8.dp).semantics { contentDescription = "option:${o.value}" })
            }
        } else for (o in visible) {
            val on = o.value in selected
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth().clickable { choose(o.value, !on) }.semantics { contentDescription = "option:${o.value}" }) {
                if (multiple) Checkbox(checked = on, onCheckedChange = { choose(o.value, it) }, colors = CheckboxDefaults.colors(checkedColor = t.accent))
                else RadioButton(selected = on, onClick = { choose(o.value, true) }, colors = RadioButtonDefaults.colors(selectedColor = t.accent))
                Text(o.label, color = t.text, fontSize = 15.sp)
            }
        }
        if (visible.isEmpty()) Text("No matching options.", color = t.muted, fontSize = 13.sp)
        Failures(node)
    }
}

// ----------------------------------------------------------------- Slider

@Composable
fun SliderView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val spec = Inputs.sliderSpec(node.props)
    val value = Inputs.sliderValue(Inputs.read(host.store, host.functions, host.surfaceId, node.props, node.scope), spec)
    Column(verticalArrangement = Arrangement.spacedBy(2.dp), modifier = Modifier.fillMaxWidth()) {
        Row(modifier = Modifier.fillMaxWidth()) {
            FieldLabel(host.text(node.props["label"], node.scope)); Box(Modifier.weight(1f))
            Text(Functions.jsNumber(value), color = t.text, fontFamily = t.mono, fontSize = 13.sp)
        }
        Slider(
            value = value.toFloat(), valueRange = spec.min.toFloat()..spec.max.toFloat(),
            onValueChange = { v -> Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonPrimitive(Inputs.sliderSnap(v.toDouble(), spec))) },
            colors = SliderDefaults.colors(thumbColor = t.accent, activeTrackColor = t.accent, inactiveTrackColor = t.border),
            modifier = Modifier.semantics { contentDescription = "slider:${node.componentId}" },
        )
        Failures(node)
    }
}

// ---------------------------------------------------------- DateTimeInput

@Composable
fun DateTimeInputView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current; val ctx = LocalContext.current
    val kind = Inputs.dateKind(node.props)
    val raw = Inputs.textValue(Inputs.read(host.store, host.functions, host.surfaceId, node.props, node.scope))
    val shown = Inputs.normalizeForControl(raw, kind)
    fun write(v: String) = Inputs.write(host.store, host.surfaceId, node.props, node.scope, JsonPrimitive(v))
    fun pickDate(then: (String) -> Unit) {
        val c = Calendar.getInstance()
        DatePickerDialog(ctx, { _, y, m, d -> then("%04d-%02d-%02d".format(y, m + 1, d)) }, c.get(Calendar.YEAR), c.get(Calendar.MONTH), c.get(Calendar.DAY_OF_MONTH)).show()
    }
    fun pickTime(then: (String) -> Unit) {
        val c = Calendar.getInstance()
        TimePickerDialog(ctx, { _, h, m -> then("%02d:%02d".format(h, m)) }, c.get(Calendar.HOUR_OF_DAY), c.get(Calendar.MINUTE), true).show()
    }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.fillMaxWidth()) {
        FieldLabel(host.text(node.props["label"], node.scope))
        Text(shown.ifEmpty { when (kind) { Inputs.DateKind.DATE -> "Pick a date"; Inputs.DateKind.TIME -> "Pick a time"; Inputs.DateKind.DATETIME -> "Pick date & time" } },
            color = if (shown.isEmpty()) t.muted else t.text, fontSize = 15.sp, fontFamily = t.mono,
            modifier = Modifier.fillMaxWidth().background(t.surface, RoundedCornerShape(8.dp)).border(1.dp, t.border, RoundedCornerShape(8.dp))
                .clickable {
                    when (kind) {
                        Inputs.DateKind.DATE -> pickDate { write(it) }
                        Inputs.DateKind.TIME -> pickTime { write(it) }
                        Inputs.DateKind.DATETIME -> pickDate { d -> pickTime { tm -> write("${d}T$tm") } }
                    }
                }.padding(12.dp).semantics { contentDescription = "datetime:${node.componentId}" })
        Failures(node)
    }
}

// ------------------------------------------------------------------ Modal

@Composable
fun ModalView(node: Node.Render) {
    val t = LocalNousTheme.current
    var open by remember { mutableStateOf(false) }   // open state is renderer-local: Modal has no bound value
    Box(modifier = Modifier.clickable { open = true }.semantics { contentDescription = "modal-trigger:${node.componentId}" }) {
        node.props.str("trigger")?.let { Render(it, node.scope, node.depth, node.ancestors) }
    }
    if (open) Dialog(onDismissRequest = { open = false }) {
        Column(Modifier.background(t.surface, RoundedCornerShape(12.dp)).border(1.dp, t.border, RoundedCornerShape(12.dp)).padding(16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) { TextButton(onClick = { open = false }) { Text("✕", color = t.muted) } }
            node.props.str("content")?.let { Render(it, node.scope, node.depth, node.ancestors) }
        }
    }
}

// ------------------------------------------------------------------- Tabs

@Composable
fun TabsView(node: Node.Render) {
    val host = LocalSurfaceHost.current; val t = LocalNousTheme.current
    val tabs = Inputs.tabs(node.props, host.functions, host.ctx(node.scope))
    if (tabs.isEmpty()) return
    var requested by remember { mutableIntStateOf(0) }
    val active = Inputs.activeTab(requested, tabs.size)
    Column(modifier = Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(modifier = Modifier.fillMaxWidth().border(1.dp, t.border, RoundedCornerShape(8.dp)).padding(2.dp), horizontalArrangement = Arrangement.spacedBy(2.dp)) {
            tabs.forEachIndexed { i, tab ->
                val on = i == active
                Text(tab.title, color = if (on) t.onAccent else t.text, fontSize = 14.sp, fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.weight(1f).background(if (on) t.accent else androidx.compose.ui.graphics.Color.Transparent, RoundedCornerShape(6.dp))
                        .clickable { requested = i }.padding(vertical = 8.dp, horizontal = 10.dp).semantics { contentDescription = "tab:$i" })
            }
        }
        // Only the ACTIVE panel is composed: an inactive subtree would run its bindings invisibly.
        tabs[active].child?.let { Render(it, node.scope, node.depth, node.ancestors) }
    }
}
