package us.fatykhov.nous.companion.core

import java.io.File

/** Repo paths for tests that read catalogs and fixtures IN PLACE (spec §7.1: `nous.repoRoot`). */
object RepoFiles {
    val root: File = File(System.getProperty("nous.repoRoot") ?: "../..").canonicalFile
    val catalogs: File get() = File(root, "nous/a2ui/catalogs")
    val examples: File get() = File(root, "tests/fixtures/a2ui/examples")
    val f096: File get() = File(root, "dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json")
    val manifest: File get() = File(root, "android/catalog-coverage.json")
}
