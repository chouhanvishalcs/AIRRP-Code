import unittest

from airrp_entity import knowledge as kb
from airrp_entity.htmlparse import parse_html
from airrp_entity.identifiers import (abn_ok, che_ok, cnpj_ok, find_identifiers, gstin_ok, jp_corporate_number_ok,
                                      lei_ok, luhn_ok)


class CheckDigitTests(unittest.TestCase):
    def test_published_examples_validate_and_one_changed_digit_fails(self):
        cases = [(lei_ok, "506700GE1G29325QX363", "506700GE1G29325QX364"),     # GLEIF's own LEI
                 (jp_corporate_number_ok, "7000012050002", "8000012050002"),   # National Tax Agency's corporate number
                 (abn_ok, "51824753556", "51824753557"),                       # ATO's documented example ABN
                 (gstin_ok, "27AAPFU0939F1ZV", "27AAPFU0939F1ZW"),
                 (cnpj_ok, "11222333000181", "11222333000182"),
                 (luhn_ok, "732829320", "732829321"),
                 (che_ok, "116281710", "116281711")]
        for fn, good, bad in cases:
            self.assertTrue(fn(good), (fn.__name__, good))
            self.assertFalse(fn(bad), (fn.__name__, bad))


class FinderTests(unittest.TestCase):
    def find(self, text):
        return {(i.scheme, i.value): i for i in find_identifiers(text)}

    def test_uk_legal_notice(self):
        ids = self.find("Registered in England and Wales No. 1234567. Registered office: 1 Example Street. "
                        "VAT No. GB 123 4567 89. LEI: 506700GE1G29325QX363.")
        self.assertIn(("UK-CRN", "01234567"), ids)
        self.assertEqual(ids[("UK-CRN", "01234567")].country, "GB")
        self.assertIn(("VAT", "GB123456789"), ids)                    # stops before ". LEI"
        self.assertTrue(ids[("LEI", "506700GE1G29325QX363")].labelled)

    def test_regulator_numbers_are_not_company_numbers(self):
        ids = self.find("FCA registration number 123456. Company Registration No. 2024-ABC-1.")
        self.assertEqual(set(ids), {("REG", "2024-ABC-1")})

    def test_german_imprint_and_japanese_corporate_number(self):
        ids = self.find("Registergericht: Amtsgericht München, HRB 123456\nUSt-IdNr.: DE123456789\n法人番号 7000012050002")
        self.assertEqual(ids[("DE-HR", "HRB 123456 (Amtsgericht München)")].extra["court"], "München")
        self.assertIn(("VAT", "DE123456789"), ids)
        self.assertTrue(ids[("JP-CN", "7000012050002")].checksum)

    def test_invalid_check_digit_is_not_reported(self):
        self.assertEqual(self.find("法人番号 8000012050002 / LEI 506700GE1G29325QX364"), {})

    def test_indian_cin_carries_ownership_class(self):
        ids = self.find("CIN: U40101DL2001GOI012345")
        cin = ids[("IN-CIN", "U40101DL2001GOI012345")]
        self.assertEqual((cin.country, cin.extra["class"], cin.extra["listed"], cin.labelled), ("IN", "GOI", False, True))

    def test_bare_twenty_character_tokens_need_a_valid_lei_check_digit(self):
        self.assertEqual(self.find("order ref ABCDEFGHIJKLMNOPQR12 and 12345678901234567890"), {})


class KnowledgeTests(unittest.TestCase):
    def test_legal_form_from_suffix_refined_by_jurisdiction(self):
        self.assertEqual(kb.legal_form_of("Sony Group Corporation", "JP")["key"], "JP-KK")
        self.assertEqual(kb.legal_form_of("Sony Group Corporation")["key"], "CORP")
        self.assertEqual(kb.legal_form_of("ソニーグループ株式会社")["key"], "JP-KK")
        self.assertEqual(kb.legal_form_of("Beispiel GmbH & Co. KG")["key"], "DE-GMBH-CO-KG")
        self.assertEqual(kb.legal_form_of("Acme Pvt. Ltd.")["suffix"], "Pvt. Ltd.")
        self.assertIsNone(kb.legal_form_of("Northwind"))

    def test_places(self):
        self.assertEqual(kb.lookup_place("England and Wales"), ("GB", "United Kingdom (England and Wales)"))
        self.assertEqual(kb.lookup_place("the State of Delaware")[0], "US-DE")
        self.assertEqual(kb.lookup_place("Georgia")[0], "GE")
        self.assertEqual(kb.lookup_place("Georgia", prefer_states=True)[0], "US-GA")
        self.assertEqual(kb.country_in_text("1-7-1 Konan Minato-ku, Tokyo 108-0075 Japan"), "JP")
        self.assertIsNone(kb.country_in_text("Atlanta, Georgia 30303"))


class HtmlTests(unittest.TestCase):
    def test_structures_a_profile_needs(self):
        p = parse_html("""<html lang="en-GB"><head><title>Corporate Data</title>
            <link rel="alternate" hreflang="de-de" href="/de/">
            <script type="application/ld+json">{"@graph": [{"@type": "Organization", "legalName": "X plc"}]}</script>
            <script>document.write("<td>not text</td>")</script></head><body>
            <a href="legal.html"><img alt="Legal notice"></a><a href="javascript:void(0)">js</a>
            <table><tr><th>Corporate Name</th><td>X plc</td></tr><tr><th>Head Office<td>1 St,<br>London</table>
            <dl><dt>Business<dd>Widgets &amp; gadgets</dl><address>1 Street<br>London</address>
            <p>Registered office: 1 Example
            Street</p></body></html>""", "https://www.x.example/en/corp/")
        self.assertEqual(p.lang, "en-GB")
        self.assertEqual(p.alternates, [("de-de", "https://www.x.example/de/")])
        self.assertEqual(p.jsonld[0]["legalName"], "X plc")
        self.assertEqual([(l.url, l.text) for l in p.links], [("https://www.x.example/en/corp/legal.html", "Legal notice")])
        self.assertEqual(p.pairs, [("Corporate Name", "X plc"), ("Head Office", "1 St, London"),
                                   ("Business", "Widgets & gadgets")])
        self.assertEqual(p.addresses, ["1 Street, London"])
        self.assertIn("Registered office: 1 Example Street", p.text)     # source line breaks are not text breaks
        self.assertNotIn("not text", p.text)


if __name__ == "__main__":
    unittest.main()
