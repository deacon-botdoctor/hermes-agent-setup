# Native d363 applied-runtime behavior tests. Copied verbatim to the isolated
# upstream tests directory for scripts/run_tests.sh; no live backend or handoff.
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

@pytest.fixture
def native(monkeypatch, tmp_path):
    try:
        from tools.computer_use import tool as t
    except ImportError:
        pytest.skip('requires applied d363 runtime')
    if not hasattr(t, '_dispatch_native'):
        pytest.skip('requires applied d363 runtime')
    monkeypatch.setenv('HERMES_COMPUTER_USE_GATE_FILE', str(tmp_path / 'input.lock'))
    t._blocked_sensitive_surfaces.clear()
    t._blocked_login_targets.clear()
    return t

class NativeBackend:
    def __init__(self, t, states):
        self.t=t; self.states=list(states); self._last_target={'pid':1,'window_id':2}; self._last_app='Chrome'; self.inputs=[]; self.targets=[]
    def capture(self, **kw):
        self.targets.append(kw)
        self._last_target={k:kw[k] for k in ('pid','window_id')}
        state=self.states.pop(0) if len(self.states)>1 else self.states[0]
        roles={'clean':[('AXWebArea','Home'),('AXButton','Search')], 'copy':[('AXWebArea','Login security guide'),('AXStaticText','Passwords and sign in help'),('AXButton','Search')], 'login':[('AXWebArea',''),('AXSecureTextField','Password')], 'permission':[('AXDialog','Allow access to camera'),('AXButton','Allow')], 'payment':[('AXDialog','Apple Pay'),('AXButton','Pay now')]}
        cap=self.t.CaptureResult(mode='ax',width=100,height=100,app='Chrome',window_title='Home',elements=[self.t.UIElement(i+1,r,l) for i,(r,l) in enumerate(roles.get(state,roles['clean']))])
        if state=='wrong': self._last_target={'pid':9,'window_id':10}
        if state=='wrong_elements': cap.elements[0].pid=9
        return cap
    def type_text(self, text, **kw):
        self.inputs.append(text); return self.t.ActionResult(True,'type')

@pytest.mark.parametrize('state', ['copy','clean'])
def test_native_innocuous_text_allowed(native,state):
    b=NativeBackend(native,[state]); result=json.loads(native._dispatch(b,'type',{'text':'hello'})); assert result['ok']; assert b.inputs==['hello']

@pytest.mark.parametrize('state,code',[('payment','payment_dialog_refused'),('permission','permission_dialog_refused'),('wrong','auth_window_unverified'),('wrong_elements','auth_window_unverified')])
def test_native_destination_and_identity(native,state,code):
    b=NativeBackend(native,[state]); result=json.loads(native._dispatch(b,'type',{'text':'hello','pid':3,'window_id':4})); assert result['code']==code; assert not b.inputs; assert b.targets[0]['pid']==3 and b.targets[0]['window_id']==4

@pytest.mark.parametrize('after',['clean','login','permission','payment','wrong','wrong_elements'])
def test_native_done_rechecks_same_clean_window(native,monkeypatch,after):
    b=NativeBackend(native,['login',after]); monkeypatch.setattr(native,'_invoke_auth_handoff',lambda site:'done')
    result=json.loads(native._dispatch(b,'type',{'text':'hello','pid':3,'window_id':4})); assert result['status']==('ok' if after=='clean' else 'auth_required'); assert not b.inputs
    assert len(b.targets)==2 and all(x['pid']==3 and x['window_id']==4 for x in b.targets)

