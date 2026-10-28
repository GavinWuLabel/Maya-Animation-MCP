"""Offline queue tests: no Maya initialization, sockets, or user scene access."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maya_side.standalone_service import MainThreadQueue
from maya_side import maya_bridge as bridge

class StandaloneQueueTests(unittest.TestCase):
    def test_submission_from_worker_executes_on_owner(self):
        q=MainThreadQueue();observed=[]
        t=threading.Thread(target=lambda:q.submit(lambda:observed.append(threading.get_ident())))
        t.start();t.join(1);self.assertEqual(observed,[])
        self.assertTrue(q.pump_once(timeout=0));self.assertEqual(observed,[threading.get_ident()])
        self.assertFalse(q.pump_once(timeout=0))
    def test_nonowner_cannot_pump(self):
        q=MainThreadQueue();errors=[]
        def run():
            try:q.pump_once(timeout=0)
            except RuntimeError as exc:errors.append(str(exc))
        t=threading.Thread(target=run);t.start();t.join(1)
        self.assertEqual(len(errors),1)
    def test_closed_queue_rejects_new_jobs(self):
        q=MainThreadQueue();q.close()
        with self.assertRaisesRegex(RuntimeError,'stopped'):q.submit(lambda:None)
    def test_cancelled_queued_request_does_not_execute_late(self):
        q=MainThreadQueue()
        with patch.object(bridge,'_defer',q.submit),patch.object(bridge,'REQUEST_TIMEOUT',.01),patch.object(bridge,'_execute_method') as execute:
            with self.assertRaises(bridge.BridgeError) as cm:bridge._dispatch('execute_python',{})
            self.assertEqual(cm.exception.code,'TIMEOUT_CANCELLED');q.pump_once(timeout=0);execute.assert_not_called()
    def test_restarted_session_invalidates_queued_job(self):
        q=MainThreadQueue();enqueued=threading.Event();errors=[]
        def defer(fn):q.submit(fn);enqueued.set()
        def request():
            try:bridge._dispatch('execute_python',{},bridge._session_generation-1)
            except bridge.BridgeError as exc:errors.append(exc.code)
        with patch.object(bridge,'_defer',defer),patch.object(bridge,'REQUEST_TIMEOUT',1),patch.object(bridge,'_execute_method') as execute:
            t=threading.Thread(target=request);t.start();self.assertTrue(enqueued.wait(1));q.pump_once(timeout=0);t.join(2)
            self.assertEqual(errors,['SESSION_EXPIRED']);execute.assert_not_called()
    def test_dispatch_result_returned_from_main_thread(self):
        q=MainThreadQueue();enqueued=threading.Event();results=[]
        def defer(fn):q.submit(fn);enqueued.set()
        with patch.object(bridge,'_defer',defer),patch.object(bridge,'REQUEST_TIMEOUT',1),patch.object(bridge,'_execute_method',side_effect=lambda *a:{'thread':threading.get_ident()}):
            t=threading.Thread(target=lambda:results.append(bridge._dispatch('get_status',{})));t.start();self.assertTrue(enqueued.wait(1));q.pump_once(timeout=0);t.join(2)
        self.assertEqual(results,[{'thread':threading.get_ident()}])

if __name__=='__main__':unittest.main(verbosity=2)
