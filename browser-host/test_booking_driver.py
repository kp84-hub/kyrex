"""Real Chromium on an intercepted fixture. No restaurant network or live reservations."""
import copy
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'kyrex-cloud'),str(ROOT/'browser-host')]
import booking_schema as schema
import booking_driver as driver
import browser_operator

URL='https://restaurant.example/book'
DETAILS={'venue':'Fixture Grill','location':'Raleigh','date':'2030-06-14','time':'20:30','party_size':4,'name':'Keith Plane','phone':'919-555-0101','email':'keith@kyrex.dev'}
FORM='''<!doctype html><title>Fixture Grill</title><h1>Fixture Grill · Raleigh</h1>
<p>Available times for your reservation.</p><form action="/confirmation" method="post">
<label>Name<input id="name" name="name" required></label>
<label>Phone<input id="phone" type="tel" name="phone" required></label>
<label>Email<input id="email" type="email" name="email" required></label>
<label>Date<input id="date" type="date" name="date" required></label>
<label>Party size<select id="party_size" name="party_size" required><option value="">Choose</option><option value="4">4 people</option></select></label>
<label>Available time<select id="time" name="time" required><option value="">Choose</option><option value="20:30">8:30 PM</option></select></label>
<button id="book" type="submit">Book reservation</button></form>'''

class Proto:
    def __init__(self): self.operations=[];self.allowed=True
    def operation(self,*args): self.operations.append(args[0]);return self.allowed


@pytest.fixture
def browser(tmp_path):
    if os.environ.get('KYREX_TEST_BROWSER')!='1': pytest.skip('Real Chromium check runs in the required booking workflow.')
    from playwright.sync_api import sync_playwright
    pw=sync_playwright().start()
    try:
        chrome=pw.chromium.launch(headless=True)
    except Exception:
        pw.stop()
        raise
    context=chrome.new_context();page=context.new_page()
    state={'posts':[],'form':FORM,'outcome':'confirmed'}
    def fixture(route):
        if route.request.method=='POST':
            body=parse_qs(route.request.post_data or '')
            state['posts'].append(body)
            if state['outcome']=='error': text='<h1>The service could not verify this reservation</h1>'
            elif state['outcome']=='request': text='<h1>Request received</h1><p>We will email availability later.</p>'
            elif state['outcome']=='wrong': text='<h1>Reservation confirmed</h1><p>Fixture Grill, Raleigh 2030-06-15 8:30 PM Party: 5 Someone Else</p>'
            else: text='<h1>Reservation confirmed</h1><p>Fixture Grill, Raleigh 2030-06-14 8:30 PM Party: 4 Keith Plane</p><p>Confirmation code: FG-1234</p>'
            route.fulfill(status=200,content_type='text/html',body=text)
        else: route.fulfill(status=200,content_type='text/html',body=state['form'])
    # Page-level fixture interception prevents any external network request.
    context.route('**/*',fixture)
    class Browser:
        _page=page;_context=context;session_dir=tmp_path
        def navigate(self,url): page.goto(url,wait_until='domcontentloaded')
    yield Browser(),state,Proto()
    context.close();chrome.close();pw.stop()


def initial():
    return {'id':'a'*32,'nonce':'b'*64,'stage':'prepare','url':URL,'details':copy.deepcopy(DETAILS),'steps':[]}


def prepare(browser):
    live,state,proto=browser;spec=initial()
    snap=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)['snapshot']
    for field in ('name','phone','email','date','party_size','time'):
        c=next(c for c in snap['controls'] if c['selector']=='css=#'+field)
        spec['steps'].append({'op':'select' if c['tag']=='select' else 'fill','control':c,'field':field,'value':str(DETAILS[field])})
    reviewed=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)['snapshot']
    assert not state['posts'],'Preparation may not submit the form.'
    target=next(c for c in reviewed['controls'] if c['selector']=='css=#book')
    spec.update(stage='submit',expected=schema.review_hash(reviewed),submit=target)
    return spec,reviewed


def test_real_form_fills_then_submits_exact_approved_values_once(browser):
    live,state,proto=browser;spec,snap=prepare(browser)
    result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert result['outcome']=='confirmed' and result['reference']=='FG-1234'
    assert len(state['posts'])==1
    assert state['posts'][0]=={k:[str(DETAILS[k])] for k in ('name','phone','email','date','party_size','time')}
    duplicate=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert duplicate['outcome']=='unknown' and len(state['posts'])==1
    assert all(op=='browser.booking' for op in proto.operations)


