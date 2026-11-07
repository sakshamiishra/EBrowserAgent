from __future__ import annotations

import time
import unittest

from app.security.url_policy import URLPolicyError, validate_target_url


class URLPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_public_http_and_https_urls(self) -> None:
        resolver = lambda _host: ["93.184.216.34"]
        for url in ("http://example.com/path", "https://sub.example.com:8443/"):
            with self.subTest(url=url):
                self.assertEqual(await validate_target_url(url, resolver=resolver), url)

    async def test_hostname_case_and_trailing_dot_are_canonicalized(self) -> None:
        seen_hosts: list[str] = []

        def resolve(host: str) -> list[str]:
            seen_hosts.append(host)
            return ["93.184.216.34"]

        result = await validate_target_url("HTTPS://ExAmPlE.COM.:443/path", resolver=resolve)
        self.assertEqual(result, "https://example.com:443/path")
        self.assertEqual(seen_hosts, ["example.com"])

    async def test_non_ascii_idn_hostname_is_rejected(self) -> None:
        with self.assertRaisesRegex(URLPolicyError, "ASCII"):
            await validate_target_url("https://bücher.example/")

    async def test_legacy_numeric_ip_hostnames_are_rejected(self) -> None:
        for host in ("2130706433", "0177.0.0.1", "0x7f000001", "0x7f.1", "0x", "127.1"):
            with self.subTest(host=host), self.assertRaises(URLPolicyError):
                await validate_target_url(f"http://{host}/")

    async def test_localhost_and_subdomains_are_rejected(self) -> None:
        for url in ("http://localhost", "https://admin.localhost/path", "https://localhost./"):
            with self.subTest(url=url), self.assertRaises(URLPolicyError):
                await validate_target_url(url)

    async def test_non_global_ipv4_ranges_are_rejected(self) -> None:
        for address in (
            "127.0.0.1", "10.0.0.5", "172.16.0.2", "192.168.1.8", "169.254.1.1",
            "224.0.0.1", "240.0.0.1", "192.0.2.1", "255.255.255.255",
        ):
            with self.subTest(address=address), self.assertRaises(URLPolicyError):
                await validate_target_url(f"http://{address}/")

    async def test_non_global_ipv6_and_private_ipv4_mapped_ipv6_are_rejected(self) -> None:
        for address in ("[::1]", "[fc00::1]", "[fe80::1]", "[ff02::1]", "[2001:db8::1]", "[::ffff:127.0.0.1]"):
            with self.subTest(address=address), self.assertRaises(URLPolicyError):
                await validate_target_url(f"http://{address}/")

    async def test_public_ip_literals_are_allowed(self) -> None:
        self.assertEqual(await validate_target_url("https://1.1.1.1/"), "https://1.1.1.1/")
        self.assertEqual(
            await validate_target_url("https://[2606:4700:4700::1111]/"),
            "https://[2606:4700:4700::1111]/",
        )

    async def test_dns_answer_with_any_private_address_is_rejected(self) -> None:
        with self.assertRaisesRegex(URLPolicyError, "non-public IP"):
            await validate_target_url(
                "https://mixed.example/",
                resolver=lambda _host: ["93.184.216.34", "10.0.0.8"],
            )

    async def test_dns_answer_with_private_ipv6_is_rejected(self) -> None:
        with self.assertRaises(URLPolicyError):
            await validate_target_url(
                "https://mixed.example/", resolver=lambda _host: ["2606:4700:4700::1111", "::1"]
            )

    async def test_dns_resolution_timeout_fails_closed(self) -> None:
        def slow_resolve(_host: str) -> list[str]:
            time.sleep(0.05)
            return ["93.184.216.34"]

        with self.assertRaisesRegex(URLPolicyError, "timed out"):
            await validate_target_url(
                "https://slow.example/", resolver=slow_resolve, dns_timeout_seconds=0.001
            )

    async def test_invalid_credentials_scheme_host_and_port_are_rejected(self) -> None:
        cases = (
            "ftp://example.com/",
            "https://user:secret@example.com/",
            "https:///missing-host",
            "https://bad host/",
            "https://example.com:99999/",
            "not a url",
        )
        for url in cases:
            with self.subTest(url=url), self.assertRaises(URLPolicyError):
                await validate_target_url(url, resolver=lambda _host: ["93.184.216.34"])

    async def test_dns_failure_is_fail_closed(self) -> None:
        def fail(_host: str) -> list[str]:
            raise OSError("DNS failure")

        with self.assertRaises(URLPolicyError):
            await validate_target_url("https://unresolvable.example/", resolver=fail)


if __name__ == "__main__":
    unittest.main()
