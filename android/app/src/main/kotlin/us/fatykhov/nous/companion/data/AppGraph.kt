package us.fatykhov.nous.companion.data

import android.content.Context
import android.content.pm.ApplicationInfo
import android.security.NetworkSecurityPolicy
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import us.fatykhov.nous.companion.push.PushManager
import us.fatykhov.nous.companion.core.Functions
import us.fatykhov.nous.companion.core.SecretBox
import us.fatykhov.nous.companion.core.SurfaceStore
import us.fatykhov.nous.companion.core.SyncEngine
import us.fatykhov.nous.companion.core.TokenPolicy
import java.io.File

/**
 * Hand-wired object graph (no DI framework for a beta). One per process. [box] and
 * [cleartextPermitted] (the build's network security config) are replaceable for tests only.
 */
class AppGraph(
    context: Context,
    box: SecretBox = KeystoreSecretBox(),
    private val cleartextPermitted: (host: String) -> Boolean = { NetworkSecurityPolicy.getInstance().isCleartextTrafficPermitted(it) },
) {
    val appContext: Context = context.applicationContext
    val settings = Settings(appContext)
    /** Read at run time: the project generates no BuildConfig. A release build is not debuggable. */
    val debuggable = (appContext.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0
    val credential = DeviceCredential(File(appContext.noBackupFilesDir, "device.properties"), box)
    val store = SurfaceStore()
    val mainScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    val http = OkHttpTransport(settings, credential, debuggable)
    val engine = SyncEngine(store, http, mainScope, tokenSent = ::tokenSent)

    /**
     * Whether the configured address is plain http on a build that will not use clear text
     * (the release build): nothing can reach it, and the screens say to use the https address.
     */
    fun needsHttps(): Boolean {
        val e = Endpoints.of(settings.baseUrl) ?: return false
        return e.scheme == "http" && !cleartextPermitted(e.host)
    }

    /** Whether calls to the configured address carry the device token (paired there, and the policy allows it). */
    fun tokenSent(): Boolean {
        val e = Endpoints.of(settings.baseUrl) ?: return false
        return credential.pairedFor(settings.baseUrl) != null && TokenPolicy.mayCarry(e.scheme, e.host, debuggable)
    }

    /**
     * Where the web links open: the public base URL the pairing returned, when this
     * phone is paired with the configured address and the server gave one; else the
     * configured address, as before pairing existed.
     */
    fun webBaseUrl(): String = credential.pairedFor(settings.baseUrl)?.publicBaseUrl ?: settings.baseUrl
    // The device zone, not the :core default of UTC: the web formatter uses LOCAL
    // date getters, so an offset-bearing timestamp shows the user's wall clock.
    val functions = Functions(fmt = AndroidFormatter, opener = { url -> UrlOpener.open(appContext, url) }, zone = java.time.ZoneId.systemDefault())
    val push = PushManager(appContext, settings, http)
    val lifecycle = ForegroundLifecycle(this)
    /** Pair and Unpair, on [mainScope] so they finish whatever the screen does. */
    val pairing = PairingActions(this)
}
