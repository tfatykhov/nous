package us.fatykhov.nous.companion

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import us.fatykhov.nous.companion.core.Http
import us.fatykhov.nous.companion.data.Settings
import us.fatykhov.nous.companion.push.PushManager

/** Push options that arrive after the phone was disconnected, or pointed at another server, are not kept. */
@RunWith(RobolectricTestRunner::class)
@Config(application = TestApp::class, sdk = [34])
class PushConfigTest {
    private val ctx: Context = ApplicationProvider.getApplicationContext()
    private val validConfig = """{"enabled":"true","project_id":"p","application_id":"1:2:android:3","api_key":"A${"b".repeat(38)}","sender_id":"123"}"""

    /** An `Http` whose push-config answer waits until the test releases it. */
    private class HeldConfig : Http {
        val asked = CompletableDeferred<Unit>()
        val answer = CompletableDeferred<Http.Response>()
        override suspend fun get(path: String): Http.Response { asked.complete(Unit); return answer.await() }
        override suspend fun postJson(path: String, body: String) = error("not used")
        override suspend fun putJson(path: String, body: String) = error("not used")
        override suspend fun delete(path: String) = error("not used")
        override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) = error("not used")
    }

    private fun answeredAfter(change: (Settings) -> Unit): Settings = runBlocking {
        val settings = Settings(ctx).apply { clear(); baseUrl = "https://nous.example" }
        val http = HeldConfig()
        val push = PushManager(ctx, settings, http)
        val refresh = async(Dispatchers.IO) { push.refreshConfig() }
        http.asked.await()
        change(settings)
        http.answer.complete(Http.Response(200, validConfig, emptyMap()))
        refresh.await()
        settings
    }

    @Test fun options_answered_after_a_disconnect_are_not_kept() {
        assertNull(answeredAfter { it.clear() }.firebaseOptions)
    }

    @Test fun options_answered_after_the_address_changed_are_not_kept() {
        assertNull(answeredAfter { it.baseUrl = "https://other.example" }.firebaseOptions)
    }
}
