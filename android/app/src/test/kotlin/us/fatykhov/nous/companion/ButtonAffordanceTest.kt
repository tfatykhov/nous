package us.fatykhov.nous.companion

import android.graphics.Bitmap
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.unit.dp
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode
import us.fatykhov.nous.companion.ui.ButtonKind
import us.fatykhov.nous.companion.ui.LocalButtonInk
import us.fatykhov.nous.companion.ui.LocalNousTheme
import us.fatykhov.nous.companion.ui.NousButton
import us.fatykhov.nous.companion.ui.NousThemed
import us.fatykhov.nous.companion.ui.Themes
import java.io.File

/**
 * A button must LOOK like one at rest and ANSWER a press. The first build's
 * did neither: a transparent fill, and a press feedback that was a black
 * overlay on a dark surface. These pin the behaviour; the pixel tests pin the
 * part a semantics tree cannot see.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34], qualifiers = "w411dp-h891dp-xxhdpi")
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class ButtonAffordanceTest {
    @get:Rule val rule = createComposeRule()

    private fun gallery(onTap: () -> Unit = {}) = rule.setContent {
        NousThemed(Themes.nousDefault) {
            val t = LocalNousTheme.current
            Column(Modifier.background(t.bg).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                NousButton(onClick = onTap) { Text("Default", color = LocalButtonInk.current ?: t.text) }
                NousButton(onClick = onTap, kind = ButtonKind.Primary) { Text("Primary", color = LocalButtonInk.current ?: t.text) }
                NousButton(onClick = onTap, kind = ButtonKind.Borderless) { Text("Borderless", color = LocalButtonInk.current ?: t.text) }
                NousButton(onClick = onTap, enabled = false) { Text("Disabled", color = LocalButtonInk.current ?: t.text) }
                NousButton(onClick = onTap, busy = true) { Text("Busy", color = LocalButtonInk.current ?: t.text) }
            }
        }
    }

    @Test fun aTapReachesTheHandler() {
        var taps by mutableIntStateOf(0)
        gallery { taps += 1 }
        rule.onNodeWithText("Default").assertHasClickAction().assertIsEnabled().performClick()
        rule.onNodeWithText("Primary").performClick()
        assertEquals(2, taps)
    }

    @Test fun aDisabledOrBusyButtonTakesNoTap() {
        var taps by mutableIntStateOf(0)
        gallery { taps += 1 }
        rule.onNodeWithText("Disabled").assertIsNotEnabled().performClick()
        rule.onNodeWithText("Busy").assertIsNotEnabled().performClick()
        assertEquals("a second tap while the first is in flight must not fire", 0, taps)
        rule.onNodeWithContentDescription("busy").assertExists()
    }

    /** The resting fill: a default button is not the colour of the page behind it. */
    @Test fun aDefaultButtonIsFilledAtRest() {
        gallery()
        val shot = rule.onNodeWithText("Default").captureToImage().asAndroidBitmap()
        val fill = shot.getPixel(shot.width / 8, shot.height / 2)
        assertNotEquals("a button with the page's own colour reads as a label", argb(Themes.nousDefault.bg), fill)
        save(rule.onRoot().captureToImage().asAndroidBitmap(), "buttons-rest.png")
    }

    /** The press: the pixels under a held finger are not the pixels at rest. */
    @Test fun aPressChangesWhatIsDrawn() {
        gallery()
        val node = rule.onNodeWithText("Primary")
        val rest = node.captureToImage().asAndroidBitmap()
        val before = rest.getPixel(rest.width / 8, rest.height / 2)
        rule.mainClock.autoAdvance = false
        node.performTouchInput { down(center) }
        rule.mainClock.advanceTimeBy(400)
        val held = node.captureToImage().asAndroidBitmap()
        val after = held.getPixel(held.width / 8, held.height / 2)
        save(rule.onRoot().captureToImage().asAndroidBitmap(), "buttons-pressed.png")
        node.performTouchInput { up() }
        rule.mainClock.autoAdvance = true
        assertNotEquals("holding a button must visibly change it", before, after)
    }

    private fun argb(c: androidx.compose.ui.graphics.Color): Int =
        android.graphics.Color.argb((c.alpha * 255).toInt(), (c.red * 255).toInt(), (c.green * 255).toInt(), (c.blue * 255).toInt())

    private fun save(b: Bitmap, name: String) {
        val dir = File("build/outputs/button-shots").apply { mkdirs() }
        File(dir, name).outputStream().use { b.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }
}
