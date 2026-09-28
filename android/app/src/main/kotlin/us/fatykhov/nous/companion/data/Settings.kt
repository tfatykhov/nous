package us.fatykhov.nous.companion.data

import android.content.Context
import java.io.File
import java.util.Properties
import java.util.UUID

/**
 * App state that must survive process death but NEVER a backup or device
 * transfer (spec §7.4): a small properties file in `noBackupFilesDir`,
 * read synchronously (it is needed in Application.onCreate).
 */
class Settings(context: Context) {
    private val file = File(context.noBackupFilesDir, "companion.properties")
    private val props = Properties().apply { if (file.isFile) file.inputStream().use { load(it) } }

    var baseUrl: String
        get() = props.getProperty("baseUrl", "")
        set(v) { props.setProperty("baseUrl", v.trimEnd('/')); save() }
    var deviceName: String
        get() = props.getProperty("deviceName", android.os.Build.MODEL ?: "Android")
        set(v) { props.setProperty("deviceName", v); save() }
    val installationId: String
        get() = props.getProperty("installationId") ?: UUID.randomUUID().toString().also { props.setProperty("installationId", it); save() }

    /** Cached Firebase options, validated before storage (spec §6.2). */
    var firebaseOptions: Map<String, String>?
        get() {
            val keys = listOf("project_id", "application_id", "api_key", "sender_id")
            val m = keys.associateWith { props.getProperty("fb.$it") ?: return null }
            return if (validFirebaseOptions(m)) m else null
        }
        set(v) { listOf("project_id", "application_id", "api_key", "sender_id").forEach { k -> if (v == null) props.remove("fb.$k") else props.setProperty("fb.$k", v[k]) }; save() }

    val configured: Boolean get() = baseUrl.isNotEmpty()

    fun clear() { props.clear(); save() }

    @Synchronized private fun save() { file.outputStream().use { props.store(it, null) } }

    companion object {
        /** The same checks the server runs (spec §6.2): malformed options crash FCM's sync thread. */
        fun validFirebaseOptions(m: Map<String, String>): Boolean =
            m["project_id"]?.isNotEmpty() == true &&
                m["application_id"]?.contains(':') == true &&
                Regex("^A[\\w-]{38}$").matches(m["api_key"] ?: "") &&
                m["sender_id"]?.all { it.isDigit() } == true && m["sender_id"]!!.isNotEmpty()
    }
}
