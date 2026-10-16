import copy
import unittest

from airrp_entity.fetch import Response
from airrp_entity.profile import build_profile, to_csv, to_markdown

from .entity_helpers import GLEIF_API, FixtureFetcher, fixture_json, gleif_routes

NORTHWIND = "https://www.northwind.example/"
SAKURA = "https://www.sakura-denki.example/en/"
SAKURA_SEARCH = "filter%5Bentity.legalName%5D=Sakura%20Denki%20Corporation&page%5Bsize%5D=20"


def values(field):
    return [c["value"] for c in field["candidates"]]


def profile(url, routes=None, **kw):
    kw.setdefault("registry", bool(routes))
    return build_profile([url], fetcher=FixtureFetcher(routes), gleif_api=GLEIF_API, **kw)


def sakura_routes(record=None, searches=None):
    rec = record or fixture_json("sakura-record.json")
    return gleif_routes(rec, legal_forms={"T417": fixture_json("legal-form-T417.json")},
                        authorities={"RA000412": fixture_json("ra-RA000412.json")},
                        searches=searches if searches is not None else {SAKURA_SEARCH: [rec]})


class WebsiteOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = profile(NORTHWIND)
        cls.f = cls.p["fields"]

    def test_every_field_is_answered_from_the_uk_fixture(self):
        expected = {
            "registered_legal_name": "Northwind Example Holdings plc",
            "legal_entity_type": "Public limited company (plc)",
            "registration_jurisdiction": "United Kingdom (England and Wales)",
            "registered_office": "1 Example Street, London EC1A 1AA, United Kingdom",
            "entity_operating_status": "Active",
            "government_public_sector_status": "Private sector (publicly listed company)",
        }
        for key, value in expected.items():
            self.assertEqual(self.f[key]["value"], value, key)
        self.assertEqual(self.f["registered_legal_name"]["status"], "confirmed")
        self.assertEqual(set(self.f["corporate_registration_reference"]["value"]), {
            "Legal Entity Identifier (LEI): 9845001NWXTEST000047 (check digit valid)",
            "UK Companies House company number: 01234567", "VAT identification number: GB123456789"})
        self.assertEqual(self.f["operating_location"]["value"][0], "1 Example Street, London EC1A 1AA, United Kingdom")
        self.assertIn("Banks (Financials)", self.f["industry_sector"]["value"])
        self.assertTrue({"Germany", "France", "Japan", "United States", "United Kingdom", "Ireland"}
                        <= set(self.f["markets_served"]["value"]))
        self.assertEqual(self.f["regulated_activity"]["value"][0], "Banking / deposit-taking")
        regs = {c["value"]: c for c in self.f["regulator_or_licence"]["candidates"]}
        fca = regs["Financial Conduct Authority (FCA), United Kingdom"]
        self.assertEqual(fca["confidence"], "high")
        self.assertIn("FCA Firm Reference Number: 123456", fca["details"])
        self.assertIn("Prudential Regulation Authority (PRA), United Kingdom", self.f["regulator_or_licence"]["value"])

    def test_offices_in_several_countries_are_operating_locations(self):
        locations = self.f["operating_location"]["value"]
        for office in ("200 Harbour Road, Leeds LS1 4AP, United Kingdom",          # contact page <address>
                       "Beispielweg 5, 60311 Frankfurt am Main, Deutschland",      # office directory
                       "2-2-2 Marunouchi, Chiyoda-ku, Tokyo 100-0005, Japan",
                       "5 Dock Lane, Bristol, BS1 4ND, GB"):                       # JSON-LD subOrganization
            self.assertIn(office, locations)
        # the directory's London office is the head office already, not a second location
        self.assertEqual(sum("1 Example Street" in v for v in locations), 1)
        # a subOrganization in JSON-LD is another company: its name is not the site's legal name
        self.assertFalse([c for c in self.f["registered_legal_name"]["candidates"]
                          if any("subOrganization" in e["method"] for e in c["evidence"])])

    def test_website_alone_never_confirms_status_or_sector_ownership(self):
        self.assertEqual(self.f["entity_operating_status"]["status"], "inferred")
        self.assertEqual(self.f["government_public_sector_status"]["status"], "inferred")

    def test_every_value_carries_evidence(self):
        for key, fd in self.f.items():
            for c in fd["candidates"]:
                self.assertTrue(c["evidence"], key)
                for ev in c["evidence"]:
                    self.assertTrue(ev["url"].startswith("https://www.northwind.example/"), (key, ev))
                    self.assertTrue(ev["method"], key)

    def test_crawl_is_polite_and_focused(self):
        urls = {p["url"] for p in self.p["pages"]}
        self.assertIn(NORTHWIND + "legal/", urls)
        self.assertIn(NORTHWIND + "regulatory/", urls)
        self.assertNotIn(NORTHWIND + "news/2026/new-app/", urls)                       # news is not entity data
        self.assertEqual(self.p["skipped"], [{"url": NORTHWIND + "private/secret/", "reason": "disallowed by robots.txt"}])
        self.assertEqual([d["url"] for d in self.p["documents_for_review"]], [NORTHWIND + "docs/annual-report-2025.pdf"])

    def test_boilerplate_does_not_change_status_or_sector(self):
        # the T&Cs mention liquidation and the regulatory page calls the FCA a public body; neither is about Northwind
        self.assertNotIn("In liquidation", values(self.f["entity_operating_status"]))
        self.assertNotIn("Government / public-sector body", values(self.f["government_public_sector_status"]))

    def test_reports(self):
        md = to_markdown(self.p)
        self.assertIn("| Registered legal name | Northwind Example Holdings plc | high | confirmed |", md)
        self.assertIn("## Documents to review (not parsed)", md)
        rows = to_csv([self.p]).splitlines()
        self.assertTrue(rows[0].startswith("input_url,registered_legal_name,registered_legal_name_confidence"))
        self.assertIn("Northwind Example Holdings plc", rows[1])


