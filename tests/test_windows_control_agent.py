from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from life_os.db import connect, initialize
from life_os import windows_control_agent as agent


class WindowsControlAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.home=self.root/"home"
        self.repo=self.root/"repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.db=self.root/"life.db"
        self.c=connect(self.db)
        initialize(self.c)

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def test_file_boundary_rejects_outside_roots(self):
        outside=self.root/"outside.txt"
        outside.write_text("no",encoding="utf-8")
        with self.assertRaises(PermissionError):
            agent.execute("file.read",{"path":str(outside)},home=self.home,repo=self.repo)

    def test_sensitive_local_files_are_denied(self):
        secret=self.home/"credentials.json"
        secret.write_text('{"token":"secret"}',encoding="utf-8")
        with self.assertRaises(PermissionError):
            agent.execute("file.read",{"path":str(secret)},home=self.home,repo=self.repo)
        with self.assertRaises(PermissionError):
            agent.execute("file.write",{"path":str(self.home/"state.sqlite"),"text":"x","expected_sha256":None},home=self.home,repo=self.repo)

    def test_runtime_fingerprint_changes_with_source_bytes(self):
        first=agent._runtime_fingerprint()
        source=Path(agent.__file__)
        expected=hashlib.sha256(str(agent.AGENT_VERSION).encode()+b"\0"+source.read_bytes()).hexdigest()
        self.assertEqual(first,expected)

    def test_file_write_requires_compare_and_swap_for_existing_file(self):
        path=self.home/"control-artifacts"/"state.txt"
        first=agent.execute("file.write",{"path":str(path),"text":"one","expected_sha256":None},home=self.home,repo=self.repo)
        self.assertTrue(first["verified"])
        with self.assertRaises(PermissionError):
            agent.execute("file.write",{"path":str(path),"text":"two","expected_sha256":"wrong"},home=self.home,repo=self.repo)
        digest=hashlib.sha256(b"one").hexdigest()
        second=agent.execute("file.write",{"path":str(path),"text":"two","expected_sha256":digest},home=self.home,repo=self.repo)
        self.assertTrue(second["verified"])
        self.assertEqual(path.read_text(encoding="utf-8"),"two")

    def test_repo_source_is_readable_but_not_remotely_writable(self):
        source=self.repo/"module.py"
        source.write_text("print('safe')",encoding="utf-8")
        read=agent.execute("file.read",{"path":str(source)},home=self.home,repo=self.repo)
        self.assertIn("print",read["text"])
        with self.assertRaises(PermissionError):
            agent.execute(
                "file.write",
                {"path":str(source),"text":"print('changed')","expected_sha256":read["sha256"]},
                home=self.home,repo=self.repo,
            )

    def test_request_replay_does_not_execute_effect_twice(self):
        machine="windows-test"
        request={
            "request_id":"replay-1","schema_version":1,"capability":agent.CAPABILITY,
            "payload":{"operation":"system.snapshot","args":{},"policy_ref":"test"},
            "created_at":"2026-10-06T00:00:00+00:00","expires_at":"2099-01-01T00:00:00+00:00",
            "status":"claimed","claimed_by":machine,"claimed_at":"2026-10-06T00:00:01+00:00","revoked_at":None,
        }
        def fake_post(base,path,payload,token,timeout=4.0):
            if path=="/control/heartbeat": return {"heartbeat":{}}
            if path=="/control/claim": return {"request":request}
            if path=="/control/check": return {"request":request}
            if path=="/control/receipt": return {"receipt":{"request_id":"replay-1","outcome":"succeeded"}}
            raise AssertionError(path)
        with patch.object(agent,"_machine_id",return_value=machine), \
             patch.object(agent,"_post",side_effect=fake_post), \
             patch.object(agent,"execute",return_value={"verified":True}) as execute:
            first=agent.poll_once(self.c,home=self.home,repo=self.repo,endpoints=("https://live.example",),token="x"*32)
            second=agent.poll_once(self.c,home=self.home,repo=self.repo,endpoints=("https://live.example",),token="x"*32)
        self.assertEqual(first["outcome"],"succeeded")
        self.assertEqual(second["outcome"],"succeeded")
        execute.assert_called_once()

    def test_interrupted_admission_fails_closed_instead_of_reexecuting(self):
        payload={"operation":"process.start","args":{"target":"worker"},"policy_ref":"test"}
        state,cached=agent._reserve_or_replay(self.c,"crash-window",payload)
        self.assertEqual((state,cached),("execute",None))
        state,cached=agent._reserve_or_replay(self.c,"crash-window",payload)
        self.assertEqual(state,"terminal")
        self.assertEqual(cached["outcome"],"failed")
        self.assertTrue(cached["result"]["reconciliation_required"])

    def test_process_listing_does_not_transmit_command_lines(self):
        with patch.object(agent,"_ps_json",return_value=[]) as ps:
            result=agent.execute("process.list",{},home=self.home,repo=self.repo)
        self.assertEqual(result,{"processes":[],"truncated":False})
        script=ps.call_args.args[0]
        self.assertNotIn("CommandLine",script)
        self.assertIn("ProcessId",script)


    def test_process_start_rejects_unapproved_executable(self):
        with self.assertRaises(PermissionError):
            agent.execute("process.start",{"executable":"cmd.exe","args":["/c","whoami"]},home=self.home,repo=self.repo)

    def test_unknown_operation_fails_closed(self):
        with self.assertRaises(ValueError):
            agent.execute("shell.exec",{"command":"whoami"},home=self.home,repo=self.repo)

    def test_cli_exposes_windows_control_subcommand(self):
        from life_os.cli import parser
        parsed=parser().parse_args(["windows-control","--poll-seconds","7"])
        self.assertEqual(parsed.command,"windows-control")
        self.assertEqual(parsed.poll_seconds,7.0)

    def test_request_schema_is_exact_and_capability_scoped(self):
        machine="windows-test"
        valid={
            "request_id":"r1","schema_version":1,"capability":agent.CAPABILITY,
            "payload":{"operation":"system.snapshot","args":{},"policy_ref":"test"},
            "created_at":"2026-10-06T00:00:00+00:00","expires_at":"2099-01-01T00:00:00+00:00",
            "status":"claimed","claimed_by":machine,"claimed_at":"2026-10-06T00:00:01+00:00","revoked_at":None,
        }
        self.assertEqual(agent._validate_request(valid,machine)["request_id"],"r1")
        bad={**valid,"capability":"request.submit.v1"}
        with self.assertRaises(ValueError):
            agent._validate_request(bad,machine)

    def test_expired_request_is_rejected(self):
        machine="windows-test"
        expired={
            "request_id":"old","schema_version":1,"capability":agent.CAPABILITY,
            "payload":{"operation":"system.snapshot","args":{},"policy_ref":"test"},
            "created_at":"2020-01-01T00:00:00+00:00","expires_at":"2020-01-01T00:01:00+00:00",
            "status":"claimed","claimed_by":machine,"claimed_at":"2020-01-01T00:00:01+00:00","revoked_at":None,
        }
        with self.assertRaises(ValueError):
            agent._validate_request(expired,machine)

    def test_transport_fails_over_without_changing_control_contract(self):
        machine="windows-test"
        request={
            "request_id":"r1","schema_version":1,"capability":agent.CAPABILITY,
            "payload":{"operation":"system.snapshot","args":{},"policy_ref":"test"},
            "created_at":"2026-10-06T00:00:00+00:00","expires_at":"2099-01-01T00:00:00+00:00",
            "status":"claimed","claimed_by":machine,"claimed_at":"2026-10-06T00:00:01+00:00","revoked_at":None,
        }
        calls=[]
        def fake_post(base,path,payload,token,timeout=4.0):
            calls.append((base,path))
            if base=="https://dead.example":
                raise agent.ControlUnavailable("down")
            if path=="/control/heartbeat": return {"heartbeat":{}}
            if path=="/control/claim": return {"request":request}
            if path=="/control/check": return {"request":request}
            if path=="/control/receipt": return {"receipt":{"request_id":"r1","outcome":"succeeded"}}
            raise AssertionError(path)
        with patch.object(agent,"_machine_id",return_value=machine), patch.object(agent,"_post",side_effect=fake_post):
            result=agent.poll_once(
                self.c,home=self.home,repo=self.repo,
                endpoints=("https://dead.example","https://live.example"),token="x"*32,
            )
        self.assertTrue(result["reachable"])
        self.assertEqual(result["endpoint"],"https://live.example")
        self.assertEqual(result["outcome"],"succeeded")
        self.assertIn(("https://dead.example","/control/heartbeat"),calls)
        self.assertIn(("https://live.example","/control/receipt"),calls)

    def test_worker_recovery_bypasses_supervisor_mutex(self):
        class Child:
            pid=444
        with patch.object(agent.subprocess,"Popen",return_value=Child()) as popen:
            result=agent.execute("worker.recover",{},home=self.home,repo=self.repo)
        argv=popen.call_args.args[0]
        self.assertEqual(argv[:3],[agent.sys.executable,"-m","life_os"])
        self.assertIn("worker",argv)
        self.assertFalse(any("worker_launcher.ps1" in str(item) for item in argv))
        self.assertTrue(result["started"])

    def test_stale_worker_self_heals_without_remote_command(self):
        from life_os.queue import set_state
        set_state(self.c,"worker.heartbeat",'{"timestamp_epoch":1}')
        with patch.object(agent,"execute",return_value={"started":True,"pid":123}) as execute:
            result=agent._maybe_self_heal_worker(self.c,home=self.home,repo=self.repo,now=1000.0)
        self.assertTrue(result["attempted"])
        self.assertTrue(result["started"])
        execute.assert_called_once_with("worker.recover",{},home=self.home,repo=self.repo)

    def test_self_heal_respects_local_stop(self):
        from life_os.queue import set_state
        set_state(self.c,"worker.emergency_stop","1")
        with patch.object(agent,"execute") as execute:
            result=agent._maybe_self_heal_worker(self.c,home=self.home,repo=self.repo,now=1000.0)
        self.assertEqual(result,{"attempted":False,"reason":"locally_stopped"})
        execute.assert_not_called()

    def test_self_heal_is_rate_limited(self):
        from life_os.queue import set_state
        set_state(self.c,"worker.heartbeat",'{"timestamp_epoch":1}')
        set_state(self.c,agent.SELF_HEAL_KEY,"950")
        with patch.object(agent,"execute") as execute:
            result=agent._maybe_self_heal_worker(self.c,home=self.home,repo=self.repo,now=1000.0)
        self.assertEqual(result,{"attempted":False,"reason":"cooldown"})
        execute.assert_not_called()


if __name__=="__main__":
    unittest.main()
