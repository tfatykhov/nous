package us.fatykhov.nous.companion.core

import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** An `Http` that answers every call from [answer] and logs each call in order. */
class ScriptedHttp(private val answer: suspend (method: String, path: String) -> Http.Response) : Http {
    val log = mutableListOf<String>()
    val bodies = mutableListOf<String>()
    override suspend fun get(path: String) = record("GET", path)
    override suspend fun postJson(path: String, body: String): Http.Response { bodies.add(body); return record("POST", path) }
    override suspend fun putJson(path: String, body: String): Http.Response { bodies.add(body); return record("PUT", path) }
    override suspend fun delete(path: String) = record("DELETE", path)
    override suspend fun stream(path: String, onOpen: () -> Unit, onChunk: (String) -> Unit) { log.add("STREAM $path") }
    private suspend fun record(method: String, path: String): Http.Response { log.add("$method $path"); return answer(method, path) }
}

private fun res(status: Int, body: String, vararg headers: Pair<String, String>) = Http.Response(status, body, headers.toMap())

const val SAMPLE_TOKEN = "nd1_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE"

class PairingTest {
    private val base = "https://nous.example"

    @Test fun a_code_is_normalised() {
        assertEquals("K7QM2XRD", PairingCode.normalise("K7QM-2XRD"))
        assertEquals("K7QM2XRD", PairingCode.normalise("k7qm2xrd"))
        assertEquals("K7QM2XRD", PairingCode.normalise("  k7qm 2xrd "))
        assertEquals("K7QM2XRD", PairingCode.normalise("k7-qm-2x-rd"))
        assertNull(PairingCode.normalise("K7QM-2XR"))
        assertNull(PairingCode.normalise("K7QM-2XRDE"))
        assertNull(PairingCode.normalise("K7QM-2XR!"))
        assertNull(PairingCode.normalise(""))
    }

    @Test fun the_token_shape_is_the_one_the_server_issues() {
        assertEquals(47, SAMPLE_TOKEN.length)
        assertTrue(DeviceToken.wellFormed(SAMPLE_TOKEN))
        assertFalse(DeviceToken.wellFormed("nd2_" + SAMPLE_TOKEN.drop(4)))
        assertFalse(DeviceToken.wellFormed(SAMPLE_TOKEN.dropLast(1)))
        assertFalse(DeviceToken.wellFormed("K7QM2XRD"))
    }

