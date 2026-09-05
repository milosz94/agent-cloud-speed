"""Test package.

Cache isolation is NOT set here. `unittest discover -s tests` imports test modules top-level and never
executes this file, so a guard placed here is silently absent under the command the project actually
runs. It lives in `acspeed/adapters/aws_price_index._cache_dir()` instead, where nothing has to opt in,
and `test_aws_catalog_id_2026_09_06.TestTheSuiteCannotCorruptTheRealPriceCache` fails if it stops
working.
"""
