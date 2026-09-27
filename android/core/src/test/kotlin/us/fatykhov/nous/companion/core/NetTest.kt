package us.fatykhov.nous.companion.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class NetTest {
    @Test fun hostOfStripsEverythingButTheHost() {
        assertEquals("nous.tail1234.ts.net", Net.hostOf("https://nous.tail1234.ts.net/companion#/s/x"))
        assertEquals("192.168.1.141", Net.hostOf("http://192.168.1.141:8383"))
        assertEquals("192.168.1.141", Net.hostOf("http://user:pw@192.168.1.141:8383/"))
        assertEquals("fd7a::1", Net.hostOf("http://[fd7a::1]:8080/x"))
        assertEquals("nous", Net.hostOf("nous:8000"))
    }

    @Test fun tailnetAndPrivateAddressesAreLocal() {
        for (h in listOf("100.64.0.1", "100.101.102.103", "100.127.255.254", "10.0.0.5", "172.16.0.1", "172.31.9.9",
            "192.168.1.141", "169.254.1.1", "127.0.0.1", "localhost", "nous.tail1234.ts.net", "nous.local", "nous", "::1", "fe80::1", "fd7a:115c::1"))
            assertTrue(Net.isLocalNetworkHost(h), h)
    }

    @Test fun publicAddressesAreNot() {
        for (h in listOf("100.63.255.255", "100.128.0.1", "172.32.0.1", "8.8.8.8", "nous.fatykhov.us", "example.com", "2001:db8::1", ""))
            assertFalse(Net.isLocalNetworkHost(h), h)
    }
}