def test_native_pending_freezes_threads_and_other_process(native,monkeypatch):
    ready=threading.Event(); release=threading.Event()
    def handoff(site): ready.set(); assert release.wait(10); return 'skip'
    monkeypatch.setattr(native,'_invoke_auth_handoff',handoff)
    first=NativeBackend(native,['login']); second=NativeBackend(native,['clean'])
    thread=threading.Thread(target=lambda:native._dispatch(first,'type',{'text':'first'})); thread.start()
    try:
        assert ready.wait(5)
        assert json.loads(native._dispatch(second,'type',{'text':'second'}))['status']=='auth_required'
        proc=subprocess.run([sys.executable,'-c',"from tools.computer_use import tool; print(tool._dispatch(None, 'type', {'text':'forbidden'}))"],text=True,capture_output=True,timeout=5,env=os.environ.copy())
        assert proc.returncode==0,proc.stderr
        assert json.loads(proc.stdout)['status']=='auth_required'; assert not second.inputs
    finally: release.set(); thread.join(5)

def test_native_opaque_script_args(native,monkeypatch,tmp_path):
    home=tmp_path/'home'; (home/'bin').mkdir(parents=True)
    (home/'bin/website_auth_access.py').write_text("import sys,json,os\nassert sys.argv[1:]==['handoff','--site','unverified browser window','--reason','computer_use reached a website login wall','--lane','computer_use']\nassert 'HERMES_AUTH_SESSION_HANDLE' not in os.environ\nprint(json.dumps({'status':'skip'}))\n")
    monkeypatch.setenv('HERMES_HOME',str(home)); monkeypatch.setenv('HERMES_AUTH_SESSION_HANDLE','must-not-pass')
    assert native._invoke_auth_handoff('unverified browser window')=='skip'

@pytest.mark.asyncio
async def test_native_callback_authorization_and_stdin(monkeypatch,tmp_path):
    from plugins.platforms.telegram.adapter import TelegramAdapter
    if not hasattr(TelegramAdapter,'_handle_human_auth_callback'): pytest.skip('requires applied d363 runtime')
    adapter=object.__new__(TelegramAdapter)
    allowed=False
    adapter._is_callback_user_authorized=lambda *a,**kw: allowed
    cap='a'*22; query=SimpleNamespace(data='hah:done:'+('b'*24)+':'+cap,from_user=SimpleNamespace(id=42,first_name='Test'),message=SimpleNamespace(chat_id=1,chat=SimpleNamespace(type='private'),message_thread_id=None),answer=AsyncMock())
    home=tmp_path/'home'; (home/'bin').mkdir(parents=True); monkeypatch.setenv('HERMES_HOME',str(home))
    # Denied callback must not run subprocess even if the script is absent.
    import unittest.mock
    with unittest.mock.patch('subprocess.run',side_effect=AssertionError('unauthorized script')):
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query),None)
    assert 'not authorized' in query.answer.call_args.kwargs['text']
    (home/'bin/human_auth_handoff.py').write_text("import sys\nassert sys.argv[1:]==['resolve','--handoff-id','"+('b'*24)+"','--result','done','--caller-id','42']\nassert sys.stdin.read()=='"+cap+"\\n'\n")
    allowed=True
    await adapter._handle_callback_query(SimpleNamespace(callback_query=query),None)
    assert query.answer.call_args.kwargs['text']=='Done'


def test_native_element_input_requires_matching_safe_snapshot(native):
    b=NativeBackend(native,['clean'])
    b.click=lambda **kw: (b.inputs.append(kw) or native.ActionResult(True,'click'))
    result=json.loads(native._dispatch(b,'click',{'element':2}))
    assert result['status']=='auth_required' and not b.inputs
    native._dispatch(b,'capture',{'mode':'ax','pid':1,'window_id':2})
    result=json.loads(native._dispatch(b,'click',{'element':2}))
    assert result['ok'] and len(b.inputs)==1
    result=json.loads(native._dispatch(b,'click',{'element':2}))
    assert result['status']=='auth_required' and len(b.inputs)==1


def test_native_app_mismatch_retains_native_refusal(native):
    b=NativeBackend(native,['clean'])
    result=json.loads(native._dispatch(b,'type',{'text':'hello','app':'Finder'}))
    assert result['code']=='input_target_mismatch' and not b.inputs and not b.targets


