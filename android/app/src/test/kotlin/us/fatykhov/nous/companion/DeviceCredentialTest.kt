package us.fatykhov.nous.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.core.SecretBox
import us.fatykhov.nous.companion.data.DeviceCredential
import java.io.File

const val SAMPLE_TOKEN = "nd1_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"

/**
 * A reversible stand-in for the Keystore box. [failSeal] and [failOpen] model a key that refuses;
 * [resetFixes] makes [reset] give a working key; [failSealFrom] fails every seal from that call on.
 */
class FakeBox(
    var failSeal: Boolean = false,
    var failOpen: Boolean = false,
    private val resetFixes: Boolean = false,
    private val failSealFrom: Int = Int.MAX_VALUE,
) : SecretBox {
    var seals = 0; private set
    var resets = 0; private set
    override fun seal(plain: ByteArray): SecretBox.Sealed? {
        seals += 1
        if (failSeal || seals >= failSealFrom) return null
        return SecretBox.Sealed(plain.map { (it.toInt() xor 0x5A).toByte() }.reversed().toByteArray(), ByteArray(12) { it.toByte() })
    }
    override fun open(sealed: SecretBox.Sealed): ByteArray? =
        if (failOpen) null else sealed.ciphertext.reversed().map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
    override fun reset() { resets += 1; if (resetFixes) failSeal = false }
}

class DeviceCredentialTest {
    @get:Rule val tmp = TemporaryFolder()
    private val base = "https://nous.example"
    private fun paired(token: String = SAMPLE_TOKEN, public: String? = "https://web.nous.example") = Paired(token, "d-1", "Pixel", public)
    private fun file() = File(tmp.root, "device.properties")

    @Test fun save_and_read_back_in_a_new_instance() {
        assertTrue(DeviceCredential(file(), FakeBox()).save(paired(), base))
        val again = DeviceCredential(file(), FakeBox())
        val p = again.pairedFor(base)
        assertNotNull(p)
        assertEquals("Pixel", p!!.name)
        assertEquals("https://web.nous.example", p.publicBaseUrl)
        assertEquals(SAMPLE_TOKEN, again.tokenFor("https", "nous.example", 443))
    }

    @Test fun the_file_never_holds_the_token_in_plain_text() {
        DeviceCredential(file(), FakeBox()).save(paired(), base)
        val text = file().readText()
        assertFalse(text.contains(SAMPLE_TOKEN))
        assertFalse(text.contains("nd1_"))
        assertFalse(text.contains(java.util.Base64.getEncoder().encodeToString(SAMPLE_TOKEN.toByteArray())))
    }

    @Test fun clear_forgets_it_here_and_on_disk() {
        val c = DeviceCredential(file(), FakeBox())
        c.save(paired(), base)
        var changes = 0
        c.onChange { changes++ }
        c.clear()
        assertNull(c.pairedFor(base))
        assertNull(c.tokenFor("https", "nous.example", 443))
        assertFalse(file().exists())
        assertNull(DeviceCredential(file(), FakeBox()).pairedFor(base))
        assertEquals(1, changes)
    }

    @Test fun a_failed_open_reads_as_not_paired() {
        DeviceCredential(file(), FakeBox()).save(paired(), base)
        val c = DeviceCredential(file(), FakeBox(failOpen = true))
        assertNull(c.pairedFor(base))
        assertNull(c.tokenFor("https", "nous.example", 443))
    }

    @Test fun a_damaged_file_reads_as_not_paired() {
        file().writeText("origin=https://nous.example:443\nname=Pixel\ntoken.ct=@@not base64@@\ntoken.iv=AAAA\n")
        assertNull(DeviceCredential(file(), FakeBox()).pairedFor(base))
    }

    @Test fun a_failed_seal_stores_nothing() {
        val c = DeviceCredential(file(), FakeBox(failSeal = true))
        assertFalse(c.save(paired(), base))
        assertNull(c.pairedFor(base))
        assertFalse(file().exists())
    }

    @Test fun the_origin_is_stored_with_the_token() {
        val c = DeviceCredential(file(), FakeBox())
        c.save(paired(), "https://Nous.Example/")
        assertNotNull(c.pairedFor("https://nous.example:443/companion"))
        assertEquals(SAMPLE_TOKEN, c.tokenFor("HTTPS", "NOUS.EXAMPLE", 443))
        assertNull(c.tokenFor("http", "nous.example", 80))
        assertNull(c.tokenFor("https", "nous.example", 8443))
        assertNull(c.tokenFor("https", "other.example", 443))
    }

    @Test fun a_changed_address_is_not_paired_until_it_is_paired_itself() {
        val c = DeviceCredential(file(), FakeBox())
        c.save(paired(), base)
        for (other in listOf("https://other.example", "http://nous.example", "https://nous.example:8443"))
            assertNull(other, c.pairedFor(other))
        // The old pairing is kept for its own origin until Disconnect clears it.
        assertNotNull(c.pairedFor(base))
    }

    @Test fun clear_with_a_token_keeps_a_newer_pairing() {
        val c = DeviceCredential(file(), FakeBox())
        c.save(paired(), base)
        val newer = "nd1_" + "Z".repeat(43)
        c.save(paired(token = newer), base)
        c.clear(onlyIf = SAMPLE_TOKEN)
        assertEquals(newer, c.tokenFor("https", "nous.example", 443))
        c.clear(onlyIf = newer)
        assertNull(c.pairedFor(base))
    }

    @Test fun ready_tries_a_seal_and_replaces_a_broken_key_once() {
        val working = FakeBox()
        assertTrue(DeviceCredential(file(), working).ready())
        assertEquals(0, working.resets)

        val fixable = FakeBox(failSeal = true, resetFixes = true)
        assertTrue(DeviceCredential(file(), fixable).ready())
        assertEquals(1, fixable.resets)

        val broken = FakeBox(failSeal = true)
        assertFalse(DeviceCredential(file(), broken).ready())
        assertEquals(1, broken.resets)
    }

    @Test fun a_pairing_without_a_public_base_url_has_none() {
        val c = DeviceCredential(file(), FakeBox())
        c.save(paired(public = null), base)
        assertNull(DeviceCredential(file(), FakeBox()).pairedFor(base)!!.publicBaseUrl)
    }
}
