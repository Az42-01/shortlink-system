"""Base62 算法与辅助函数的单元测试。"""

from django.test import RequestFactory, SimpleTestCase

from shortener import utils
from shortener.utils import ShortCodeError, ShortCodeOverflow


class Base62EncodeTests(SimpleTestCase):
    def test_known_vectors(self):
        cases = {
            0: "0000",
            1: "0001",
            9: "0009",
            10: "000a",
            35: "000z",
            36: "000A",
            61: "000Z",
            62: "0010",
            62**2: "0100",
            62**3: "1000",
        }
        for number, expected in cases.items():
            with self.subTest(number=number):
                self.assertEqual(utils.encode_base62(number), expected)

    def test_round_trip(self):
        for number in range(0, 5000):
            code = utils.encode_base62(number)
            with self.subTest(number=number):
                self.assertEqual(utils.decode_base62(code), number)

    def test_round_trip_for_large_ids(self):
        ids = [62**4, 62**5, 62**6 - 2, utils.MAX_ENCODABLE_ID]
        for number in ids:
            with self.subTest(number=number):
                self.assertEqual(utils.decode_base62(utils.encode_base62(number)), number)

    def test_code_length_is_between_4_and_6(self):
        for number in (1, 61, 62, 62**2, 62**3, 62**5, utils.MAX_ENCODABLE_ID):
            length = len(utils.encode_base62(number))
            with self.subTest(number=number, length=length):
                self.assertGreaterEqual(length, utils.MIN_CODE_LENGTH)
                self.assertLessEqual(length, utils.MAX_CODE_LENGTH)

    def test_max_supported_id_is_six_uppercase_z(self):
        self.assertEqual(utils.encode_base62(utils.MAX_ENCODABLE_ID), "ZZZZZZ")
        self.assertEqual(utils.MAX_ENCODABLE_ID, 62**6 - 1)

    def test_ids_are_bijective_no_collisions(self):
        codes = {utils.generate_short_code(pk) for pk in range(1, 10001)}
        self.assertEqual(len(codes), 10000)

    def test_negative_id_rejected(self):
        with self.assertRaises(ShortCodeError):
            utils.encode_base62(-1)

    def test_overflow_rejected(self):
        with self.assertRaises(ShortCodeOverflow):
            utils.encode_base62(utils.MAX_ENCODABLE_ID + 1)

    def test_non_int_rejected(self):
        with self.assertRaises(ShortCodeError):
            utils.encode_base62("123")

    def test_decode_rejects_invalid_input(self):
        for bad in ("", "ab cd", "abc!", None, 123):
            with self.subTest(bad=bad):
                with self.assertRaises(ShortCodeError):
                    utils.decode_base62(bad)

    def test_decode_tolerates_padding(self):
        self.assertEqual(utils.decode_base62("0001"), 1)
        self.assertEqual(utils.decode_base62("000z"), 35)


class ShortCodeValidationTests(SimpleTestCase):
    def test_is_valid_short_code(self):
        for good in ("0001", "aB3x", "zzzz", "AbCdEf", "123456"):
            with self.subTest(code=good):
                self.assertTrue(utils.is_valid_short_code(good))

    def test_rejects_bad_values(self):
        for bad in ("", None, "abc", "abcdefg", "abcd-", "a b c", "中文短码"):
            with self.subTest(code=bad):
                self.assertFalse(utils.is_valid_short_code(bad))


class NormalizeUrlTests(SimpleTestCase):
    def test_adds_scheme(self):
        self.assertEqual(utils.normalize_url("example.com"), "http://example.com/")

    def test_keeps_path_and_query(self):
        self.assertEqual(
            utils.normalize_url("example.com/path?utm=1"),
            "http://example.com/path?utm=1",
        )

    def test_lowercases_scheme_and_host(self):
        self.assertEqual(
            utils.normalize_url("HTTPS://Example.COM/Path?A=1"),
            "https://example.com/Path?A=1",
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            utils.normalize_url("  https://example.com/a  "),
            "https://example.com/a",
        )

    def test_rejects_unsupported_scheme(self):
        for bad in ("ftp://example.com/a", "javascript:alert(1)//"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    utils.normalize_url(bad)

    def test_rejects_empty(self):
        for bad in ("", "   ", None):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    utils.normalize_url(bad)


class RequestHelperTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def test_prefers_first_forwarded_for(self):
        request = self.factory.get(
            "/x", HTTP_X_FORWARDED_FOR="203.0.113.9, 10.0.0.1", REMOTE_ADDR="10.0.0.2"
        )
        self.assertEqual(utils.get_client_ip(request), "203.0.113.9")

    def test_falls_back_to_remote_addr(self):
        request = self.factory.get("/x", REMOTE_ADDR="198.51.100.7")
        self.assertEqual(utils.get_client_ip(request), "198.51.100.7")

    def test_invalid_ip_returns_empty_string(self):
        request = self.factory.get("/x", HTTP_X_FORWARDED_FOR="unknown", REMOTE_ADDR="not-an-ip")
        self.assertEqual(utils.get_client_ip(request), "")

    def test_supports_ipv6(self):
        request = self.factory.get("/x", REMOTE_ADDR="2001:db8::1")
        self.assertEqual(utils.get_client_ip(request), "2001:db8::1")

    def test_user_agent_truncated(self):
        self.assertEqual(len(utils.truncate_user_agent("a" * 900)), 512)
        self.assertEqual(utils.truncate_user_agent("Mozilla/5.0"), "Mozilla/5.0")
        self.assertEqual(utils.truncate_user_agent(None), "")


class DeviceTypeParsingTests(SimpleTestCase):
    """看板饼图的设备分类：只分 PC / 移动端 / 其他 三类。"""

    def test_mobile_user_agents(self):
        cases = (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36",
            "Mozilla/5.0 (iPad; CPU OS 16_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
            "Mozilla/5.0 (Linux; Android 12; SM-T870) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
            "Mozilla/5.0 (compatible; MSIE 10.0; Windows Phone 8.0; IEMobile/10.0)",
        )
        for ua in cases:
            with self.subTest(ua=ua):
                self.assertEqual(utils.parse_device_type(ua), utils.DEVICE_MOBILE)

    def test_desktop_user_agents(self):
        cases = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 Chrome/101.0 Safari/537.36",
        )
        for ua in cases:
            with self.subTest(ua=ua):
                self.assertEqual(utils.parse_device_type(ua), utils.DEVICE_PC)

    def test_other_user_agents(self):
        cases = (
            "",
            "   ",
            None,
            "curl/8.5.0",
            "PostmanRuntime/7.37.0",
            "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        )
        for ua in cases:
            with self.subTest(ua=ua):
                self.assertEqual(utils.parse_device_type(ua), utils.DEVICE_OTHER)

    def test_iphone_is_not_mistaken_for_mac(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15"
        self.assertEqual(utils.parse_device_type(ua), utils.DEVICE_MOBILE)

    def test_device_labels_cover_all_types(self):
        self.assertEqual(utils.DEVICE_TYPES, ("pc", "mobile", "other"))
        for device_type in utils.DEVICE_TYPES:
            with self.subTest(device_type=device_type):
                self.assertTrue(utils.DEVICE_TYPE_LABELS[device_type])
        self.assertEqual(utils.device_label("pc"), "PC")
        self.assertEqual(utils.device_label("mobile"), "移动端")
        # 未知类型降级为「其他」，避免前端出现 undefined
        self.assertEqual(utils.device_label("watch"), "其他")

