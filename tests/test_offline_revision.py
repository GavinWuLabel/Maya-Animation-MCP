"""Offline fault-injection tests. No real sockets, Maya imports or user scenes."""
import asyncio
import io
import json
import socket
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from maya_side import maya_bridge as bridge
from maya_animation_mcp import bridge_client as client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + b"\x00\x00\x00\x01" * 2

class FakeSocket:
    def __init__(self, raw=b'', send_error=None):
        self.raw, self.send_error = raw, send_error
        self.sent = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def settimeout(self, value): self.timeout = value
    def sendall(self, payload):
        self.sent.append(payload)
        if self.send_error: raise self.send_error
    def makefile(self, mode): return io.BytesIO(self.raw)

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'connection.json'
        self.info = {'host':'127.0.0.1','protocol':1,'port':12345,'token':'offline-token'}
        self.path.write_text(json.dumps(self.info))
        self.cp = patch.object(client, 'connection_path', return_value=self.path)
        self.cp.start(); self.addCleanup(self.cp.stop)
        self.up = patch.object(client.uuid, 'uuid4', return_value=types.SimpleNamespace(hex='test-id'))
        self.up.start(); self.addCleanup(self.up.stop)
    def run_response(self, response):
        raw = response if isinstance(response, bytes) else json.dumps(response).encode()+b'\n'
        sock=FakeSocket(raw)
        with patch.object(client.socket,'create_connection',return_value=sock) as connect:
            try: return client.call('get_status')
            finally: self.assertEqual(connect.call_count,1)
    def test_valid_response_and_escaped_newline(self):
        sock=FakeSocket(b'{"id":"test-id","ok":true,"result":{"ready":true}}\n')
        with patch.object(client.socket,'create_connection',return_value=sock):
            self.assertEqual(client.call('execute_python',{'code':'a\nb'}),{'ready':True})
        self.assertEqual(sock.sent[0].count(b'\n'),1)
        self.assertEqual(json.loads(sock.sent[0])['params']['code'],'a\nb')
    def test_missing_connection(self):
        self.path.unlink()
        with patch.object(client.socket,'create_connection') as connect:
            with self.assertRaisesRegex(client.BridgeError,'CONNECTION_NOT_READY'):client.call('get_status')
            connect.assert_not_called()
    def test_invalid_metadata_never_connects(self):
        for info in [[],None,{},dict(self.info,port=True),dict(self.info,port=0),dict(self.info,port='12'),dict(self.info,token=None),dict(self.info,protocol=True),dict(self.info,host='8.8.8.8')]:
            with self.subTest(info=info),patch.object(client.socket,'create_connection') as connect:
                self.path.write_text(json.dumps(info))
                with self.assertRaisesRegex(client.BridgeError,'INVALID_CONNECTION'):client.call('get_status')
                connect.assert_not_called()
    def test_connection_refused_is_known_unsent(self):
        with patch.object(client.socket,'create_connection',side_effect=ConnectionRefusedError) as connect:
            with self.assertRaisesRegex(client.BridgeError,'CONNECT_FAILED'):client.call('set_keyframes')
            self.assertEqual(connect.call_count,1)
    def test_send_timeout_is_unknown_never_retried(self):
        sock=FakeSocket(send_error=socket.timeout())
        with patch.object(client.socket,'create_connection',return_value=sock) as connect:
            with self.assertRaisesRegex(client.BridgeError,'EXECUTION_UNKNOWN'):client.call('set_keyframes')
            self.assertEqual(connect.call_count,1);self.assertEqual(len(sock.sent),1)
    def test_bad_response_shapes(self):
        for response in [[],None,{'id':'test-id','ok':1},{'id':'other','ok':True,'result':{}},{'id':'test-id','ok':True},{'id':'test-id','ok':False,'error':[]},{'id':'test-id','ok':False,'error':{'code':1,'message':'bad'}}]:
            with self.subTest(response=response),self.assertRaisesRegex(client.BridgeError,'INVALID_RESPONSE'):self.run_response(response)
    def test_bad_wire_responses(self):
        for raw in [b'',b'{}',b'invalid\n',b'\xff\n',b'{"ok":true,"id":"test-id","result":{"x":NaN}}\n']:
            with self.subTest(raw=raw),self.assertRaisesRegex(client.BridgeError,'INVALID_RESPONSE'):self.run_response(raw)
    def test_bridge_error_code_survives(self):
        with self.assertRaisesRegex(client.BridgeError,'TIMEOUT_CANCELLED: pending'):
            self.run_response({'id':'test-id','ok':False,'error':{'code':'TIMEOUT_CANCELLED','message':'pending'}})
    def test_nonfinite_request_never_sent(self):
        with patch.object(client.socket,'create_connection') as connect:
            with self.assertRaisesRegex(client.BridgeError,'INVALID_ARGUMENT'):client.call('set_current_time',{'frame':float('nan')})
            connect.assert_not_called()
    def test_response_size_limit(self):
        with patch.object(client,'MAX_BYTES',64),patch.object(client.socket,'create_connection') as connect:
            with self.assertRaisesRegex(client.BridgeError,'Request exceeds'):client.call('get_status')
            connect.assert_not_called()
        with patch.object(client,'MAX_BYTES',300),self.assertRaisesRegex(client.BridgeError,'INVALID_RESPONSE'):
            self.run_response(b' ' * 301+b'\n')

