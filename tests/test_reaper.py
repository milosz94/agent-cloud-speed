"""Universal reaper: enumerate-by-token, delete by a type map, retry until stable. Runner is injected."""
import json
import unittest

from acspeed import reaper


class FakeAWS:
    """A stateful fake AWS: holds resources, answers describes, honors deletes, and models one dependency
    (a security group cannot delete while a load balancer with the same token still exists)."""

    def __init__(self, tokened=True):
        t = "acsdead00" if tokened else "other"
        self.lbs = {f"umami-{t}-alb": f"arn:lb/umami-{t}-alb"}
        self.tgs = {f"umami-{t}-tg": f"arn:tg/umami-{t}-tg"}
        self.sgs = {f"umami-{t}-alb": "sg-111", f"umami-{t}-app": "sg-222"}
        self.rds = {f"umami-{t}-db"}
        self.logs = {f"/ecs/umami-{t}"}
        self.roles = {f"umami-{t}-exec", "umami-ecs-execution-role"}  # shared role has NO token
        self.ec2 = {"i-0ec2deadbeef0": f"app-{t}"}   # a VM-based deploy: instance id -> Name tag
        self.deleted = []

    def run(self, args, timeout=90):
        a = " ".join(args)
        if "describe-instances" in a:
            return 0, json.dumps([[iid, name] for iid, name in self.ec2.items()]), ""
        if "terminate-instances" in a:
            iid = args[args.index("--instance-ids") + 1]
            self.ec2.pop(iid, None)
            self.deleted.append(iid)
            return 0, json.dumps({"Return": True}), ""
        if "describe-load-balancers" in a:
            return 0, json.dumps([[n, arn] for n, arn in self.lbs.items()]), ""
        if "describe-target-groups" in a:
            return 0, json.dumps([[n, arn] for n, arn in self.tgs.items()]), ""
        if "describe-security-groups" in a:
            return 0, json.dumps([[n, gid] for n, gid in self.sgs.items()]), ""
        if "describe-db-instances" in a:
            return 0, json.dumps(sorted(self.rds)), ""
        if "describe-log-groups" in a:
            return 0, json.dumps(sorted(self.logs)), ""
        if "list-roles" in a:
            return 0, json.dumps(sorted(self.roles)), ""
        if "list-clusters" in a:
            return 0, json.dumps([]), ""
        if "list-attached-role-policies" in a or "list-role-policies" in a:
            return 0, json.dumps([]), ""
        # deletes
        if "delete-load-balancer" in a:
            arn = args[args.index("--load-balancer-arn") + 1]
            self.lbs = {k: v for k, v in self.lbs.items() if v != arn}
            self.deleted.append(arn)
            return 0, "", ""
        if "delete-target-group" in a:
            arn = args[args.index("--target-group-arn") + 1]
            self.tgs = {k: v for k, v in self.tgs.items() if v != arn}
            self.deleted.append(arn)
            return 0, "", ""
        if "delete-security-group" in a:
            gid = args[args.index("--group-id") + 1]
            # DEPENDENCY: the alb SG (sg-111) cannot delete while the app SG (sg-222) still references it,
            # so it FAILS on pass 1 and only succeeds a later pass -> genuinely exercises retry-until-stable.
            if gid == "sg-111" and "sg-222" in self.sgs.values():
                return 1, "", "DependencyViolation: an SG still references this group"
            self.sgs = {k: v for k, v in self.sgs.items() if v != gid}
            self.deleted.append(gid)
            return 0, "", ""
        if "delete-db-instance" in a:
            ident = args[args.index("--db-instance-identifier") + 1]
            self.rds.discard(ident)
            self.deleted.append(ident)
            return 0, "", ""
        if "delete-log-group" in a:
            name = args[args.index("--log-group-name") + 1]
            self.logs.discard(name)
            self.deleted.append(name)
            return 0, "", ""
        if "delete-role" in a:
            name = args[args.index("--role-name") + 1]
            self.roles.discard(name)
            self.deleted.append(name)
            return 0, "", ""
        return 0, "[]", ""


class FakeGCP:
    def __init__(self):
        self.services = [{"metadata": {"name": "umami-acsdead00"}, "status": {"url": "https://umami-acsdead00-uc.a.run.app"}}]
        self.sql = [{"name": "umami-acsdead00-db"}]
        self.repos = [{"name": "projects/p/locations/us-central1/repositories/umami-acsdead00"},
                      {"name": "projects/p/locations/us-central1/repositories/shared-base"}]  # no token
        self.deleted = []

    def run(self, args, timeout=90):
        a = " ".join(args)
        if "run services list" in a:
            return 0, json.dumps(self.services), ""
        if "sql instances list" in a:
            return 0, json.dumps(self.sql), ""
        if "artifacts repositories list" in a:
            return 0, json.dumps(self.repos), ""
        if "run services delete" in a:
            self.services = [s for s in self.services if s["metadata"]["name"] != args[args.index("delete") + 1]]
            self.deleted.append(args[args.index("delete") + 1])
            return 0, "", ""
        if "sql instances delete" in a:
            self.sql = [s for s in self.sql if s["name"] != args[args.index("delete") + 1]]
            self.deleted.append(args[args.index("delete") + 1])
            return 0, "", ""
        if "artifacts repositories delete" in a:
            self.deleted.append(args[args.index("delete") + 1])
            self.repos = [r for r in self.repos if not r["name"].endswith(args[args.index("delete") + 1])]
            return 0, "", ""
        return 0, "[]", ""