@pytest.mark.parametrize('orphan', ['dead_owner', 'expired'])
def test_native_dispatch_recovers_orphan_under_os_gate(native, monkeypatch, orphan):
    import time
    if orphan == 'dead_owner':
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait(timeout=5)
        pid, deadline = child.pid, time.time() + 600
        assert not native._pid_is_alive(pid)
    else:
        pid, deadline = os.getpid(), time.time() - 1
    native._shared_handoff_epoch_path().write_text('1')
    native._shared_handoff_owner_path().write_text(json.dumps({'pid': pid, 'pending_until': deadline}))
    original = native._recover_orphaned_shared_handoff
    recovered = []
    def recover(epoch):
        assert native._process_gate_users > 0 and native._process_gate_handle is not None
        recovered.append(epoch)
        return original(epoch)
    monkeypatch.setattr(native, '_recover_orphaned_shared_handoff', recover)
    backend = NativeBackend(native, ['clean'])
    # The call observed an old auth epoch: repair it, but never replay that input.
    result = json.loads(native._dispatch(backend, 'type', {'text': 'stale'}))
    assert result['status'] == 'auth_required' and not backend.inputs
    assert recovered == [1]
    assert native._read_shared_handoff_epoch() == 2
    assert not native._shared_handoff_owner_path().exists()
    assert native._process_gate_users == 0
    result = json.loads(native._dispatch(backend, 'type', {'text': 'fresh'}))
    assert result['ok'] and backend.inputs == ['fresh']


def test_native_dispatch_preserves_live_owner_freeze(native, monkeypatch):
    native._shared_handoff_epoch_path().write_text('1')
    native._write_shared_handoff_owner()
    before = native._shared_handoff_owner_path().read_bytes()
    monkeypatch.setattr(native, '_enter_process_gate', lambda: pytest.fail('active owner must refuse without blocking'))
    backend = NativeBackend(native, ['clean'])
    assert json.loads(native._dispatch(backend, 'type', {'text': 'stale'}))['status'] == 'auth_required'
    assert not backend.inputs and native._read_shared_handoff_epoch() == 1
    assert native._shared_handoff_owner_path().read_bytes() == before


def test_native_dispatch_refuses_epoch_changed_while_waiting_for_gate(native, monkeypatch):
    original = native._enter_process_gate
    def enter_after_handoff():
        original()
        native._advance_shared_handoff_epoch()
        native._advance_shared_handoff_epoch()
    monkeypatch.setattr(native, '_enter_process_gate', enter_after_handoff)
    backend = NativeBackend(native, ['clean'])
    assert json.loads(native._dispatch(backend, 'type', {'text': 'queued'}))['status'] == 'auth_required'
    assert not backend.inputs and native._process_gate_users == 0


def test_native_orphan_recovery_rechecks_owner_after_gate_acquisition(native, monkeypatch):
    native._shared_handoff_epoch_path().write_text('1')
    original = native._enter_process_gate
    def enter_after_owner_appears():
        original()
        native._write_shared_handoff_owner()
    monkeypatch.setattr(native, '_enter_process_gate', enter_after_owner_appears)
    backend = NativeBackend(native, ['clean'])
    assert json.loads(native._dispatch(backend, 'type', {'text': 'stale'}))['status'] == 'auth_required'
    assert not backend.inputs and native._read_shared_handoff_epoch() == 1
    assert native._shared_handoff_owner_path().exists()
    assert native._process_gate_users == 0


@pytest.mark.parametrize('role,label,blocked', [
    ('AXButton', 'Buy now', True),
    ('AXButton', 'Place order', True),
    ('AXButton', 'Confirm order', True),
    ('AXButton', 'Submit order', True),
    ('AXButton', 'Order now', True),
    ('AXStaticText', 'Confirm order', False),
    ('AXStaticText', 'Submit order', False),
    ('AXStaticText', 'Order now', False),
    ('AXButton', 'Pay now', True),
    ('AXStaticText', 'Buy now', False),
    ('AXStaticText', 'Payment methods and PayPal help', False),
])
def test_native_nonmodal_payment_action_refusal(native, role, label, blocked):
    backend = NativeBackend(native, ['clean'])
    capture = backend.capture
    def checkout(**kwargs):
        cap = capture(**kwargs)
        cap.window_title = 'Checkout'
        cap.elements = [native.UIElement(1, 'AXWebArea', 'Checkout'), native.UIElement(2, role, label), native.UIElement(3, 'AXButton', 'Search')]
        return cap
    backend.capture = checkout
    result = json.loads(native._dispatch(backend, 'type', {'text': 'must not pay'}))
    if blocked:
        assert result['code'] == 'payment_dialog_refused' and not backend.inputs
    else:
        assert result['ok'] and backend.inputs == ['must not pay']


