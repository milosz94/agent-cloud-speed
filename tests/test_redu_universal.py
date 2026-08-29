"""redu cost adapter: the UNIVERSAL managed-store sweep (Part 4 cost axis).

The defect this proves fixed: the old resolver enumerated managed datastores with per-type id branches
(db_id -> Postgres/MySQL, redis_id -> Redis) and had NO branch for managed ClickHouse, so a deploy's
ClickHouse tier fell off the bill entirely, and media_space was disclosed-unpriced by a hand-written line.
The new resolver drives enumeration from ONE data table (``_REDU_STORES``) and attributes each row to the
deployment by the deploy's private ``network_id`` (the universal handle: every VM the deploy stood up
shares it, so a store the deployment row never references - ClickHouse - is still caught) or by a
referenced id, pricing each at the flat ``pricing_rules`` rate surfaced through ``list_flavors.hourly_rate``
(the SAME source Stripe bills from). A resource TYPE with no code branch is caught the moment it is one row
in the table; a row that cannot be priced is disclosed by type+id, never dropped.

Offline: ``call_tool`` is injected with canned list_* responses (the REAL MCP structuredKeys, confirmed
against redu-mcp buildServer.js and the backend list controllers). FX is pinned so the run is deterministic.

GROUND-TRUTH NOTE (honest scope). The network_id-first attribution is the correct universal design, and it
prices ClickHouse the instant a network_id handle is present. Live TODAY the redu API under-exposes that
handle: neither list/get deployment nor the datastore list rows (except media_spaces) SELECT network_id,
and list_redis / list_relational_databases omit flavor_id. Those are one-column SELECT widenings in the
backend controllers, out of scope for the adapter; the canned rows here carry the fields to prove the
mechanism the widenings unlock. The referenced-id fallback already attributes Postgres/Redis/media_space
live (they are referenced by the deployment row); ClickHouse needs the network_id column exposed.
"""
import unittest

from acspeed.adapters import redu_cost
from acspeed.adapters.redu_cost import ReduRunRateAdapter

# The real redu list_flavors shape: prices under `hourly_rate` (GBP), the flavor bundles its disk.
REDU_FLAVORS = [
    {"id": "1", "name": "m1.tiny",   "vcpus": 1, "ram": 512,   "disk": 1,   "hourly_rate": 0.0118},
    {"id": "2", "name": "m1.small",  "vcpus": 1, "ram": 2048,  "disk": 20,  "hourly_rate": 0.0174},
    {"id": "3", "name": "m1.medium", "vcpus": 2, "ram": 4096,  "disk": 40,  "hourly_rate": 0.0278},
    {"id": "4", "name": "m1.large",  "vcpus": 4, "ram": 8192,  "disk": 80,  "hourly_rate": 0.0486},
    {"id": "5", "name": "m1.xlarge", "vcpus": 8, "ram": 16384, "disk": 160, "hourly_rate": 0.0764},
]

# pinned dated FX so the composed USD figure is deterministic (GBP -> USD at 1.27)
_FIXED_FX = lambda ccy, date: ((1.27, "2026-08-26", "test-fx") if ccy == "GBP" else None)


def _call_tool(deps, *, databases=None, relational=None, clickhouse=None, redis=None,
               media_spaces=None, extra=None):
    """Mimic redu_mcp_http.call_tool. Each list_* returns its canned rows under the REAL structuredKey
    (list_databases->'databases', list_relational_databases->'relational_databases',
    list_clickhouse_databases->'clickhouse', list_redis->'redis', list_media_spaces->'media_spaces').
    ``extra`` maps any additional tool name -> full response dict (used to exercise a FUTURE store type)."""
    keyed = {
        "list_deployments": {"deployments": deps},
        "list_flavors": {"flavors": REDU_FLAVORS},
        "list_databases": {"databases": databases or []},
        "list_relational_databases": {"relational_databases": relational or []},
        "list_clickhouse_databases": {"clickhouse": clickhouse or []},
        "list_redis": {"redis": redis or []},
        "list_media_spaces": {"media_spaces": media_spaces or []},
    }
    keyed.update(extra or {})

    def call(name, args):
        if name == "get_deployment":
            return next((d for d in deps if str(d.get("id")) == str(args.get("id"))), {})
        return keyed.get(name, {})
    return call


def _adapter(deps, **lists):
    return ReduRunRateAdapter(call_tool=_call_tool(deps, **lists), fx=_FIXED_FX)