class UndoCmds:
    def __init__(self,state=True,open_fail=False,close_fail=False):
        self.state=state;self.open_fail=open_fail;self.close_fail=close_fail;self.top='user_previous';self.active=None;self.changed=False;self.undos=0
    def undoInfo(self,**kw):
        if kw.get('query'):return self.state if kw.get('state') else self.top
        if 'stateWithoutFlush' in kw:self.state=kw['stateWithoutFlush'];return
        if kw.get('openChunk'):
            if self.open_fail:raise RuntimeError('open failed')
            self.active=kw['chunkName'];self.changed=False
        if kw.get('closeChunk'):
            if self.close_fail:raise RuntimeError('close failed')
            if self.changed:self.top=self.active
            self.active=None
    def undo(self):self.undos+=1;self.top='user_previous'
    def mutate(self):self.changed=True

class TransactionTests(unittest.TestCase):
    def test_empty_failure_preserves_user_undo(self):
        c=UndoCmds()
        with patch.object(bridge,'_cmds',return_value=c),self.assertRaises(RuntimeError):
            with bridge._undo_chunk('test'):raise RuntimeError('early')
        self.assertEqual(c.undos,0);self.assertEqual(c.top,'user_previous')
    def test_partial_failure_rolls_back_own_chunk(self):
        c=UndoCmds(state=False)
        with patch.object(bridge,'_cmds',return_value=c),self.assertRaises(RuntimeError):
            with bridge._undo_chunk('test'):c.mutate();raise RuntimeError('partial')
        self.assertEqual(c.undos,1);self.assertFalse(c.state)
    def test_open_failure_restores_disabled_undo(self):
        c=UndoCmds(state=False,open_fail=True)
        with patch.object(bridge,'_cmds',return_value=c),self.assertRaisesRegex(RuntimeError,'open failed'):
            with bridge._undo_chunk('test'):self.fail('body must not run')
        self.assertFalse(c.state);self.assertEqual(c.undos,0)
    def test_close_failure_restores_disabled_undo(self):
        c=UndoCmds(state=False,close_fail=True)
        with patch.object(bridge,'_cmds',return_value=c),self.assertRaisesRegex(RuntimeError,'close failed'):
            with bridge._undo_chunk('test'):c.mutate()
        self.assertFalse(c.state)

