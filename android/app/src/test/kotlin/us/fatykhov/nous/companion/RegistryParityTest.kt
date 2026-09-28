package us.fatykhov.nous.companion

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Test
import org.junit.Assert.assertEquals
import java.io.File
import us.fatykhov.nous.companion.core.Registry as CoreRegistry
import us.fatykhov.nous.companion.ui.catalog.Registry as UiRegistry

/**
 * The three registries must agree (spec §8.3): the Compose renderer map,
 * `:core.Registry` (the JVM sweep's view) and the coverage manifest.
 * Plain JUnit — no Robolectric needed to compare sets.
 */
class RegistryParityTest {
    @Test fun uiRegistryEqualsCoreRegistry() {
        assertEquals(CoreRegistry.ported, UiRegistry.names)
    }

    @Test fun uiRegistryEqualsManifestPortedSet() {
        val root = File(System.getProperty("nous.repoRoot") ?: "../..").canonicalFile
        val m = Json.parseToJsonElement(File(root, "android/catalog-coverage.json").readText()).jsonObject
        val ported = listOf("basic", "nous_core").flatMap { sec ->
            m[sec]!!.jsonObject.entries.filter { it.value.jsonObject["status"]!!.jsonPrimitive.content == "ported" }.map { it.key }
        }.toSet()
        assertEquals(ported, UiRegistry.names)
    }
}