class TestUniversalSweepPricesEveryType(unittest.TestCase):
    def test_postgres_clickhouse_redis_on_same_network_all_priced_by_flavor(self):
        """(a) The motivating gap: a deploy with Postgres + ClickHouse + Redis on ONE private network
        prices ALL THREE by flavor. ClickHouse is NOT referenced by the deployment row (no clickhouse_id)
        and is caught purely by shared network_id - the exact case the old per-type branches dropped."""
        net = "net-deploy-a"
        dep = {"id": 900, "flavor_id": "3", "region": "uk-london", "network_id": net,
               "db_id": "11", "redis_id": "22"}                 # note: NO clickhouse handle exists
        rr = _adapter(
            [dep],
            databases=[{"id": "11", "flavor_id": "2", "network_id": net, "ha": False}],   # m1.small
            clickhouse=[{"id": "33", "flavor_id": "3", "engine": "clickhouse",
                         "network_id": net, "ha": False}],                                # m1.medium
            redis=[{"id": "22", "flavor_id": "1", "network_id": net, "ha": False}],       # m1.tiny
        ).run_rate(900, capture_date="2026-08-26")

        self.assertIsNotNone(rr)
        names = [c.name for c in rr.components]
        self.assertEqual(names, ["compute", "compute:postgres", "compute:clickhouse", "compute:redis"])
        self.assertIn("compute:clickhouse", names)              # the gap that motivated the rework
        # all-in = (app 0.0278 + pg 0.0174 + ch 0.0278 + redis 0.0118) GBP/hr x 1.27
        expected = round((0.0278 + 0.0174 + 0.0278 + 0.0118) * 1.27, 6)
        self.assertAlmostEqual(rr.all_in_hourly_usd, expected)
        ch = next(c for c in rr.components if c.name == "compute:clickhouse")
        self.assertAlmostEqual(ch.raw_unit_price, 0.0278)       # native GBP figure disclosed
        self.assertNotIn("excludes unpriced", rr.to_dict()["price_source"])  # nothing dropped, nothing faked

    def test_clickhouse_caught_by_network_even_with_no_referenced_ids(self):
        """ClickHouse is standalone: the deployment references nothing, yet the network_id it shares with
        the app VM attributes it. Proves the universal handle, not a hidden reference, does the work."""
        net = "net-analytics"
        dep = {"id": 901, "flavor_id": "3", "region": "uk-london", "network_id": net}
        rr = _adapter(
            [dep],
            clickhouse=[{"id": "77", "flavor_id": "4", "engine": "clickhouse", "network_id": net}],
        ).run_rate(901, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:clickhouse"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0486) * 1.27, 6))


class TestDisclosureNeverDrops(unittest.TestCase):
    def test_media_space_with_unpriceable_flavor_is_disclosed_by_type_and_id(self):
        """(b) A media_space whose VM flavor is not in list_flavors is DISCLOSED (type+id), never dropped
        and never faked as zero. The app is still priced (only the load-bearing app compute failing -> None)."""
        net = "net-wp"
        dep = {"id": 902, "flavor_id": "3", "region": "uk-london", "network_id": net,
               "media_space_id": "5"}
        rr = _adapter(
            [dep],
            media_spaces=[{"id": "5", "flavor_id": "does-not-exist", "network_id": net, "size_gb": 50}],
        ).run_rate(902, capture_date="2026-08-26")
        self.assertIsNotNone(rr)
        self.assertEqual([c.name for c in rr.components], ["compute"])
        price_source = rr.to_dict()["price_source"]
        self.assertIn("excludes unpriced:", price_source)       # disclosed, never a silent omission
        self.assertIn("media_space:5", price_source)            # disclosed by type + id

    def test_media_space_with_known_flavor_is_priced(self):
        """The other half: when list_flavors DOES carry the media_space VM flavor, it is priced (not
        hand-disclosed as the old code always did) - the flavor rule is universal."""
        net = "net-wp2"
        dep = {"id": 903, "flavor_id": "3", "region": "uk-london", "network_id": net,
               "media_space_id": "6"}
        rr = _adapter(
            [dep],
            media_spaces=[{"id": "6", "flavor_id": "2", "network_id": net, "size_gb": 50}],  # m1.small
        ).run_rate(903, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:media_space"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174) * 1.27, 6))
        self.assertNotIn("excludes unpriced", rr.to_dict()["price_source"])

    def test_dangling_reference_is_disclosed_not_silently_zero(self):
        """A referenced id that no enumerated row satisfies (a managed store deleted out from under the
        deployment) is disclosed by its neutral label, exactly as before the rework."""
        dep = {"id": 904, "flavor_id": "3", "region": "uk-london", "db_id": "999"}
        rr = _adapter([dep], databases=[], relational=[]).run_rate(904, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute"])
        self.assertIn("excludes unpriced: database", rr.to_dict()["price_source"])

    def test_future_store_type_is_one_data_row(self):
        """(b, extended) A store TYPE with no existing table row is picked up by adding ONE _REDU_STORES
        row - no code branch. Here a hypothetical managed 'qdrant' (list tool + structuredKey) is added to
        the table; a qdrant row on the deploy's network is then priced by flavor with zero other changes.
        This is the literal 'even a new type will just pick it up'."""
        net = "net-vec"
        dep = {"id": 905, "flavor_id": "3", "region": "uk-london", "network_id": net}
        future = redu_cost._StoreKind("list_qdrant", "qdrant", "qdrant", None)
        original = redu_cost._REDU_STORES
        redu_cost._REDU_STORES = original + (future,)
        try:
            rr = _adapter(
                [dep],
                extra={"list_qdrant": {"qdrant": [{"id": "1", "flavor_id": "2", "network_id": net}]}},
            ).run_rate(905, capture_date="2026-08-26")
        finally:
            redu_cost._REDU_STORES = original                   # restore the module table
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:qdrant"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174) * 1.27, 6))


class TestHaMemberCounting(unittest.TestCase):
    def test_ha_clickhouse_counts_members_as_instance_hours(self):
        """(c) HA member counting still works through the universal path: a 3-member HA ClickHouse is
        priced as 3 instance-hours (len(member_ips)), not one."""
        net = "net-ha"
        dep = {"id": 906, "flavor_id": "3", "region": "uk-london", "network_id": net}
        rr = _adapter(
            [dep],
            clickhouse=[{"id": "9", "flavor_id": "2", "engine": "clickhouse", "network_id": net,
                         "ha": True, "member_ips": ["10.0.0.1", "10.0.0.2", "10.0.0.3"]}],
        ).run_rate(906, capture_date="2026-08-26")
        ch = next(c for c in rr.components if c.name == "compute:clickhouse")
        self.assertEqual(ch.quantity, 3.0)                      # members disclosed as the quantity
        self.assertAlmostEqual(ch.hourly_usd, 0.0174 * 3 * 1.27)
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174 * 3) * 1.27, 6))

    def test_ha_without_member_ips_defaults_to_three(self):
        """An HA row that does not enumerate member_ips still counts as 3 (the cluster size)."""
        net = "net-ha2"
        dep = {"id": 907, "flavor_id": "3", "region": "uk-london", "network_id": net, "db_id": "3"}
        rr = _adapter(
            [dep],
            databases=[{"id": "3", "flavor_id": "2", "network_id": net, "ha": True, "member_ips": None}],
        ).run_rate(907, capture_date="2026-08-26")
        pg = next(c for c in rr.components if c.name == "compute:postgres")
        self.assertEqual(pg.quantity, 3.0)