    @Test fun redeem_posts_the_code_installation_and_name_and_returns_the_token_and_the_public_base_url() = runTest {
        val http = ScriptedHttp { _, _ -> res(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN","public_base_url":"https://web.nous.example/"}""") }

        val out = Pairing.redeem(http, base, "k7qm-2xrd", "inst-1", "Pixel 9", debuggable = false)

        assertEquals(listOf("POST /a2ui/devices/redeem"), http.log)
        val sent = Json.parseToJsonElement(http.bodies.single()).jsonObject
        assertEquals("K7QM2XRD", sent["code"]!!.jsonPrimitive.content)
        assertEquals("inst-1", sent["installation_id"]!!.jsonPrimitive.content)
        assertEquals("Pixel 9", sent["name"]!!.jsonPrimitive.content)
        val paired = assertIs<RedeemOutcome.Done>(out).paired
        assertEquals(SAMPLE_TOKEN, paired.token)
        assertEquals("d-1", paired.deviceId)
        assertEquals("Pixel", paired.name)
        assertEquals("https://web.nous.example", paired.publicBaseUrl)
    }

    @Test fun a_null_public_base_url_is_none() = runTest {
        val http = ScriptedHttp { _, _ -> res(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN","public_base_url":null}""") }
        val paired = assertIs<RedeemOutcome.Done>(Pairing.redeem(http, base, "K7QM2XRD", "i", "n", false)).paired
        assertNull(paired.publicBaseUrl)
    }

    private suspend fun errorFor(response: Http.Response): PairingError =
        assertIs<RedeemOutcome.Refused>(Pairing.redeem(ScriptedHttp { _, _ -> response }, base, "K7QM2XRD", "i", "n", false)).error

    @Test fun each_status_maps_to_its_sentence() = runTest {
        assertEquals(PairingError.NotValid, errorFor(res(404, """{"error":"that code is not valid"}""")))
        assertEquals(PairingError.TooManyTries(37), errorFor(res(429, """{"error":"too many tries: wait a minute"}""", "Retry-After" to "37")))
        assertEquals(PairingError.TooManyTries(null), errorFor(res(429, "")))
        assertEquals(PairingError.Unsupported, errorFor(res(405, "Method Not Allowed")))
        assertEquals(PairingError.Failed(500), errorFor(res(500, """{"error":"the request could not be processed"}""")))
        assertEquals(PairingError.Failed(415), errorFor(res(415, """{"error":"Content-Type must be application/json"}""")))
        assertEquals(PairingError.Failed(200), errorFor(res(200, """{"device_id":"d","token":"not-a-token"}""")))
        val unreachable = Pairing.redeem(ScriptedHttp { _, _ -> throw java.io.IOException("connection refused") }, base, "K7QM2XRD", "i", "n", false)
        assertEquals(PairingError.Unreachable, assertIs<RedeemOutcome.Refused>(unreachable).error)

        assertEquals("That code is not valid or has run out. Show a new one in the dashboard.", PairingWords.of(PairingError.NotValid))
        assertEquals("Too many tries. Wait 37 seconds, then try again.", PairingWords.of(PairingError.TooManyTries(37)))
        assertEquals("Too many tries. Wait a minute, then try again.", PairingWords.of(PairingError.TooManyTries(null)))
        assertEquals("This server does not support pairing yet.", PairingWords.of(PairingError.Unsupported))
        assertEquals("Couldn't reach Nous. Check Tailscale and try again.", PairingWords.of(PairingError.Unreachable))
        assertTrue(PairingWords.of(PairingError.NoHttps).contains("https address"))
        assertTrue(PairingWords.of(PairingError.Failed(500)).contains("HTTP 500"))
    }

    @Test fun a_404_with_no_json_body_means_the_server_has_no_pairing() = runTest {
        assertEquals(PairingError.Unsupported, errorFor(res(404, "Not Found")))
        assertEquals(PairingError.Unsupported, errorFor(res(404, "<html><body>404</body></html>")))
        assertEquals(PairingError.Unsupported, errorFor(res(404, """{"detail":"Not Found"}""")))
    }

    @Test fun redeem_refuses_a_non_https_address_before_sending() = runTest {
        for (address in listOf("http://nous.example", "http://nous.example:8000", "http://192.0.2.10:8000")) {
            val http = ScriptedHttp { _, _ -> error("nothing may be sent") }
            assertEquals(PairingError.NoHttps, assertIs<RedeemOutcome.Refused>(Pairing.redeem(http, address, "K7QM2XRD", "i", "n", debuggable = true)).error, address)
            assertTrue(http.log.isEmpty())
        }
        val release = ScriptedHttp { _, _ -> error("nothing may be sent") }
        assertEquals(PairingError.NoHttps, assertIs<RedeemOutcome.Refused>(Pairing.redeem(release, "http://10.0.2.2:8000", "K7QM2XRD", "i", "n", debuggable = false)).error)
        val emulator = ScriptedHttp { _, _ -> res(404, """{"error":"that code is not valid"}""") }
        Pairing.redeem(emulator, "http://10.0.2.2:8000", "K7QM2XRD", "i", "n", debuggable = true)
        assertEquals(listOf("POST /a2ui/devices/redeem"), emulator.log)
    }

    @Test fun a_malformed_code_is_refused_before_sending() = runTest {
        val http = ScriptedHttp { _, _ -> error("nothing may be sent") }
        assertEquals(PairingError.Malformed, assertIs<RedeemOutcome.Refused>(Pairing.redeem(http, base, "K7QM", "i", "n", false)).error)
        assertTrue(http.log.isEmpty())
    }

    @Test fun no_sentence_or_error_string_contains_the_token_or_the_code() = runTest {
        val code = "B4XW9PQT"
        val errors = listOf(PairingError.Malformed, PairingError.NotValid, PairingError.TooManyTries(5), PairingError.TooManyTries(null),
            PairingError.Unsupported, PairingError.NoHttps, PairingError.Unreachable, PairingError.Failed(500))
        val sentences = errors.map { PairingWords.of(it) } + errors.map { it.toString() } + listOf(
            PairingWords.TAP_NOT_PAIRED, PairingWords.TOKEN_REJECTED, PairingWords.NOT_CHECKED, PairingWords.INBOX_NOT_PAIRED,
            PairingWords.UNPAIRED, PairingWords.UNPAIRED_UNCONFIRMED, PairingWords.NOT_STORED, PairingWords.paired("Pixel"),
        )
        val done = Pairing.redeem(ScriptedHttp { _, _ -> res(200, """{"device_id":"d-1","name":"Pixel","token":"$SAMPLE_TOKEN"}""") }, base, code, "i", "n", false)
        val printed = sentences + listOf(done.toString(), assertIs<RedeemOutcome.Done>(done).paired.toString())
        for (s in printed) {
            assertFalse(s.contains(SAMPLE_TOKEN), s)
            assertFalse(s.contains("nd1_"), s)
            assertFalse(s.uppercase().replace("-", "").contains(code), s)
        }
    }

    @Test fun unpair_deletes_the_push_row_then_revokes_then_forgets() = runTest {
        val http = ScriptedHttp { method, _ -> if (method == "POST") res(200, """{"revoked":true}""") else res(200, "{}") }
        val report = Pairing.unpair(http, "inst 1", paired = true, forget = { http.log.add("FORGET") })
        assertEquals(listOf("DELETE /a2ui/push/tokens/inst%201", "POST /a2ui/devices/unpair", "FORGET"), http.log)
        assertEquals(UnpairReport(pushDeleted = true, revoked = true), report)
    }

    @Test fun unpair_goes_on_when_a_step_fails_and_always_forgets() = runTest {
        val pushDown = ScriptedHttp { method, _ -> if (method == "DELETE") throw java.io.IOException("down") else res(200, """{"revoked":true}""") }
        assertEquals(UnpairReport(pushDeleted = false, revoked = true), Pairing.unpair(pushDown, "i", true, forget = { pushDown.log.add("FORGET") }))
        assertEquals(listOf("DELETE /a2ui/push/tokens/i", "POST /a2ui/devices/unpair", "FORGET"), pushDown.log)

        val serverDown = ScriptedHttp { _, _ -> throw java.io.IOException("down") }
        assertEquals(UnpairReport(pushDeleted = false, revoked = false), Pairing.unpair(serverDown, "i", true, forget = { serverDown.log.add("FORGET") }))
        assertEquals("FORGET", serverDown.log.last())

        val refused = ScriptedHttp { method, _ -> if (method == "POST") res(500, "{}") else res(200, "{}") }
        assertEquals(UnpairReport(pushDeleted = true, revoked = false), Pairing.unpair(refused, "i", true, forget = { refused.log.add("FORGET") }))
        assertEquals("FORGET", refused.log.last())
    }

    @Test fun unpair_counts_a_token_the_server_already_rejects_as_revoked() = runTest {
        val http = ScriptedHttp { method, _ ->
            if (method == "POST") res(403, """{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"x"}}""") else res(200, "{}")
        }
        assertTrue(Pairing.unpair(http, "i", true, forget = {}).revoked)
    }

    @Test fun unpair_bounds_each_step_and_still_forgets() = runTest {
        val http = ScriptedHttp { _, _ -> awaitCancellation() }
        var forgotten = false
        val report = Pairing.unpair(http, "i", true, forget = { forgotten = true })
        assertEquals(listOf("DELETE /a2ui/push/tokens/i", "POST /a2ui/devices/unpair"), http.log)
        assertTrue(forgotten)
        assertEquals(UnpairReport(pushDeleted = false, revoked = false), report)
    }

    @Test fun an_unpaired_phone_deletes_its_push_row_and_calls_no_unpair() = runTest {
        val http = ScriptedHttp { _, _ -> res(200, "{}") }
        Pairing.unpair(http, "i", paired = false, forget = { http.log.add("FORGET") })
        assertEquals(listOf("DELETE /a2ui/push/tokens/i", "FORGET"), http.log)
    }
}

class RefusalTest {
    @Test fun the_two_codes_are_read_from_the_error_object() {
        assertEquals(Refusal.NOT_PAIRED, Refusal.of(403, """{"error":{"code":"OWNER_PROOF_REQUIRED","surfaceId":"s","message":"m"}}"""))
        assertEquals(Refusal.TOKEN_REJECTED, Refusal.of(403, """{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"m"}}"""))
    }

    @Test fun the_call_envelope_carries_the_code_under_agentFunctionResponse() {
        assertEquals(Refusal.NOT_PAIRED, Refusal.of(403, """{"version":"v1.0","agentFunctionResponse":{"functionCallId":"f","error":{"code":"OWNER_PROOF_REQUIRED","message":"m"}}}"""))
        assertEquals(Refusal.TOKEN_REJECTED, Refusal.of(403, """{"agentFunctionResponse":{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"m"}}}"""))
    }

    @Test fun any_other_403_is_not_a_code() {
        assertNull(Refusal.of(403, """{"error":"forbidden"}"""))
        assertNull(Refusal.of(403, """{"error":{"code":"NONCE_MISMATCH","message":"stale"}}"""))
        assertNull(Refusal.of(403, "Forbidden"))
        assertNull(Refusal.of(403, ""))
        assertNull(Refusal.of(404, """{"error":{"code":"OWNER_PROOF_REQUIRED"}}"""))
        assertNull(Refusal.of(200, """{"error":{"code":"DEVICE_TOKEN_REJECTED"}}"""))
    }
}

class TokenPolicyTest {
    @Test fun https_may_carry_the_token_anywhere() {
        assertTrue(TokenPolicy.mayCarry("https://nous.example", debuggable = false))
        assertTrue(TokenPolicy.mayCarry("https://nous.example:8443/x", debuggable = false))
        assertTrue(TokenPolicy.mayCarry("HTTPS://NOUS.EXAMPLE", debuggable = true))
    }

    @Test fun plain_http_only_to_the_emulator_hosts_on_a_debuggable_build() {
        assertTrue(TokenPolicy.mayCarry("http://10.0.2.2:8000", debuggable = true))
        assertTrue(TokenPolicy.mayCarry("http://localhost:8000", debuggable = true))
        assertFalse(TokenPolicy.mayCarry("http://10.0.2.2:8000", debuggable = false))
        assertFalse(TokenPolicy.mayCarry("http://localhost:8000", debuggable = false))
        for (url in listOf("http://nous.example", "http://127.0.0.1:8000", "http://192.0.2.10:8000", "http://10.0.2.3"))
            assertFalse(TokenPolicy.mayCarry(url, debuggable = true), url)
    }

    @Test fun nothing_else() {
        for (url in listOf("ftp://nous.example", "nous.example", "", "https://", "ws://nous.example"))
            assertFalse(TokenPolicy.mayCarry(url, debuggable = true), url)
    }
}

class OriginTest {
    @Test fun the_default_port_is_filled_in() {
        assertEquals("https://nous.example:443", Origin.of("https://nous.example"))
        assertEquals(Origin.of("https://nous.example:443/companion"), Origin.of("https://nous.example"))
        assertEquals(Origin.of("http://nous.example:80"), Origin.of("http://nous.example"))
    }

    @Test fun case_userinfo_path_query_and_fragment_do_not_matter() {
        assertEquals(Origin.of("https://nous.example"), Origin.of("HTTPS://Nous.Example/a2ui?x=1#y"))
        assertEquals(Origin.of("https://nous.example"), Origin.of("https://user:pw@nous.example"))
    }

    @Test fun scheme_host_and_port_each_make_another_origin() {
        val paired = Origin.of("https://nous.example")
        for (other in listOf("http://nous.example", "https://nous.example:8443", "https://other.example", "https://nous.example.other.example"))
            assertFalse(paired == Origin.of(other), other)
    }

    @Test fun the_parts_form_matches_the_url_form() {
        assertEquals(Origin.of("https://nous.example"), Origin.of("https", "NOUS.example", 443))
        assertEquals(Origin.of("http://[fd00::1]:8000"), Origin.of("http", "fd00::1", 8000))
    }

    @Test fun an_address_that_is_not_an_http_url_has_no_origin() {
        for (bad in listOf("", "nous.example", "ftp://nous.example", "https://", "http://bad host"))
            assertNull(Origin.of(bad), bad)
    }
}