class OtherLayoutsTests(unittest.TestCase):
    def test_german_impressum(self):
        f = profile("https://www.beispiel.example/impressum/")["fields"]
        self.assertEqual(f["registered_legal_name"]["value"], "Beispiel Technik GmbH")
        self.assertIn("German commercial register (Handelsregister): HRB 123456 (Amtsgericht München)",
                      f["corporate_registration_reference"]["value"])
        self.assertEqual((f["registration_jurisdiction"]["value"], f["registration_jurisdiction"]["status"]),
                         ("Germany", "confirmed"))
        self.assertEqual(f["registered_office"]["value"], "München (registered seat)")
        self.assertEqual(f["operating_location"]["value"], ["Musterstraße 1, 80331 München, Deutschland"])
        self.assertTrue(f["legal_entity_type"]["value"].startswith("Gesellschaft mit beschränkter Haftung"))

    def test_indian_government_company(self):
        f = profile("https://www.example-psu.example/")["fields"]
        self.assertEqual(f["registered_legal_name"]["value"], "Example Power Corporation Limited")
        self.assertEqual((f["government_public_sector_status"]["value"], f["government_public_sector_status"]["status"]),
                         ("Government company (India)", "confirmed"))
        self.assertEqual(f["legal_entity_type"]["value"], "Government company (Government of India)")
        self.assertEqual(f["registration_jurisdiction"]["value"], "India")
        self.assertEqual(f["registered_office"]["value"], "Example Bhawan, 7 Example Marg, New Delhi 110001, India")

    def test_a_list_of_group_companies_does_not_compete_with_the_entity_name(self):
        p = profile(SAKURA)
        self.assertIn("https://www.sakura-denki.example/en/corporate/group/", [x["url"] for x in p["pages"]])
        name = p["fields"]["registered_legal_name"]
        self.assertEqual((name["value"], name["status"]), ("Sakura Denki Corporation", "confirmed"))
        others = [c for c in name["candidates"][1:] if c["value"].startswith("Sakura ")]
        self.assertTrue(all(c["confidence"] == "low" for c in others), others)

    def test_nothing_is_invented_when_the_site_states_nothing(self):
        p = profile("https://www.bare.example/")
        for key, fd in p["fields"].items():
            if key == "entity_operating_status":
                self.assertEqual((fd["value"], fd["confidence"]), ("Active", "low"))   # only "the website is live"
            else:
                self.assertEqual(fd["status"], "not_found", key)
        self.assertTrue(any("No legal-notice" in n for n in p["notes"]))

    def test_unreachable_site(self):
        p = build_profile(["https://www.unknown.example/"], fetcher=FixtureFetcher(), registry=False)
        self.assertEqual(p["pages"], [])
        self.assertTrue(p["notes"][0].startswith("No page could be fetched"))
        self.assertTrue(all(fd["status"] == "not_found" for fd in p["fields"].values()))