class FileCmds:
    def __init__(self,folder,count=3,batch=False):
        self.folder=Path(folder);self.count=count;self.batch=batch;self.time=7;self.selected=['|control'];self.modified=True;self.scene='original.mb';self.invalid=False;self.fail=False;self.saved_flags={};self.restore_time_fail=False
    def about(self,**kw):return self.batch
    def currentTime(self,value=None,**kw):
        if kw.get('query'):return self.time
        if self.restore_time_fail and value==7:raise RuntimeError('restore time failed')
        self.time=value
    def ls(self,**kw):return self.selected[:]
    def select(self,items=None,**kw):self.selected=[] if kw.get('clear') else items[:]
    def playblast(self,**kw):
        self.time=99;self.selected=[]
        if self.fail:raise RuntimeError('viewport failed')
        if 'completeFilename' in kw:Path(kw['completeFilename']).write_bytes(b'' if self.invalid else PNG)
        else:
            for i in range(self.count):Path(kw['filename']+'.%04d.png'%(i+1)).write_bytes(b'' if self.invalid else PNG)
        return kw.get('filename',kw.get('completeFilename'))
    def file(self,path=None,**kw):
        if kw.get('query'):return self.scene if kw.get('sceneName') else self.modified
        if 'modified' in kw:self.modified=kw['modified'];return
        self.saved_flags=kw;self.modified=False
        if self.fail:raise RuntimeError('export failed')
        Path(path).write_bytes(b'' if self.invalid else b'//Maya ASCII scene')

class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.c=FileCmds(self.root)
        for p in [patch.object(bridge,'_cmds',return_value=self.c),patch.object(bridge,'_output_root',str(self.root))]:p.start();self.addCleanup(p.stop)
    def test_scene_copy_preserves_name_dirty_and_references(self):
        r=bridge._save_scene_copy({'path':str(self.root/'copy.ma')})
        self.assertEqual(r['original_scene'],'original.mb');self.assertTrue(self.c.modified);self.assertEqual(self.c.scene,'original.mb');self.assertTrue(self.c.saved_flags['preserveReferences']);self.assertTrue(self.c.saved_flags['exportAll'])
    def test_failed_save_restores_dirty(self):
        self.c.fail=True
        with self.assertRaisesRegex(RuntimeError,'export failed'):bridge._save_scene_copy({'path':str(self.root/'copy.ma')})
        self.assertTrue(self.c.modified)
    def test_empty_save_is_failure(self):
        self.c.invalid=True
        with self.assertRaises(bridge.BridgeError) as cm:bridge._save_scene_copy({'path':str(self.root/'copy.ma')})
        self.assertEqual(cm.exception.code,'SAVE_FAILED');self.assertTrue(self.c.modified)
    def test_output_escape_and_overwrite_rejected(self):
        q=self.root/'exists.ma';q.write_text('keep')
        for path in [q,self.root/'..'/'escape.ma']:
            with self.subTest(path=path),self.assertRaises(bridge.BridgeError):bridge._save_scene_copy({'path':str(path)})
        self.assertEqual(q.read_text(),'keep')
    def test_single_capture_restores_time_and_selection(self):
        r=bridge._playblast({'path':str(self.root/'frame.png'),'frame':12},single=True)
        self.assertEqual(r['frame'],12);self.assertEqual(self.c.time,7);self.assertEqual(self.c.selected,['|control'])
    def test_blank_capture_is_not_success(self):
        self.c.invalid=True
        with self.assertRaises(bridge.BridgeError) as cm:bridge._playblast({'path':str(self.root/'frame.png')},single=True)
        self.assertEqual(cm.exception.code,'CAPTURE_FAILED');self.assertEqual(self.c.time,7)
    def test_complete_sequence_counts_frames(self):
        r=bridge._playblast({'path':str(self.root/'seq'),'start':1,'end':3})
        self.assertEqual(r['frame_count'],3);self.assertEqual(len(r['files']),3)
    def test_partial_sequence_is_explicit_failure(self):
        self.c.count=1
        with self.assertRaises(bridge.BridgeError) as cm:bridge._playblast({'path':str(self.root/'seq'),'start':1,'end':3})
        self.assertEqual(cm.exception.code,'CAPTURE_INCOMPLETE');self.assertEqual(len(list(self.root.glob('*.png'))),1);self.assertEqual(self.c.selected,['|control'])
    def test_capture_error_restores_state(self):
        self.c.fail=True
        with self.assertRaisesRegex(RuntimeError,'viewport failed'):bridge._playblast({'path':str(self.root/'frame.png')},single=True)
        self.assertEqual(self.c.time,7);self.assertEqual(self.c.selected,['|control'])
    def test_selection_restored_if_time_restore_fails(self):
        self.c.restore_time_fail=True
        with self.assertRaisesRegex(RuntimeError,'restore time failed'):bridge._playblast({'path':str(self.root/'frame.png')},single=True)
        self.assertEqual(self.c.selected,['|control'])
    def test_batch_rejected_without_output(self):
        self.c.batch=True
        with self.assertRaises(bridge.BridgeError) as cm:bridge._playblast({'path':str(self.root/'frame.png')},single=True)
        self.assertEqual(cm.exception.code,'GUI_REQUIRED');self.assertEqual(list(self.root.iterdir()),[])

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.queue=[];self.utils=types.ModuleType('maya.utils');self.utils.executeDeferred=self.queue.append
        maya=types.ModuleType('maya');maya.utils=self.utils
        for p in [patch.dict(sys.modules,{'maya':maya,'maya.utils':self.utils}),patch.object(bridge,'REQUEST_TIMEOUT',.01)]:p.start();self.addCleanup(p.stop)
    def test_pending_timeout_prevents_late_execution(self):
        with patch.object(bridge,'_execute_method') as call:
            with self.assertRaises(bridge.BridgeError) as cm:bridge._dispatch('set_keyframes',{})
            self.assertEqual(cm.exception.code,'TIMEOUT_CANCELLED');self.queue.pop()();call.assert_not_called()
    def test_session_expiry_prevents_execution(self):
        self.utils.executeDeferred=lambda fn:fn()
        with patch.object(bridge,'_execute_method') as call:
            with self.assertRaises(bridge.BridgeError) as cm:bridge._dispatch('set_keyframes',{},bridge._session_generation-1)
            self.assertEqual(cm.exception.code,'SESSION_EXPIRED');call.assert_not_called()
    def test_running_timeout_is_unknown_no_retry(self):
        started=threading.Event();release=threading.Event();threads=[]
        def execute(*a):started.set();release.wait(1);return {'done':True}
        def defer(fn):
            t=threading.Thread(target=fn);threads.append(t);t.start();started.wait(1)
        self.utils.executeDeferred=defer
        try:
            with patch.object(bridge,'_execute_method',side_effect=execute) as call:
                with self.assertRaises(bridge.BridgeError) as cm:bridge._dispatch('set_keyframes',{})
                self.assertEqual(cm.exception.code,'EXECUTION_UNKNOWN');self.assertEqual(call.call_count,1)
        finally:
            release.set()
            for t in threads:t.join(2)

