package us.fatykhov.nous.companion.data

import us.fatykhov.nous.companion.core.DeviceToken
import us.fatykhov.nous.companion.core.Origin
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.core.SecretBox
import java.io.File
import java.util.Base64
import java.util.Properties
import java.util.concurrent.CopyOnWriteArrayList

/**
 * This phone's pairing: the device token sealed by [box], the origin it was
 * paired with, the device's name and the server's public base URL. One small
 * file of its own in `noBackupFilesDir` (never in a backup or a device
 * transfer), apart from `Settings`' file, which rewrites itself whole on
 * every change. The token is never written in plain text; it is held in
 * memory after the first read, because every request asks for it.
 *
 * The token belongs to one origin (scheme, host, port, as OkHttp parses the
 * address: [Endpoints]): [tokenFor] and [pairedFor] answer only for that
 * origin, so a changed server address is not paired until it is paired itself.
 *
 * Anything that cannot be read or opened (no file, a damaged file, a
 * Keystore failure) reads as not paired.
 */
class DeviceCredential(private val file: File, private val box: SecretBox) {
    /** What the screens show about a pairing: never the token. */
    class PairedDevice(val name: String, val publicBaseUrl: String?)

    private class Held(val token: String, val origin: String, val name: String, val publicBaseUrl: String?) {
        override fun toString(): String = "Held(origin=$origin, name=$name)"
    }

    private var loaded = false
    private var held: Held? = null
    private val listeners = CopyOnWriteArrayList<() -> Unit>()

    fun onChange(l: () -> Unit) { listeners += l }
    fun removeListener(l: () -> Unit) { listeners -= l }
    private fun changed() { listeners.forEach { it() } }

    @Synchronized private fun current(): Held? {
        if (!loaded) { held = read(); loaded = true }
        return held
    }

    /** The pairing for [baseUrl]'s origin, or null when this phone is not paired there. */
    fun pairedFor(baseUrl: String): PairedDevice? {
        val origin = Endpoints.of(baseUrl)?.origin ?: return null
        val h = current() ?: return null
        return if (h.origin == origin) PairedDevice(h.name, h.publicBaseUrl) else null
    }

    /** The token for a request to this scheme, host and port, or null. Where a token may travel is the caller's check. */
    fun tokenFor(scheme: String, host: String, port: Int): String? {
        val h = current() ?: return null
        return h.token.takeIf { h.origin == Origin.of(scheme, host, port) }
    }

    /**
     * Whether a token can be sealed now: a trial seal and, when it fails, one
     * new key and a second trial. Called before a pairing sends anything, so a
     * key that stopped working never costs a redeemed code.
     */
    fun ready(): Boolean {
        if (box.seal(TRIAL) != null) return true
        box.reset()
        return box.seal(TRIAL) != null
    }

    /** Seal and store [paired] for [baseUrl]'s origin. False, and nothing stored, when it cannot be sealed or written. */
    fun save(paired: Paired, baseUrl: String): Boolean {
        val origin = Endpoints.of(baseUrl)?.origin ?: return false
        val sealed = box.seal(paired.token.toByteArray(Charsets.UTF_8)) ?: return false
        val p = Properties()
        p.setProperty(ORIGIN, origin)
        p.setProperty(NAME, paired.name)
        paired.publicBaseUrl?.let { p.setProperty(PUBLIC_BASE_URL, it) }
        p.setProperty(CIPHERTEXT, Base64.getEncoder().encodeToString(sealed.ciphertext))
        p.setProperty(IV, Base64.getEncoder().encodeToString(sealed.iv))
        synchronized(this) {
            try {
                val tmp = File(file.parentFile, file.name + ".tmp")
                tmp.outputStream().use { p.store(it, null) }
                if (!tmp.renameTo(file)) { file.delete(); if (!tmp.renameTo(file)) return false }
            } catch (_: Exception) { return false }
            held = Held(paired.token, origin, paired.name, paired.publicBaseUrl)
            loaded = true
        }
        changed()
        return true
    }

    /** Forget the pairing. With [onlyIf], only while that token is still the one held (a newer pairing is kept). */
    fun clear(onlyIf: String? = null) {
        synchronized(this) {
            if (onlyIf != null && current()?.token != onlyIf) return
            held = null; loaded = true
            file.delete()
        }
        changed()
    }

    private fun read(): Held? = try {
        if (!file.isFile) null else {
            val p = Properties().apply { file.inputStream().use { load(it) } }
            val origin = p.getProperty(ORIGIN)
            val name = p.getProperty(NAME)
            val ct = p.getProperty(CIPHERTEXT)
            val iv = p.getProperty(IV)
            if (origin == null || name == null || ct == null || iv == null) null
            else box.open(SecretBox.Sealed(Base64.getDecoder().decode(ct), Base64.getDecoder().decode(iv)))
                ?.toString(Charsets.UTF_8)
                ?.takeIf { DeviceToken.wellFormed(it) }
                ?.let { Held(it, origin, name, p.getProperty(PUBLIC_BASE_URL)) }
        }
    } catch (_: Exception) { null }

    private companion object {
        val TRIAL = "trial".toByteArray()
        const val ORIGIN = "origin"
        const val NAME = "name"
        const val PUBLIC_BASE_URL = "publicBaseUrl"
        const val CIPHERTEXT = "token.ct"
        const val IV = "token.iv"
    }
}