class RegistryTests(unittest.TestCase):
    def test_exact_name_match_corroborates_and_registered_name_wins(self):
        p = profile(SAKURA, sakura_routes())
        f = p["fields"]
        self.assertEqual(p["registry"]["lei"], "9845002SKRDENKI00040")
        self.assertEqual(p["registry"]["matched_by"], "exact legal-name match for 'Sakura Denki Corporation'")
        name = f["registered_legal_name"]
        self.assertEqual((name["value"], name["status"]), ("さくら電機株式会社", "confirmed"))
        self.assertTrue(name["candidates"][0]["corroborated"])
        self.assertIn("Sakura Denki Corporation", name["candidates"][0]["also_seen_as"])
        refs = {c["value"]: c for c in f["corporate_registration_reference"]["candidates"]}
        number = refs["Corporate Number Publication Site (National Tax Agency): 3010401999999"]
        self.assertTrue(number["corroborated"])                        # same number the website labels 'Corporate Number'
        self.assertIn("Legal Entity Identifier (LEI): 9845002SKRDENKI00040 (check digit valid)", refs)
        self.assertEqual(f["legal_entity_type"]["value"], "Kabushiki kaisha (joint-stock company)")
        self.assertTrue(f["legal_entity_type"]["candidates"][0]["corroborated"])
        self.assertEqual(f["registration_jurisdiction"]["value"], "Japan")
        office = f["registered_office"]["candidates"][0]
        self.assertTrue(office["corroborated"])                        # same postal code as the website's head office
        self.assertEqual((f["entity_operating_status"]["value"], f["entity_operating_status"]["status"]),
                         ("Active", "confirmed"))

    def test_lei_on_the_website_is_looked_up_directly(self):
        rec = copy.deepcopy(fixture_json("sakura-record.json"))
        a = rec["data"]["attributes"]
        a["lei"] = rec["data"]["id"] = "9845001NWXTEST000047"
        a["entity"].update({"legalName": {"name": "Northwind Example Holdings plc", "language": "en"}, "otherNames": [],
                            "transliteratedOtherNames": [], "jurisdiction": "GB", "registeredAs": "01234567",
                            "registeredAt": {"id": "RA000585"}, "legalForm": {"id": None, "other": "plc"},
                            "otherAddresses": [], "status": "ACTIVE",
                            "legalAddress": {"addressLines": ["1 Example Street"], "city": "London", "country": "GB",
                                             "postalCode": "EC1A 1AA"},
                            "headquartersAddress": {"addressLines": ["1 Example Street"], "city": "London",
                                                    "country": "GB", "postalCode": "EC1A 1AA"}})
        p = profile(NORTHWIND, gleif_routes(rec))
        f = p["fields"]
        self.assertEqual(p["registry"]["matched_by"], "LEI 9845001NWXTEST000047 published on the website")
        crn = next(c for c in f["corporate_registration_reference"]["candidates"] if "01234567" in c["value"])
        self.assertTrue(crn["corroborated"])
        self.assertEqual(f["registered_office"]["status"], "confirmed")
        self.assertTrue(f["registered_office"]["candidates"][0]["corroborated"])
        self.assertEqual(f["entity_operating_status"]["status"], "confirmed")

    def test_disagreement_is_a_conflict_for_a_person(self):
        rec = copy.deepcopy(fixture_json("sakura-record.json"))
        rec["data"]["attributes"]["entity"]["legalAddress"].update({"addressLines": ["9-9-9 Other"], "postalCode": "530-0001",
                                                                   "city": "大阪府"})
        rec["data"]["attributes"]["entity"]["jurisdiction"] = "US-DE"
        p = profile(SAKURA, sakura_routes(record=rec))
        f = p["fields"]
        self.assertEqual(f["registration_jurisdiction"]["status"], "conflict")
        self.assertTrue(any(n.startswith("Registration jurisdiction: sources disagree") for n in p["notes"]))
        # the website never states a registered office (only a head office), so the registry's is not contradicted;
        # the head office stays a separate, uncorroborated candidate instead of being merged into it
        office = f["registered_office"]["candidates"]
        self.assertEqual(office[0]["value"], "9-9-9 Other, 大阪府, JP-13, 530-0001, JP")
        self.assertFalse(office[0]["corroborated"])
        self.assertIn("1-2-3 Konan Example, Minato-ku, Tokyo 108-0000 Japan", [c["value"] for c in office[1:]])

    def test_several_equal_matches_are_reported_not_chosen(self):
        rec = fixture_json("sakura-record.json")
        twin = copy.deepcopy(rec)
        twin["data"]["attributes"]["lei"] = twin["data"]["id"] = "9845003TWINRECORD050"
        p = profile(SAKURA, sakura_routes(searches={SAKURA_SEARCH: [rec, twin]}))
        self.assertIsNone(p["registry"])
        self.assertTrue(any("2 GLEIF records match 'Sakura Denki Corporation' equally well" in n for n in p["notes"]))
        self.assertEqual(p["fields"]["registered_legal_name"]["value"], "Sakura Denki Corporation")

    def test_no_match_and_unreachable_registry_leave_the_website_profile_intact(self):
        p = profile(SAKURA, routes={"https://unused.example/": "{}"})
        self.assertIsNone(p["registry"])
        self.assertTrue(any("no GLEIF record matched" in n for n in p["notes"]))

        class Offline(FixtureFetcher):
            def _get(self, url, accept):
                if "gleif" in url:
                    return Response(url, 0, "", b"", error="URLError: [Errno -2] Name or service not known")
                return super()._get(url, accept)
        p = build_profile([SAKURA], fetcher=Offline(), gleif_api=GLEIF_API)
        self.assertTrue(any("URLError" in n for n in p["notes"]))
        self.assertEqual(p["fields"]["registered_legal_name"]["value"], "Sakura Denki Corporation")

    def test_lei_from_the_website_that_the_registry_does_not_know(self):
        p = profile(NORTHWIND, routes={"https://unused.example/": "{}"})
        self.assertTrue(any("LEI 9845001NWXTEST000047 found on the website is not in the GLEIF registry" in n
                            for n in p["notes"]))


if __name__ == "__main__":
    unittest.main()