class KeyCmds(UndoCmds):
    def __init__(self):super().__init__();self.time=9;self.key_count=0;self.layers=[];self.fail_key=0;self.members=[]
    def currentTime(self,value=None,**kw):
        if kw.get('query'):return self.time
        self.time=value
    def ls(self,**kw):return self.layers
    def objExists(self,name):return name in self.layers
    def nodeType(self,name):return 'animLayer'
    def getAttr(self,plug):return False
    def referenceQuery(self,*a,**kw):return False
    def animLayer(self,name=None,**kw):
        if kw.get('root'):return 'BaseAnimation'
        if kw.get('query'):return self.members[:]
        self.mutate()
        if kw.get('edit'):self.members.append(kw['attribute']);return name
        self.layers.append(name);return name
    def setKeyframe(self,*a,**kw):
        self.key_count+=1;self.mutate()
        if self.key_count==self.fail_key:raise RuntimeError('key failure')
        return 1

class KeyLayerTests(unittest.TestCase):
    def setUp(self):
        self.c=KeyCmds();self.cp=patch.object(bridge,'_cmds',return_value=self.c);self.cp.start();self.addCleanup(self.cp.stop)
        self.wp=patch.object(bridge,'_writable_plug',side_effect=self.writable);self.wp.start();self.addCleanup(self.wp.stop)
    def writable(self,node,attr):
        if attr=='locked':raise bridge.BridgeError('LOCKED_ATTRIBUTE','locked')
        return node+'.'+attr,'double'
    def key(self,attr='translateX',time=1):return {'node':'|ctrl','attribute':attr,'time':time,'value':2}
    def test_preflight_failure_keys_nothing(self):
        with self.assertRaises(bridge.BridgeError):bridge._set_keyframes({'keys':[self.key(),self.key('locked')]})
        self.assertEqual(self.c.key_count,0);self.assertEqual(self.c.undos,0)
    def test_partial_key_failure_undoes_transaction(self):
        self.c.fail_key=2
        with self.assertRaises(RuntimeError):bridge._set_keyframes({'keys':[self.key(),self.key(time=2)]})
        self.assertEqual(self.c.undos,1);self.assertEqual(self.c.time,9)
    def test_existing_layers_require_explicit_target(self):
        self.c.layers=['BaseAnimation','Walk']
        with self.assertRaises(bridge.BridgeError) as cm:bridge._set_keyframes({'keys':[self.key()]})
        self.assertEqual(cm.exception.code,'LAYER_REQUIRED');self.assertEqual(self.c.key_count,0)
    def test_explicit_layer_adds_missing_channel(self):
        self.c.layers=['BaseAnimation','Walk']
        r=bridge._set_keyframes({'keys':[self.key()],'layer':'Walk'})
        self.assertEqual(r['layer'],'Walk');self.assertEqual(self.c.members,['|ctrl.translateX']);self.assertEqual(self.c.key_count,1)
    def test_duplicate_layer_is_rejected_without_transaction(self):
        self.c.layers=['Walk']
        with self.assertRaises(bridge.BridgeError) as cm:bridge._create_animation_layer({'name':'Walk','nodes':[],'attributes':['translateX']})
        self.assertEqual(cm.exception.code,'LAYER_EXISTS');self.assertEqual(self.c.undos,0)
    def test_locked_layer_is_rejected(self):
        self.c.layers=['Walk'];self.c.getAttr=lambda p:True
        with self.assertRaises(bridge.BridgeError) as cm:bridge._set_keyframes({'keys':[self.key()],'layer':'Walk'})
        self.assertEqual(cm.exception.code,'LOCKED_LAYER');self.assertEqual(self.c.key_count,0)
    def test_enum_and_bool_validation(self):
        for value,kind in [(1.5,'enum'),(2,'bool')]:
            with self.assertRaises(bridge.BridgeError):bridge._value_for_type(value,kind,'|ctrl.switch')