@pytest.mark.parametrize('destination', [(1, 3), (9, 2)])
def test_native_snapshot_binds_exact_latest_pid_window(native, destination):
    backend = NativeBackend(native, ['clean'])
    backend.click = lambda **kw: (backend.inputs.append(kw) or native.ActionResult(True, 'click'))
    a = {'pid': 1, 'window_id': 2}
    b = dict(zip(('pid', 'window_id'), destination))
    # Same semantic element in another window/process must not reuse A's index.
    native._dispatch(backend, 'capture', {'mode': 'ax', **a})
    refused = json.loads(native._dispatch(backend, 'click', {'element': 2, **b}))
    assert refused['status'] == 'auth_required' and not backend.inputs
    native._dispatch(backend, 'capture', {'mode': 'ax', **b})
    assert json.loads(native._dispatch(backend, 'click', {'element': 2, **b}))['ok']
    assert len(backend.inputs) == 1
    # Capturing B replaces A rather than keeping an old-window snapshot cache.
    native._dispatch(backend, 'capture', {'mode': 'ax', **a})
    native._dispatch(backend, 'capture', {'mode': 'ax', **b})
    refused = json.loads(native._dispatch(backend, 'click', {'element': 2, **a}))
    assert refused['status'] == 'auth_required' and len(backend.inputs) == 1
    native._dispatch(backend, 'capture', {'mode': 'ax', **a})
    assert json.loads(native._dispatch(backend, 'click', {'element': 2, **a}))['ok']


@pytest.mark.parametrize('after,code', [('payment', 'payment_dialog_refused'), ('permission', 'permission_dialog_refused'), ('login', None), ('empty', 'auth_window_unverified')])
def test_native_follow_capture_preserves_safety_verdict(native, monkeypatch, after, code):
    backend = NativeBackend(native, ['clean', after])
    capture = backend.capture
    def post_capture(**kwargs):
        cap = capture(**kwargs)
        if after == 'empty' and len(backend.targets) > 1:
            cap.elements = []
        return cap
    backend.capture = post_capture
    def confirmed_type(text, **kwargs):
        backend.inputs.append(text)
        return native.ActionResult(True, 'type', message='Typed successfully', verified=True, effect='confirmed', code='action_ok')
    backend.type_text = confirmed_type
    monkeypatch.setattr(native, '_get_backend', lambda **kw: backend)
    monkeypatch.setattr(native, '_request_approval', lambda *a, **kw: None)
    monkeypatch.setattr(native, '_capture_after_mode', lambda: 'ax')
    monkeypatch.setattr(native, '_invoke_auth_handoff', lambda site: 'skip')
    result = json.loads(native.handle_computer_use({'action': 'type', 'text': 'safe initial input', 'capture_after': True, 'pid': 1, 'window_id': 2}))
    assert backend.inputs == ['safe initial input'] and len(backend.targets) == 2
    assert result.get('code') == code
    assert 'ok' not in result and 'verdict' not in result
    if after == 'login':
        assert result['message'] == 'website authentication was not completed'
        assert result['status'] == 'auth_required'
    if after == 'empty':
        assert result['status'] == 'auth_required'