class TestNetworkAttribution(unittest.TestCase):
    def test_network_id_includes_own_stores_and_excludes_another_deploys(self):
        """(d) Attribution by network_id includes THIS deploy's stores and excludes another deploy's:
        two ClickHouse rows exist account-wide, on two different networks; only the one on the deploy's
        network is priced. Proves the sweep does not bill a neighbour's resources."""
        mine, theirs = "net-mine", "net-theirs"
        dep = {"id": 908, "flavor_id": "3", "region": "uk-london", "network_id": mine}
        rr = _adapter(
            [dep],
            clickhouse=[
                {"id": "100", "flavor_id": "4", "engine": "clickhouse", "network_id": mine},   # mine
                {"id": "200", "flavor_id": "5", "engine": "clickhouse", "network_id": theirs},  # neighbour
            ],
            redis=[{"id": "300", "flavor_id": "1", "network_id": theirs}],                       # neighbour
        ).run_rate(908, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:clickhouse"])
        # only my m1.large ClickHouse (0.0486), never the neighbour's m1.xlarge (0.0764) or their redis
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0486) * 1.27, 6))

    def test_no_double_count_when_row_matches_both_network_and_reference(self):
        """A store attributed by BOTH its shared network_id AND a deployment reference is billed ONCE."""
        net = "net-dedup"
        dep = {"id": 909, "flavor_id": "3", "region": "uk-london", "network_id": net, "db_id": "42"}
        rr = _adapter(
            [dep],
            databases=[{"id": "42", "flavor_id": "2", "network_id": net, "ha": False}],
        ).run_rate(909, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:postgres"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0174) * 1.27, 6))

    def test_referenced_store_priced_without_network_id_on_the_row(self):
        """The fallback handle still works live: a Redis referenced by redis_id but whose list row carries
        NO network_id (today's real list_redis shape) is still attributed by the reference and priced."""
        dep = {"id": 910, "flavor_id": "3", "region": "uk-london", "redis_id": "8"}   # dep has no network_id
        rr = _adapter(
            [dep],
            redis=[{"id": "8", "flavor_id": "1"}],                                     # no network_id, no ha
        ).run_rate(910, capture_date="2026-08-26")
        self.assertEqual([c.name for c in rr.components], ["compute", "compute:redis"])
        self.assertAlmostEqual(rr.all_in_hourly_usd, round((0.0278 + 0.0118) * 1.27, 6))


if __name__ == "__main__":
    unittest.main()
