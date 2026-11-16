package us.fatykhov.nous.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import us.fatykhov.nous.companion.push.Registration
import us.fatykhov.nous.companion.push.registrationOutcome

/** The push token worker stops on an answer a retry cannot change, and says why in Settings. */
class TokenWorkerOutcomeTest {
    private val notPaired = """{"error":{"code":"OWNER_PROOF_REQUIRED","message":"m"}}"""
    private val rejected = """{"error":{"code":"DEVICE_TOKEN_REJECTED","message":"m"}}"""

    @Test fun a_403_stops_the_worker_and_leaves_a_state_the_app_shows() {
        assertEquals(Registration.STOP to "register: this phone is not paired (HTTP 403)", registrationOutcome(403, notPaired))
        assertEquals(Registration.STOP to "register: this device is no longer paired (HTTP 403)", registrationOutcome(403, rejected))
        assertEquals(Registration.STOP to "register: HTTP 403", registrationOutcome(403, """{"error":"forbidden"}"""))
    }

    @Test fun other_client_errors_and_a_redirect_stop_too() {
        for (status in listOf(400, 401, 405, 409, 415, 302))
            assertEquals("$status", Registration.STOP to "register: HTTP $status", registrationOutcome(status, ""))
    }

    @Test fun a_timeout_a_limit_or_a_server_error_is_retried() {
        for (status in listOf(408, 429, 500, 502, 503))
            assertEquals("$status", Registration.RETRY, registrationOutcome(status, "").first)
    }

    @Test fun success_and_a_server_without_push_are_done() {
        assertEquals(Registration.DONE to null, registrationOutcome(200, "{}"))
        val (outcome, error) = registrationOutcome(404, "Not Found")
        assertEquals(Registration.DONE, outcome)
        assertNull(error)
    }
}
