"""Teardown deletes must NEVER cancel the creates they tear down.

A run that succeeds deprovisions everything it built, so its deletes sit in the same CloudTrail window
as its creates. Cancelling a create against any delete of the same KIND therefore zeroed every completed
run. Measured on aws-medium-b run21: three billable resources (2 Lightsail container services + 1
Lightsail relational database) created 19:38-19:40 and all deleted at 20:32, which the first cut of
``discover`` reported as ZERO. A $0 run-rate for a healthy run is the same silent-undercount defect this
module exists to remove.
"""
import unittest
from unittest import mock

from acspeed.adapters import aws_ct_cost as ct


def _event(name, ident, when, src="lightsail"):
    return {"EventName": name, "CloudTrailEvent": __import__("json").dumps({
        "eventTime": when, "eventSource": f"{src}.amazonaws.com",
        "requestParameters": {"serviceName": ident}, "responseElements": {}})}


class TestTeardownDoesNotCancelCreates(unittest.TestCase):

    def _discover(self, events):
        with mock.patch.object(ct, "_aws", side_effect=[({"Events": events}, None)]):
            return ct.discover("2026-09-05T18:00:00Z", "2026-09-05T21:00:00Z", "acs21",
                               ["us-east-1"])

    def test_run21_shape_reports_its_resources(self):
        events = [
            _event("CreateContainerService", "umami-acs21", "2026-09-05T19:38:25Z"),
            _event("CreateContainerService", "alcove-acs21", "2026-09-05T19:40:30Z"),
            _event("DeleteContainerService", "umami-acs21", "2026-09-05T20:32:17Z"),
            _event("DeleteContainerService", "alcove-acs21", "2026-09-05T20:32:17Z"),
        ]
        found, _unc = self._discover(events)
        self.assertEqual(len(found), 2,
                         "teardown deletes must not zero a healthy run's resources")

    def test_midrun_churn_is_still_netted_out(self):
        """A resource built and abandoned BEFORE the last create is not standing and must not be priced."""
        events = [
            _event("CreateContainerService", "throwaway-acs21", "2026-09-05T19:10:00Z"),
            _event("DeleteContainerService", "throwaway-acs21", "2026-09-05T19:20:00Z"),
            _event("CreateContainerService", "umami-acs21", "2026-09-05T19:38:25Z"),
            _event("DeleteContainerService", "umami-acs21", "2026-09-05T20:32:17Z"),
        ]
        found, _unc = self._discover(events)
        self.assertEqual(len(found), 1, "the abandoned service must net out, the surviving one must not")
        self.assertEqual(found[0]["identity"], "umami-acs21")


if __name__ == "__main__":
    unittest.main()
