package us.fatykhov.nous.companion.data

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import us.fatykhov.nous.companion.core.SecretBox
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * AES-256-GCM with a key that lives in the Android Keystore under [alias].
 * The key is made inside the Keystore and app code cannot export it (it may
 * be hardware-backed); every seal uses a fresh IV. No user authentication is
 * required, so the app can send the token from a background worker.
 *
 * What this keeps: the token off the disk in plain text, out of any backup
 * and away from other apps. What it does not: code running in this app's
 * own process, or a rooted phone, can still ask the Keystore to decrypt. On
 * a debuggable build someone with USB debugging can also use the token, and
 * that build trusts user-installed certificate authorities; the release
 * build does neither.
 *
 * Any failure (no Keystore, a lost or invalidated key, a provider error,
 * which can be unchecked) answers null, and the app reads that as not paired.
 * Before a pairing the app tries a seal first and, when it fails, [reset]s
 * the key once (`DeviceCredential.ready`), so a key that stopped working is
 * replaced instead of failing every later pairing after the server issued a
 * token.
 */
class KeystoreSecretBox(private val alias: String = "nous_device_token_v1") : SecretBox {
    override fun seal(plain: ByteArray): SecretBox.Sealed? = try {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key(create = true))
        SecretBox.Sealed(cipher.doFinal(plain), cipher.iv)
    } catch (_: Exception) { null }

    override fun open(sealed: SecretBox.Sealed): ByteArray? = try {
        key(create = false)?.let { k ->
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, k, GCMParameterSpec(128, sealed.iv))
            cipher.doFinal(sealed.ciphertext)
        }
    } catch (_: Exception) { null }

    override fun reset() {
        try { KeyStore.getInstance(PROVIDER).apply { load(null) }.deleteEntry(alias) } catch (_: Exception) { }
    }

    private fun key(create: Boolean): SecretKey? {
        val store = KeyStore.getInstance(PROVIDER).apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        if (!create) return null
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, PROVIDER)
        gen.init(
            KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .setRandomizedEncryptionRequired(true)
                .setUserAuthenticationRequired(false)
                .build(),
        )
        return gen.generateKey()
    }

    private companion object {
        const val PROVIDER = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}
