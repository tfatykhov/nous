package us.fatykhov.nous.companion.data

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import kotlinx.coroutines.launch
import us.fatykhov.nous.companion.core.Pairing
import us.fatykhov.nous.companion.core.PairingWords
import us.fatykhov.nous.companion.core.RedeemOutcome

/**
 * Pair and Unpair, run on the app's scope rather than a screen's. Leaving
 * Settings or turning the phone while the server answers would otherwise
 * cancel the work halfway: a code the server already redeemed would leave a
 * live device whose token the phone never stored, and an unpair stopped
 * after the push row was deleted would leave the phone paired without it.
 * Settings shows [state], also when it comes back after the work finished.
 */
class PairingActions(private val graph: AppGraph) {
    /** What Settings shows: whether work is running, and the last result's sentence. */
    class State(val busy: Boolean = false, val note: String = "", val isError: Boolean = false)

    var state by mutableStateOf(State())
        private set

    /** Redeem [code] at the configured address and keep the token; first make sure a token can be kept. Ignored while other work runs. */
    fun pair(code: String) = launchWork {
        if (!graph.credential.ready()) return@launchWork State(note = PairingWords.NO_KEY, isError = true)
        val base = graph.settings.baseUrl
        when (val out = Pairing.redeem(graph.http, Endpoints.of(base), code, graph.settings.installationId, graph.settings.deviceName, graph.debuggable)) {
            is RedeemOutcome.Done -> if (graph.credential.save(out.paired, base)) {
                graph.engine.connect()
                graph.push.registerIfPossible()
                State(note = PairingWords.paired(out.paired.name))
            } else State(note = PairingWords.NOT_STORED, isError = true)
            is RedeemOutcome.Refused -> State(note = PairingWords.of(out.error), isError = true)
        }
    }

    /** Delete the push row, revoke the device, forget the token (see [Pairing.unpair]). Ignored while other work runs. */
    fun unpair() = launchWork {
        val report = Pairing.unpair(graph.http, graph.settings.installationId, paired = true, forget = { graph.credential.clear() })
        graph.engine.connect()
        if (report.revoked) State(note = PairingWords.UNPAIRED) else State(note = PairingWords.UNPAIRED_UNCONFIRMED, isError = true)
    }

    private fun launchWork(work: suspend () -> State) {
        if (state.busy) return
        state = State(busy = true)
        graph.mainScope.launch {
            try { state = work() } finally { if (state.busy) state = State() }
        }
    }
}
