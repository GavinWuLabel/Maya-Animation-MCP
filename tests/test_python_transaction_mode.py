import contextlib,sys,unittest
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from maya_side import maya_bridge as b
class TransactionModeTests(unittest.TestCase):
 def test_default_uses_transaction(self):
  with patch.object(b,'_allow_python',True),patch.object(b,'_cmds'),patch.object(b,'_undo_chunk',return_value=contextlib.nullcontext()) as tx:
   self.assertEqual(b._execute_python({'code':'result=7'})['result'],7)
   tx.assert_called_once()
 def test_explicit_false_does_not_open_transaction(self):
  cmds=Mock()
  with patch.object(b,'_allow_python',True),patch.object(b,'_cmds',return_value=cmds),patch.object(b,'_undo_chunk') as tx:
   b._execute_python({'code':'cmds.undo(); result=True','undo_chunk':False})
   tx.assert_not_called();cmds.undo.assert_called_once()
 def test_invalid_mode_rejected(self):
  with patch.object(b,'_allow_python',True):
   with self.assertRaisesRegex(b.BridgeError,'undo_chunk must be a boolean'):b._execute_python({'code':'result=1','undo_chunk':'false'})
 def test_false_failure_does_not_undo(self):
  cmds=Mock()
  with patch.object(b,'_allow_python',True),patch.object(b,'_cmds',return_value=cmds):
   with self.assertRaisesRegex(b.BridgeError,'ValueError: test'):b._execute_python({'code':'raise ValueError("test")','undo_chunk':False})
   cmds.undo.assert_not_called()
class PoseQueryTests(unittest.TestCase):
 def fake(self,state=True,fail=False):
  c=Mock();c.time=7;c.enabled=state
  def time(*args,**kwargs):
   if kwargs.get('query'):return c.time
   c.time=args[0]
  def undo(**kwargs):
   if kwargs.get('query'):return c.enabled
   c.enabled=kwargs['stateWithoutFlush']
  c.currentTime.side_effect=time;c.undoInfo.side_effect=undo
  c.listAttr.return_value=['translateX']
  c.getAttr.side_effect=lambda *a,**kw: 'double' if kw.get('type') else 3
  if fail:c.xform.side_effect=RuntimeError('query failed')
  else:c.xform.return_value=[1]*16
  return c
 def test_pose_restores_time_and_undo(self):
  c=self.fake()
  with patch.object(b,'_cmds',return_value=c),patch.object(b,'_nodes',return_value=['|ctrl']):
   self.assertEqual(b._get_pose({'nodes':['|ctrl'],'frame':2})['frame'],2)
  self.assertEqual(c.time,7);self.assertTrue(c.enabled)
  self.assertEqual(c.undoInfo.call_args_list[-1].kwargs,{'stateWithoutFlush':True})
 def test_query_failure_restores_time_and_undo(self):
  c=self.fake(fail=True)
  with patch.object(b,'_cmds',return_value=c),patch.object(b,'_nodes',return_value=['|ctrl']):
   with self.assertRaisesRegex(RuntimeError,'query failed'):b._get_pose({'nodes':['|ctrl'],'frame':2})
  self.assertEqual(c.time,7);self.assertTrue(c.enabled)
 def test_disabled_undo_remains_disabled(self):
  c=self.fake(state=False)
  with patch.object(b,'_cmds',return_value=c),patch.object(b,'_nodes',return_value=['|ctrl']):b._get_pose({'nodes':['|ctrl'],'frame':7})
  self.assertFalse(c.enabled)
  c.currentTime.assert_any_call(7,edit=True,update=True)
 def test_unspecified_frame_forces_evaluation(self):
  c=self.fake()
  with patch.object(b,'_cmds',return_value=c),patch.object(b,'_nodes',return_value=['|ctrl']):b._get_pose({'nodes':['|ctrl']})
  c.currentTime.assert_any_call(7,edit=True,update=True)
if __name__=='__main__' : unittest.main()
