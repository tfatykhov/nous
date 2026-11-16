package us.fatykhov.nous.companion

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import us.fatykhov.nous.companion.core.Origin
import us.fatykhov.nous.companion.core.Paired
import us.fatykhov.nous.companion.data.DeviceCredential
import us.fatykhov.nous.companion.data.Endpoints
import java.io.File

/**
 * The origin stored at pairing and the one each request is checked against
 * come from one parser (OkHttp's), so they agree on every address OkHttp
 * accepts, and an address it cannot parse cannot be paired.
 */
class EndpointsTest {
    @get:Rule val tmp = TemporaryFolder()

    /** What the interceptor computes for a request the transport builds from [address] (base + path). */
    private fun requestOrigin(address: String): String {
        val url = (address.trim().trimEnd('/') + "/a2ui/surfaces").toHttpUrl()
        return Origin.of(url.scheme, url.host, url.port)
    }

    private val addresses = listOf(
        "https://[2001:db8:0:0:0:0:0:1]",   // written out in full; OkHttp compresses it
        "https://[2001:db8::1]:8443",
        "https://my_host.example",          // an underscore
        "https://nöus.example",         // a non-ASCII host; OkHttp writes it in punycode
        "https://127.1",
        "https://nous.example",
        "HTTPS://Nous.Example:443/",
        "https://user:pw@nous.example",
        "http://10.0.2.2:8000",
    )

    @Test fun the_stored_origin_and_the_request_origin_agree() {
        for (address in addresses) {
            val endpoint = Endpoints.of(address)
            assertNotNull(address, endpoint)
            assertEquals(address, endpoint!!.origin, requestOrigin(address))
        }
    }

    @Test fun a_phone_paired_at_any_of_these_addresses_sends_its_token_there() {
        for (address in addresses) {
            val cred = DeviceCredential(File(tmp.root, "device.properties"), FakeBox())
            cred.clear()
            assertTrue(address, cred.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), address))
            val url = (address.trimEnd('/') + "/a2ui/surfaces").toHttpUrl()
            assertEquals(address, SAMPLE_TOKEN, cred.tokenFor(url.scheme, url.host, url.port))
            assertNotNull(address, cred.pairedFor(address))
        }
    }

    @Test fun ipv6_is_compared_in_the_form_okhttp_writes() {
        assertEquals("2001:db8::1", Endpoints.of("https://[2001:db8:0:0:0:0:0:1]")!!.host)
        assertEquals(Endpoints.of("https://[2001:db8::1]")!!.origin, Endpoints.of("https://[2001:db8:0:0:0:0:0:1]:443")!!.origin)
    }

    @Test fun the_default_port_case_and_userinfo() {
        assertEquals("https://nous.example:443", Endpoints.of("https://nous.example")!!.origin)
        assertEquals(Endpoints.of("https://nous.example")!!.origin, Endpoints.of("HTTPS://Nous.Example:443/companion?x=1#y")!!.origin)
        assertEquals(Endpoints.of("https://nous.example")!!.origin, Endpoints.of("https://user:pw@nous.example")!!.origin)
        assertEquals("http://nous.example:80", Endpoints.of("http://nous.example")!!.origin)
    }

    @Test fun an_address_okhttp_cannot_parse_has_no_endpoint_and_cannot_be_paired() {
        for (bad in listOf("", "nous.example", "ftp://nous.example", "https://", "http://bad host", "https://[2001:db8::1")) {
            assertNull(bad, Endpoints.of(bad))
            val cred = DeviceCredential(File(tmp.root, "bad.properties"), FakeBox())
            assertTrue(bad, !cred.save(Paired(SAMPLE_TOKEN, "d-1", "Pixel", null), bad))
            assertNull(bad, cred.pairedFor(bad))
        }
    }
}