def test_real_changed_terms_or_disabled_slot_prevents_booking(browser):
    live,state,proto=browser;spec,_=prepare(browser)
    state['form']=FORM.replace('Available times','Changed availability: Available times')
    result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert result['outcome']=='changed' and not state['posts']


@pytest.mark.parametrize('outcome,expected',[('wrong','unknown'),('error','unknown'),('request','submitted')])
def test_real_receipt_does_not_invent_confirmation_or_retry(browser,outcome,expected):
    live,state,proto=browser;spec,_=prepare(browser);state['outcome']=outcome
    result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert result['outcome']==expected
    assert len(state['posts'])==1
    driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert len(state['posts'])==1


def test_real_consequential_preparation_click_is_refused(browser):
    live,state,proto=browser;spec=initial()
    snap=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)['snapshot']
    spec['steps']=[{'op':'click','control':next(c for c in snap['controls'] if c['selector']=='css=#book')}]
    with pytest.raises(ValueError,match='approval'): driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert not state['posts']


def test_real_contact_is_not_entered_on_a_different_business_page(browser):
    live,state,proto=browser;spec=initial()
    snap=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)['snapshot']
    spec['steps']=[{'op':'fill','control':next(c for c in snap['controls'] if c['selector']=='css=#name'),'field':'name','value':'Keith Plane'}]
    state['form']=FORM.replace('Fixture Grill','Another Business')
    with pytest.raises(ValueError,match='business'): driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert live._page.locator('#name').input_value()=='' and not state['posts']


def test_real_final_operation_denial_prevents_the_submit_click(browser):
    live,state,proto=browser;spec,_=prepare(browser)
    count=[0]
    def authorize(*args):
        count[0]+=1
        return count[0]<len(spec['steps'])+2
    proto.operation=authorize
    result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert result['outcome']=='cancelled' and not state['posts']


def test_fixed_task_schema_rejects_generic_approval_and_unprovided_values():
    import json
    spec=initial();assert browser_operator.parse_spec(json.dumps({'booking_browser':spec}))[0]['action']=='booking_browser'
    with pytest.raises(browser_operator.SpecError): browser_operator.parse_spec(json.dumps({'actions':[{'action':'booking_browser','spec':spec}]}))
    assert not browser_operator.preflight(json.dumps({'booking_browser':spec}),['other.example'])[0]
    assert not schema.matches('email','attacker@example.com',DETAILS['email'])
    assert not schema.field_allowed('phone',{'label':'Credit card phone','type':'tel'})
    assert schema.field_value(DETAILS,'first_name')=='Keith' and schema.field_value(DETAILS,'last_name')=='Plane'


def test_success_phrase_alone_does_not_confirm_the_requested_booking():
    assert driver.receipt({'url':URL,'text':'Reservation confirmed'},DETAILS)['outcome']=='unknown'
    assert driver.receipt({'url':URL,'headings':['Reservation confirmed'],'text':'Fixture Grill 2030-06-14 8:30 PM Party: 5 Keith Plane Reservation confirmed'},DETAILS)['outcome']=='unknown'
    assert driver.receipt({'url':URL,'headings':['Reservation confirmed'],'text':'Fixture Grill 2030-06-14 8:30 PM Party: 4 Keith Plane Reservation confirmed'},DETAILS)['outcome']=='confirmed'



def test_real_auto_submit_on_field_change_cannot_book_before_yes(browser):
    live,state,proto=browser
    state['form']=FORM.replace('id="time" name="time"', 'id="time" name="time" onchange="this.form.submit()"')
    spec=initial()
    snap=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)['snapshot']
    for field in ('name','phone','email','date','party_size','time'):
        c=next(c for c in snap['controls'] if c['selector']=='css=#'+field)
        spec['steps'].append({'op':'select' if c['tag']=='select' else 'fill','control':c,'field':field,'value':str(DETAILS[field])})
    try:
        result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
        assert result.get('outcome')=='blocked' or result['status']=='error'
    except Exception:
        # Aborting the unapproved navigation can also make the live-origin check fail.
        pass
    assert not state['posts'],'Field change events may not submit before owner confirmation.'


def test_real_approved_click_cannot_post_to_an_unreviewed_endpoint(browser):
    live,state,proto=browser
    state['form']=FORM.replace('id="book" type="submit"',
        'id="book" type="button" onclick="fetch(\'/unreviewed\', {method:\'POST\',body:\'name=Keith Plane\'})"')
    spec,_=prepare(browser)
    result=driver.run(live,proto,spec,allowlist=['restaurant.example'],root=live.session_dir)
    assert result['outcome']=='unknown' and not state['posts']
