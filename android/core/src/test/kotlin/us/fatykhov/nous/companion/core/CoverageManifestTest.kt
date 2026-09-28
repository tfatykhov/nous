package us.fatykhov.nous.companion.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The JUnit half of the §8.3 ratchet: the Kotlin registry must agree with
 * `android/catalog-coverage.json` exactly. The Python half checks the
 * manifest against the catalogs.
 */
class CoverageManifestTest {
    private val manifest = Json.parseToJsonElement(RepoFiles.manifest.readText()).jsonObject

    private fun ported(section: String) = manifest[section]!!.jsonObject.entries
        .filter { it.value.jsonObject["status"]!!.jsonPrimitive.content == "ported" }.map { it.key }.toSet()

    @Test fun registryEqualsManifestPortedSet() {
        val declared = ported("basic") + ported("nous_core")
        assertEquals(declared, Registry.ported, "registry and manifest disagree on what is ported")
    }

    @Test fun manifestNamesEveryCatalogComponent() {
        for ((section, file) in listOf("basic" to "basic", "nous_core" to "nous_core")) {
            val cat = Json.parseToJsonElement(java.io.File(RepoFiles.catalogs, "$file/catalog.json").readText()).jsonObject
            val comps = cat["components"]!!.jsonObject.keys
            val listed = manifest[section]!!.jsonObject.keys
            assertEquals(comps, listed, "$section: catalog vs manifest")
        }
    }

    @Test fun webUnimplementedStayUnsupported() {
        assertTrue("Video" !in Registry.ported && "AudioPlayer" !in Registry.ported)
    }
}
