import csv
import io
import unittest

from airrp_entity.profile import build_profile, to_markdown, to_subsidiaries_csv

from .entity_helpers import GLEIF_API, FixtureFetcher, child_record, fixture_json, gleif_routes

SAKURA = "https://www.sakura-denki.example/en/"
SEARCH = "filter%5Bentity.legalName%5D=Sakura%20Denki%20Corporation&page%5Bsize%5D=20"
EUROPE, AMERICA, GERMANY, OLD = ("9845002SKREUROPE0061", "9845002SKRAMERICA015", "9845002SKRGERMANY032",
                                 "9845002SKROLDLTD0035")


def group_routes():
    """Sakura Denki Corporation and four subsidiaries reported to GLEIF over two result pages (one inactive)."""
    parent = fixture_json("sakura-record.json")
    eu = child_record(parent, EUROPE, "Sakura Electronics Europe B.V.", "NL", "Amsterdam", "1011 AA", "NL",
                      registered_as="12345678", legal_form="54M6")
    us = child_record(parent, AMERICA, "Sakura Denki America, Inc.", "US", "San Jose", "95110", "US-DE")
    de = child_record(parent, GERMANY, "Sakura Denki Deutschland GmbH", "DE", "Berlin", "10115", "DE")
    old = child_record(parent, OLD, "Sakura Old Trading Ltd.", "GB", "London", "EC1A 1AA", "GB", status="INACTIVE")
    bv = {"data": {"attributes": {"names": [{"localName": "besloten vennootschap met beperkte aansprakelijkheid",
                                             "language": "Dutch", "abbreviationLocal": "B.V."}]}}}
    return gleif_routes(parent, legal_forms={"T417": fixture_json("legal-form-T417.json"), "54M6": bv},
                        authorities={"RA000412": fixture_json("ra-RA000412.json")}, searches={SEARCH: [parent]},
                        children={parent["data"]["attributes"]["lei"]: [[eu, us], [de, old]]})


def run(**kw):
    return build_profile([SAKURA], fetcher=FixtureFetcher(group_routes()), gleif_api=GLEIF_API, **kw)


class SubsidiaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry_only = run()
        cls.crawled = run(crawl_subsidiaries=5)

    def entity(self, profile, lei):
        return next(e for e in profile["subsidiaries"]["entities"] if e["lei"] == lei)

    def test_every_page_of_subsidiaries_is_read_and_counted(self):
        s = self.registry_only["subsidiaries"]
        self.assertEqual((s["relation"], s["total_reported"], s["fetched"], s["active"], s["countries"]),
                         ("ultimate-children", 4, 4, 3, 3))
        self.assertEqual({e["lei"] for e in s["entities"]}, {EUROPE, AMERICA, GERMANY, OLD})
        self.assertEqual([c["country"] for c in s["by_country"]], ["DE", "NL", "US"])      # the inactive one is not counted

    def test_each_subsidiary_gets_the_registry_fields(self):
        f = self.entity(self.registry_only, AMERICA)["fields"]
        self.assertEqual(f["registered_legal_name"]["value"], "Sakura Denki America, Inc.")
        self.assertEqual(f["registration_jurisdiction"]["value"], "Delaware, United States")
        self.assertEqual(f["registered_office"]["value"], "San Jose Example Road 1, San Jose, 95110, US")
        self.assertEqual((f["entity_operating_status"]["value"], f["entity_operating_status"]["status"]),
                         ("Active", "confirmed"))
        self.assertEqual(f["legal_entity_type"]["value"], "Corporation (stock corporation)")
        self.assertEqual(f["industry_sector"]["status"], "not_found")           # needs its website: nothing invented
        self.assertEqual(self.entity(self.registry_only, OLD)["fields"]["entity_operating_status"]["value"], "Inactive")

    def test_parent_operating_locations_list_the_countries_of_its_subsidiaries(self):
        locations = self.registry_only["fields"]["operating_location"]["value"]
        self.assertIn("Netherlands: 1 subsidiary (Amsterdam)", locations)
        self.assertIn("United States: 1 subsidiary (San Jose)", locations)
        self.assertIn("Germany: 1 subsidiary (Berlin)", locations)
        self.assertFalse([v for v in locations if v.startswith("United Kingdom")])  # its only UK entity is inactive

    def test_country_sites_are_found_but_only_crawled_on_request(self):
        sites = {s["site"] for s in self.registry_only["related_sites"]}
        self.assertEqual(sites, {"sakura-denki-europe.example", "sakura-denki-us.example"})
        self.assertEqual(self.registry_only["subsidiaries"]["websites_crawled"], [])

    def test_a_crawled_site_is_merged_into_the_subsidiary_it_belongs_to(self):
        e = self.entity(self.crawled, EUROPE)
        self.assertEqual((e["website"], e["profile_source"]),
                         ("https://www.sakura-denki-europe.example/", "registry + website"))
        f = e["fields"]
        self.assertTrue(f["registered_legal_name"]["corroborated"])
        self.assertTrue(f["corporate_registration_reference"]["corroborated"])   # KvK number on the site = registry
        self.assertTrue(f["registered_office"]["corroborated"])
        self.assertEqual(f["legal_entity_type"]["value"], "Besloten vennootschap (B.V., private limited company)")
        self.assertTrue({"Germany", "France", "Netherlands", "United Kingdom"} <= set(f["markets_served"]["value"]))
        self.assertIn("Consumer electronics (Consumer Discretionary)", f["industry_sector"]["value"])
        unmatched = self.crawled["subsidiaries"]["unmatched_sites"]
        self.assertEqual([(u["url"], u["reason"]) for u in unmatched],
                         [("https://www.sakura-denki-us.example/", "no page could be fetched")])
        # the parent's own profile is unchanged by its subsidiaries' websites
        self.assertEqual(self.crawled["fields"]["registered_legal_name"]["value"], "さくら電機株式会社")

    def test_a_site_that_matches_no_subsidiary_is_reported_not_attached(self):
        p = build_profile([SAKURA], fetcher=FixtureFetcher(group_routes()), gleif_api=GLEIF_API, crawl_subsidiaries=5,
                          subsidiary_sites=[{"url": "https://www.northwind.example/"}])
        u = p["subsidiaries"]["unmatched_sites"][0]
        self.assertEqual(u["legal_name_found"], "Northwind Example Holdings plc")
        self.assertIn("verify it belongs to the group", u["reason"])
        self.assertEqual(u["fields"]["registered_legal_name"]["value"], "Northwind Example Holdings plc")
        self.assertTrue(all(e["website"] != "https://www.northwind.example/" for e in p["subsidiaries"]["entities"]))

    def test_csv_and_markdown(self):
        rows = list(csv.DictReader(io.StringIO(to_subsidiaries_csv(self.crawled))))
        europe = next(r for r in rows if r["lei"] == EUROPE)
        self.assertEqual((europe["registered_legal_name"], europe["registration_jurisdiction"], europe["website"]),
                         ("Sakura Electronics Europe B.V.", "Netherlands", "https://www.sakura-denki-europe.example/"))
        self.assertEqual(len(rows), 4)                # the unreachable site has no fields, so no row
        md = to_markdown(self.crawled)
        self.assertIn("## Subsidiaries (4 read of 4 reported to GLEIF; 3 active, in 3 countries)", md)
        self.assertIn("| Sakura Denki America, Inc. | [9845002SKRAMERICA015]", md)

    def test_can_be_switched_off(self):
        p = run(subsidiaries=False)
        self.assertIsNone(p["subsidiaries"])
        self.assertFalse([v for v in p["fields"]["operating_location"]["value"] if "subsidiar" in v])

    def test_a_cap_on_subsidiaries_is_reported(self):
        p = run(max_subsidiaries=2)
        self.assertEqual(p["subsidiaries"]["fetched"], 2)
        self.assertTrue(any("GLEIF reports 4 subsidiaries" in n for n in p["notes"]))


if __name__ == "__main__":
    unittest.main()