class WireHandlerTests(unittest.TestCase):
    def handle(self, raw):
        h=object.__new__(bridge._Handler);h.connection=FakeSocket();h.rfile=io.BytesIO(raw);h.wfile=io.BytesIO();h.server=types.SimpleNamespace(auth_token='offline-token',generation=42)
        h.handle();return json.loads(h.wfile.getvalue())
    def test_authenticated_request_dispatches_exactly_once(self):
        req={'id':'one','token':'offline-token','method':'get_status','params':{}}
        with patch.object(bridge,'_dispatch',return_value={'ready':True}) as dispatch:
            r=self.handle(json.dumps(req).encode()+b'\n');self.assertTrue(r['ok']);dispatch.assert_called_once_with('get_status',{},42)
    def test_bad_token_never_dispatches(self):
        req={'id':'one','token':'wrong','method':'set_keyframes','params':{}}
        with patch.object(bridge,'_dispatch') as dispatch:
            r=self.handle(json.dumps(req).encode()+b'\n');self.assertEqual(r['error']['code'],'UNAUTHORIZED');dispatch.assert_not_called()
    def test_invalid_request_envelopes_never_dispatch(self):
        for raw in [b'[]\n',b'null\n',b'{}',b'bad\n',b'{"id":true}\n',b'{"id":1,"params":{"x":NaN}}\n']:
            with self.subTest(raw=raw),patch.object(bridge,'_dispatch') as dispatch:
                r=self.handle(raw);self.assertEqual(r['error']['code'],'INVALID_REQUEST');dispatch.assert_not_called()
    def test_oversized_output_returns_protocol_error(self):
        req={'id':'one','token':'offline-token','method':'get_status','params':{}}
        with patch.object(bridge,'_dispatch',return_value={'x':'x'*500}),patch.object(bridge,'MAX_MESSAGE_BYTES',300):
            r=self.handle(json.dumps(req).encode()+b'\n');self.assertEqual(r['error']['code'],'RESPONSE_TOO_LARGE')

if __name__ == '__main__': unittest.main(verbosity=2)