class TestReaper(unittest.TestCase):
    def test_token_required(self):
        r = reaper.reap_universal("aws", "", dry_run=True)
        self.assertFalse(r["checked"])
        self.assertEqual(r["planned"], [])

    def test_gcp_dry_run_plans_all_families_scopes_by_token(self):
        g = FakeGCP()
        r = reaper.reap_universal("gcp", "acsdead00", dry_run=True, run=g.run)
        kinds = {p.split(":")[0] for p in r["planned"]}
        self.assertEqual(kinds, {"gcp/run-service", "gcp/sql-instance", "gcp/artifact-repo"})
        self.assertTrue(r["dry_run"])
        self.assertEqual(g.deleted, [])  # dry run deletes NOTHING
        # the shared repo without the token is NOT planned
        self.assertNotIn("gcp/artifact-repo:shared-base", r["planned"])

    def test_gcp_real_delete(self):
        g = FakeGCP()
        r = reaper.reap_universal("gcp", "acsdead00", dry_run=False, run=g.run)
        self.assertFalse(r["dry_run"])
        self.assertEqual(len(r["deleted"]), 3)
        self.assertEqual(r["failed"], [])
        self.assertIn("umami-acsdead00", g.deleted)
        self.assertIn("umami-acsdead00-db", g.deleted)

    def test_aws_retry_until_stable(self):
        f = FakeAWS()
        r = reaper.reap_universal("aws", "acsdead00", dry_run=False, run=f.run)
        # the alb-named security group blocks on pass 1 (lb alive), deletes after the lb is gone
        self.assertGreaterEqual(r["passes"], 2)
        self.assertEqual(r["failed"], [], f"unexpected survivors: {r['failed']}")
        self.assertIn("sg-111", f.deleted)   # the blocked SG did eventually delete
        self.assertIn("arn:lb/umami-acsdead00-alb", f.deleted)

    def test_aws_never_touches_shared_role(self):
        f = FakeAWS()
        reaper.reap_universal("aws", "acsdead00", dry_run=False, run=f.run)
        self.assertIn("umami-acsdead00-exec", f.deleted)      # token-named role deleted
        self.assertNotIn("umami-ecs-execution-role", f.deleted)  # shared role LEFT ALONE

    def test_aws_dry_run_finds_orphans_deletes_nothing(self):
        f = FakeAWS()
        r = reaper.reap_universal("aws", "acsdead00", dry_run=True, run=f.run)
        self.assertEqual(f.deleted, [])
        kinds = {p.split(":")[0] for p in r["planned"]}
        self.assertIn("aws/security-group", kinds)
        self.assertIn("aws/rds", kinds)
        self.assertIn("aws/iam-role", kinds)
        self.assertIn("aws/log-group", kinds)

    def test_aws_reaps_ec2_instance(self):
        # the gap that leaked the Medium-B aws VM: the reaper handled only ECS/Fargate, not EC2.
        f = FakeAWS()
        r = reaper.reap_universal("aws", "acsdead00", dry_run=False, run=f.run)
        self.assertIn("i-0ec2deadbeef0", f.deleted)                       # the VM instance is terminated
        self.assertTrue(any("aws/ec2-instance" in p for p in r["planned"]))
        self.assertEqual(r["failed"], [])

    def test_aws_ec2_needs_the_name_tag_token(self):
        # an instance whose Name tag does NOT carry the token is left alone (never a foreign/other-run VM)
        f = FakeAWS()
        f.ec2 = {"i-0foreign": "someone-elses-box"}
        reaper.reap_universal("aws", "acsdead00", dry_run=False, run=f.run)
        self.assertNotIn("i-0foreign", f.deleted)

    def test_azure_collapses_to_resource_groups(self):
        rows = [{"name": "umami-acsdead00", "resourceGroup": "rg-umami-acsdead00"},
                {"name": "db-acsdead00", "resourceGroup": "rg-umami-acsdead00"}]

        def run(args, timeout=90):
            a = " ".join(args)
            if "resource list" in a:
                return 0, json.dumps(rows), ""
            if "group list" in a:
                return 0, json.dumps(["rg-umami-acsdead00", "rg-other"]), ""
            if "group delete" in a:
                return 0, "", ""
            return 0, "[]", ""

        r = reaper.reap_universal("azure", "acsdead00", dry_run=True, run=run)
        # two resources in one group collapse to ONE group delete; rg-other (no token) excluded
        self.assertEqual(r["planned"], ["azure/resource-group:rg-umami-acsdead00"])

    def test_unmapped_kind_disclosed_not_skipped(self):
        # inject an enumerate (via the dispatch dict enum() uses) that returns a kind with no delete verb
        orig = reaper._ENUMERATE["gcp"]
        reaper._ENUMERATE["gcp"] = lambda tok, run: [reaper.Res(kind="gcp/unknown-thing", name="x-acsdead00")]
        try:
            r = reaper.reap_universal("gcp", "acsdead00", dry_run=True, run=lambda *a, **k: (0, "[]", ""))
            self.assertTrue(any("no delete mapping" in f["reason"] for f in r["failed"]),
                            f"expected a disclosed unmapped kind, got failed={r['failed']}")
        finally:
            reaper._ENUMERATE["gcp"] = orig


if __name__ == "__main__":
    unittest.main()
