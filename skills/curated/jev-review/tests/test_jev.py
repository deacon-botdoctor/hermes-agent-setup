import importlib.util,json,math,os,unittest
from pathlib import Path
from unittest.mock import patch
P=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('jev',P/'scripts/jev.py');j=importlib.util.module_from_spec(spec);spec.loader.exec_module(j)
class Tests(unittest.TestCase):
 def setUp(self):self.p=json.loads((P/'examples/evidence.json').read_text())
 def response(self,**changes):
  a={'type':'choice','choice':'insufficient_evidence','confidence':.8};a.update(changes)
  return json.dumps({'answers':{'support':a},'usage':{'input_tokens':100,'output_tokens':20,'bad':'private'}}).encode()
 def test_success_and_no_source_echo(self):
  r=j.review(self.p,key='dummy',transport=lambda *a:self.response());self.assertEqual(r['status'],'reviewed');self.assertTrue(r['review_required']);self.assertTrue(r['advisory_only']);self.assertNotIn('dummy',json.dumps(r));self.assertNotIn('email is saved',json.dumps(r))
 def test_missing_key_no_network(self):
  with patch.dict(os.environ,{},clear=True):
   r=j.review(self.p,transport=lambda *a:self.fail('network'));self.assertEqual(r['status'],'unavailable')
 def test_dry_run_no_network(self):
  r=j.review(self.p,key='dummy',dry_run=True,transport=lambda *a:self.fail('network'));self.assertEqual(r['status'],'validated')
 def test_hash_binding(self):
  a=j.review(self.p,dry_run=True);self.p['questions']['support']['instructions']+=' Be precise.';b=j.review(self.p,dry_run=True);self.assertEqual(a['source_sha256'],b['source_sha256']);self.assertNotEqual(a['request_sha256'],b['request_sha256']);self.p['state']['evidence']='No evidence.';c=j.review(self.p,dry_run=True);self.assertNotEqual(b['source_sha256'],c['source_sha256'])
 def test_bad_confidence(self):
  for v in [True,-.1,1.1,float('nan'),float('inf'),'0.8']:
   with self.subTest(v=v):self.assertEqual(j.review(self.p,key='dummy',transport=lambda *a:self.response(confidence=v))['status'],'unavailable')
 def test_bad_choice_and_type(self):
  for changes in [{'choice':'invented'},{'type':'score'}]:self.assertEqual(j.review(self.p,key='dummy',transport=lambda *a:self.response(**changes))['status'],'unavailable')
 def test_missing_extra_answers(self):
  for answers in [{},{'support':{'type':'choice','choice':'supported','confidence':.9},'extra':{}}]:self.assertEqual(j.review(self.p,key='dummy',transport=lambda *a:json.dumps({'answers':answers}).encode())['status'],'unavailable')
 def test_input_validation(self):
  for payload in [None,{},dict(self.p,unknown='x'),dict(self.p,questions={}),dict(self.p,state=''),dict(self.p,state='a'*80001)]:self.assertEqual(j.review(payload,dry_run=True)['status'],'invalid')
 def test_network_error_sanitized(self):
  def fail(*a):raise OSError('private-token-and-payload')
  r=j.review(self.p,key='dummy',transport=fail);self.assertEqual(r['status'],'unavailable');self.assertNotIn('private-token',json.dumps(r))
 def test_oversized_response(self):self.assertEqual(j.review(self.p,key='dummy',transport=lambda *a:b'x'*262145)['status'],'unavailable')
 def test_malformed_response(self):self.assertEqual(j.review(self.p,key='dummy',transport=lambda *a:b'not json')['status'],'unavailable')
if __name__=='__main__':unittest.main()