@pytest.mark.parametrize('app,has_document,allowed', [('Nebula',False,False),('Finder',False,True),('Chrome',False,False),('Chrome',True,True),('Nebula',True,True)])
def test_unknown_app_chrome_does_not_prove_native_identity(native, app, has_document, allowed):
    b = NativeBackend(native, ['clean'])
    b._last_app = app
    capture = b.capture
    def partial_capture(**kwargs):
        cap = capture(**kwargs)
        cap.app = app
        cap.window_title = 'Home'
        if not has_document:
            cap.elements = [native.UIElement(1, 'AXButton', 'Search')]
        return cap
    b.capture = partial_capture
    captured = json.loads(native._dispatch(b, 'capture', {'pid':1, 'window_id':2}))
    result = json.loads(native._dispatch(b, 'type', {'text':'test', 'pid':1, 'window_id':2}))
    if allowed:
        assert captured['elements'] and result['ok'] and b.inputs == ['test']
    else:
        assert captured['code'] == 'auth_window_unverified'
        assert result['status'] == 'auth_required' and b.inputs == []

@pytest.mark.parametrize('app,title', [('Finder','Search'),('TextEdit','Browser notes')])
def test_native_app_identity_ignores_document_title(native, app, title):
    b = NativeBackend(native, ['clean'])
    b._last_app = app
    capture = b.capture
    def native_capture(**kwargs):
        cap = capture(**kwargs)
        cap.app, cap.window_title = app, title
        cap.elements = [native.UIElement(1,'AXButton','One')]
        return cap
    b.capture = native_capture
    assert json.loads(native._dispatch(b,'capture',{'mode':'ax','pid':1,'window_id':2}))['elements']
    assert json.loads(native._dispatch(b,'type',{'text':'test','pid':1,'window_id':2}))['ok']
    assert b.inputs == ['test']

@pytest.mark.parametrize('app,allowed', [('Gmail',False),('WebNotes',False),('Calculator Pro',False),('prefixFinderSuffix',False),('Mail',True),('Notes',True),(' TextEdit ',True)])
def test_native_application_requires_exact_known_identity(native, app, allowed):
    b = NativeBackend(native, ['clean'])
    b._last_app = app
    original = b.capture
    def capture(**kwargs):
        cap = original(**kwargs)
        cap.app = app
        cap.elements = [native.UIElement(1,'AXButton','Help')]
        return cap
    b.capture = capture
    cap = json.loads(native._dispatch(b,'capture',{'mode':'ax','pid':1,'window_id':2}))
    result = json.loads(native._dispatch(b,'type',{'text':'test','pid':1,'window_id':2}))
    assert bool(cap.get('elements')) is allowed
    assert bool(b.inputs) is allowed
    if not allowed:
        assert result['status']=='auth_required'


def test_native_handoff_helper_timeout_keeps_login_block_and_releases_gate(native, monkeypatch):
    monkeypatch.setenv('HERMES_AUTH_HANDOFF_TIMEOUT_S','5')
    b = NativeBackend(native,['login'])
    seen = []
    def hung(cmd, **kwargs):
        seen.append(kwargs['timeout'])
        assert native._computer_auth_pending and native._process_gate_users > 0
        queued = NativeBackend(native,['clean'])
        assert json.loads(native._dispatch(queued,'type',{'text':'queued'}))['status']=='auth_required'
        assert queued.inputs == []
        raise subprocess.TimeoutExpired(cmd,kwargs['timeout'])
    monkeypatch.setattr(native.subprocess,'run',hung)
    result = json.loads(native._dispatch(b,'type',{'text':'never','pid':1,'window_id':2}))
    assert seen == [35.0] and result['handoff_result']=='timeout' and b.inputs==[]
    assert not native._computer_auth_pending and not native._sensitive_handoff_owner
    assert native._process_gate_users==0 and native._process_gate_handle is None
    assert native._read_shared_handoff_epoch()%2==0 and not native._shared_handoff_owner_path().exists()
    assert native._blocked_sensitive_surfaces[id(b)]=='login'
    assert native._blocked_login_targets[id(b)]==(1,2)
    other = NativeBackend(native,['clean'])
    assert json.loads(native._dispatch(other,'type',{'text':'fresh request','pid':3,'window_id':4}))['ok']
    assert other.inputs==['fresh request']
