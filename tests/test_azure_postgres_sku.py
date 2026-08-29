"""Azure Postgres Flexible Server cost: the SKU-to-meter mapping (both families) and run-token scoping.

Regression for run01 (a priced B-series Container Apps deploy that returned UNPRICED). Two defects, both
here:
  1. the pricer did ``meter_contains = sku.split('_')[-1]``, turning 'Standard_D2ds_v5' into 'v5' (matches
     no meter). Azure prices the two flexible-server families with DIFFERENT meter shapes: Burstable by a
     per-SKU meter, but General Purpose / Memory Optimized by a GENERIC 'vCore' meter whose skuName is only
     the vCore COUNT ('2 vCore'), so D2ds_v5 and E2ds_v5 collide on skuName and are separated ONLY by
     armSkuName. The fix matches GP/MO by exact armSkuName, Burstable by an exact size-token match.
  2. the resolver ran a SUBSCRIPTION-WIDE ``az postgres flexible-server list``, so a single unpriceable
     foreign server failed the whole run's cost (and its price was wrongly folded into this run's floor).
     The fix scopes enumeration to the run token.

Offline: canned retail rows (real shape, measured 2026-08-29) and a canned ``run_cmd``. No network, no cloud,
no em-dash characters (repo test bans them)."""
import unittest

from acspeed.adapters import azure_cost as az


# Real-shaped 'Azure Database for PostgreSQL' Flexible Server rows (eastus2, measured 2026-08-29):
#  - Burstable -> a PER-SKU meter (skuName / meterName is the size itself)
#  - GP / MO   -> a GENERIC 'vCore' meter; skuName is only the vCore COUNT, so D2ds_v5 and E2ds_v5 COLLIDE
#                 on skuName '2 vCore' and are separated ONLY by armSkuName.
_ROWS = {"Items": [
    {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "B1MS",
     "skuName": "B1MS", "armSkuName": "AzureDB_PostgreSQL_Burstable", "unitOfMeasure": "1 Hour",
     "retailPrice": 0.017, "type": "Consumption"},
    {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "B2ms vCore",
     "skuName": "B2ms", "armSkuName": "AzureDB_PostgreSQL_Burstable", "unitOfMeasure": "1 Hour",
     "retailPrice": 0.136, "type": "Consumption"},
    {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "vCore",
     "skuName": "2 vCore", "armSkuName": "Standard_D2ds_v5", "unitOfMeasure": "1 Hour",
     "retailPrice": 0.178, "type": "Consumption"},
    {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "vCore",
     "skuName": "2 vCore", "armSkuName": "Standard_E2ds_v5", "unitOfMeasure": "1 Hour",
     "retailPrice": 0.25, "type": "Consumption"},        # SAME skuName, different price: the collision
    {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "Compute - Free vCore",
     "skuName": "Compute - Free", "armSkuName": "Compute-Free", "unitOfMeasure": "1 Hour",
     "retailPrice": 0.0, "type": "Consumption"},          # the free row must never be picked
], "NextPageLink": None}


def _fetch(url):
    return _ROWS


class TestPostgresSkuPricing(unittest.TestCase):
    def test_burstable_prices_by_per_sku_meter(self):
        self.assertAlmostEqual(az.postgres_flexible_hourly("eastus2", "Standard_B1ms", 0, fetch=_fetch), 0.017)
        self.assertAlmostEqual(az.postgres_flexible_hourly("eastus2", "Standard_B2ms", 0, fetch=_fetch), 0.136)

    def test_gp_mo_prices_by_exact_armskuname(self):
        # the OLD split('_')[-1] gave 'v5' and matched no meter; the fix matches armSkuName exactly and,
        # for a colliding skuName ('2 vCore'), picks the RIGHT generation's price.
        self.assertAlmostEqual(az.postgres_flexible_hourly("eastus2", "Standard_D2ds_v5", 0, fetch=_fetch), 0.178)
        self.assertAlmostEqual(az.postgres_flexible_hourly("eastus2", "Standard_E2ds_v5", 0, fetch=_fetch), 0.25)

    def test_burstable_size_match_is_exact_not_substring(self):
        # 'B1ms' must NOT match a 'B16ms' meter (the substring trap the old contains-match risked).
        rows = {"Items": [
            {"productName": "Azure Database for PostgreSQL Flexible Server", "meterName": "B16ms vCore",
             "skuName": "B16ms", "armSkuName": "x", "unitOfMeasure": "1 Hour",
             "retailPrice": 1.088, "type": "Consumption"},
        ], "NextPageLink": None}
        self.assertIsNone(az.postgres_flexible_hourly("eastus2", "Standard_B1ms", 0, fetch=lambda u: rows))

    def test_unpriceable_sku_returns_none_not_a_guess(self):
        self.assertIsNone(az.postgres_flexible_hourly("eastus2", "Standard_ZZ9_v9", 0, fetch=_fetch))


class TestPostgresResolverScoping(unittest.TestCase):
    def _servers(self):
        # 'location' is the DISPLAY name ('East US 2'), exactly as `az postgres flexible-server list` returns
        # it -- the resolver must normalize it to the ARM short name 'eastus2' or the Retail query returns 0
        # rows and the whole cost goes UNPRICED (the third run01 defect).
        return [
            {"name": "pg-acs5db818d0", "resourceGroup": "rg-acs5db818d0",
             "id": "/subs/x/resourceGroups/rg-acs5db818d0/providers/.../pg-acs5db818d0",
             "location": "East US 2", "sku": {"name": "Standard_B1ms"}, "storage": {"storageSizeGb": 32}},
            {"name": "pg-foreign", "resourceGroup": "rg-someone-else",
             "id": "/subs/x/resourceGroups/rg-someone-else/providers/.../pg-foreign",
             "location": "West Europe", "sku": {"name": "Standard_D64ds_v5"}, "storage": {"storageSizeGb": 512}},
        ]

    def test_token_scoping_keeps_only_this_runs_db(self):
        out = az.azure_postgres_extras("acs5db818d0", lambda args: self._servers())
        self.assertEqual([s["sku"] for s in out], ["Standard_B1ms"])   # the foreign D64 server is excluded
        self.assertEqual(out[0]["storage_gb"], 32)
        self.assertEqual(out[0]["region"], "eastus2")                  # display name normalized to ARM name

    def test_empty_token_returns_all(self):
        self.assertEqual(len(az.azure_postgres_extras("", lambda args: self._servers())), 2)

    def test_arm_region_normalizes_display_names(self):
        self.assertEqual(az._arm_region("East US 2"), "eastus2")
        self.assertEqual(az._arm_region("West Europe"), "westeurope")
        self.assertEqual(az._arm_region("eastus2"), "eastus2")         # idempotent on already-ARM names


if __name__ == "__main__":
    unittest.main()
