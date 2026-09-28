// `:core` — the platform-neutral half of the companion (spec §4): protocol
// envelopes, the SSE parser, sync engine, surface store, JSON Pointer,
// dynamic binding, walker, markdown-lite, chart geometry, activity and
// freshness. No Android types. Bytecode 17 with NO toolchain so a JDK 25
// (this dev machine) and JDK 21 (CI) both compile it.
plugins {
    alias(libs.plugins.kotlin.jvm)
    alias(libs.plugins.kotlin.serialization)
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.core)
    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test.junit)
    testImplementation(libs.kotlinx.coroutines.test)
}

tasks.withType<Test>().configureEach {
    workingDir = projectDir
    // Relative, so the build-cache key is machine-independent (spec §7.1).
    systemProperty("nous.repoRoot", "../..")
    // Declared as inputs so a change to a golden vector or a catalog can
    // never leave this task UP-TO-DATE or FROM-CACHE.
    val root = rootProject.layout.projectDirectory.dir("..")
    inputs.dir(root.dir("nous/a2ui/catalogs"))
        .withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("catalogs")
    inputs.dir(root.dir("tests/fixtures/a2ui"))
        .withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("fixtures")
    inputs.file(root.file("dashboard-app/src/companion/catalog/__fixtures__/f096-report-app.json"))
        .withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("f096")
    maxHeapSize = "2g"
    testLogging { events("failed"); exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL }
}
