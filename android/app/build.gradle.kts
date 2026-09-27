// `:app` — Nous Companion for Android (spec §7). Compose UI over `:core`.
// Compiled and tested in CI (Temurin 21 + Android SDK); this module is
// excluded from the build on a machine without an SDK.
plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Inside `android {}` the identifier `java` is the JavaPluginExtension, so the
// JDK decoder is reached through a top-level function instead.
fun decodeBase64(s: String): ByteArray = java.util.Base64.getDecoder().decode(s)

android {
    namespace = "us.fatykhov.nous.companion"
    compileSdk = 37

    defaultConfig {
        applicationId = "us.fatykhov.nous.companion"   // must equal NOUS_A2UI_ANDROID_PACKAGE (spec §6.7)
        minSdk = 26
        targetSdk = 37
        // 1000 + CI run number, so a local build (versionCode 1) never installs over a CI build.
        versionCode = (System.getenv("GITHUB_RUN_NUMBER")?.toIntOrNull()?.plus(1000)) ?: 1
        versionName = System.getenv("GITHUB_RUN_NUMBER")?.let { "0.1.$it-beta" } ?: "0.1.0-local"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    // Beta signing key from CI secrets when present (spec §11); never committed.
    val betaKeystoreB64 = System.getenv("ANDROID_BETA_KEYSTORE_B64")
    val betaKeystorePassword = System.getenv("ANDROID_BETA_KEYSTORE_PASSWORD")
    if (!betaKeystoreB64.isNullOrEmpty() && !betaKeystorePassword.isNullOrEmpty()) {
        val ks = layout.buildDirectory.file("beta.jks").get().asFile
        ks.parentFile.mkdirs()
        ks.writeBytes(decodeBase64(betaKeystoreB64))
        signingConfigs {
            getByName("debug") {
                storeFile = ks
                storePassword = betaKeystorePassword
                keyAlias = "beta"
                keyPassword = betaKeystorePassword
            }
        }
    }

    buildTypes {
        debug { isMinifyEnabled = false }
        release { isMinifyEnabled = false }   // beta: no R8 (spec §7.1)
    }

    buildFeatures { compose = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    testOptions {
        unitTests.isIncludeAndroidResources = true
        unitTests.isReturnDefaultValues = true
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

dependencies {
    implementation(project(":core"))
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.foundation)
    implementation(libs.compose.material3)
    implementation(libs.compose.ui.tooling.preview)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.lifecycle.viewmodel.compose)
    implementation(libs.lifecycle.process)
    implementation(libs.core.ktx)
    implementation(libs.work.runtime)
    implementation(libs.datastore.preferences)
    implementation(libs.okhttp)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)
    implementation(platform(libs.firebase.bom))
    implementation(libs.firebase.messaging)

    testImplementation(libs.junit)
    testImplementation(libs.kotlin.test.junit)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.kotlinx.coroutines.test)
    testImplementation(platform(libs.compose.bom))
    testImplementation(libs.compose.ui.test.junit4)
    debugImplementation(libs.compose.ui.test.manifest)
}

tasks.withType<Test>().configureEach {
    workingDir = projectDir
    systemProperty("nous.repoRoot", "../..")
    val root = rootProject.layout.projectDirectory.dir("..")
    inputs.dir(root.dir("nous/a2ui/catalogs")).withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("catalogs")
    inputs.dir(root.dir("tests/fixtures/a2ui")).withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("fixtures")
    inputs.file(root.file("android/catalog-coverage.json")).withPathSensitivity(PathSensitivity.RELATIVE).withPropertyName("manifest")
    maxHeapSize = "2g"
    testLogging { events("failed"); exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL }
}
