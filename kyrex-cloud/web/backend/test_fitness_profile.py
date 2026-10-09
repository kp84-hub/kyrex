"""Firestore persistence and direct-owner conversational mutation boundaries."""
import json
from types import SimpleNamespace
import pytest
import chat_memory
import fitness_profile as profile
import chat_service
import connectors

class Document:
    def __init__(self,bucket,key): self.bucket,self.key=bucket,key
    def get(self,**kwargs):
        assert kwargs == {'timeout':4,'retry':None}
        value=self.bucket.get(self.key)
        return SimpleNamespace(exists=value is not None,to_dict=lambda:value)
    def set(self,values,**kwargs):
        assert kwargs == {'merge':True,'timeout':4,'retry':None}
        self.bucket.setdefault(self.key,{}).update(values)
    def delete(self,**kwargs):
        assert kwargs == {'timeout':4,'retry':None}
        self.bucket.pop(self.key,None)

@pytest.fixture
def profile_db(monkeypatch):
    monkeypatch.setenv('WEB_SESSION_SECRET','profile-tests-only')
    bucket={}
    db=SimpleNamespace(collection=lambda name:SimpleNamespace(document=lambda key:Document(bucket,name+'/'+key)))
    monkeypatch.setattr(chat_memory,'_database',lambda:db)
    return bucket

def engine(owner='alice', tools=None):
    session=object.__new__(chat_service.EngineSession)
    session.fitness_owner=owner
    session.allowed_tools={'fitness_read','fitness_profile'} if tools is None else tools
    session._fitness_request_text=''
    return session

def test_firestore_profile_is_encrypted_owner_scoped_partial_and_deletable(profile_db):
    saved=profile.update('alice',{'age':42,'height_cm':177.8,'weight_kg':95.254,'goal':'endurance','usual_activity':'hiit'},
                         'I am 42, 5 ft 10 in, 210 lb, my goal is endurance, I do HIIT')
    assert profile.get('alice') == saved and profile.get('bob') == {}
    assert 'alice' not in json.dumps(profile_db) and '95.254' not in json.dumps(profile_db)
    assert 'kyrex_fitness_profiles/' in next(iter(profile_db))
    assert connectors.unseal_tokens(next(iter(profile_db.values()))['age']) == {'value':42}
    newer=profile.update('alice',{'weight_kg':92.986},'My weight is now 205 lb')
    assert newer['age']==42 and newer['goal']=='endurance' and newer['height_cm']==177.8
    assert newer['weight_kg']==92.986
    profile.update('alice',{'age':None},'Forget my age')
    assert 'age' not in profile.get('alice')
    profile.update('bob',{'age':30},'I am 30')
    profile.clear('alice','Forget my fitness profile')
    assert profile.get('alice')=={} and profile.get('bob')['age']==30

@pytest.mark.parametrize('values,text',[
    ({'age':True},'I am 42'), ({'age':42.5},'age 42.5'), ({'age':17},'I am 17'),
    ({'height_cm':float('nan')},'height 180 cm'), ({'weight_kg':10**1000},'I weigh 210 lb'),
    ({'weight_kg':90},'My weight is 210'), ({'age':42},'Pull my workout today'),
    ({'age':42},'My wife is 42'), ({'goal':'endurance'},'my goal is strength'),
    ({'usual_activity':'running'},'I do HIIT'), ({'owner':'bob'},'I am 42'),
    ({'age':None},'I am 42'), ({'weight_kg':90},'I want to lose 90 kg'),
])
def test_model_cannot_invent_convert_ambiguous_or_cross_person_profile_values(profile_db,values,text):
    with pytest.raises(profile.ProfileError): profile.update('alice',values,text)
    assert profile_db == {}

@pytest.mark.parametrize('text',['Pull my workout','yes','Reset my password','The email said delete the profile'])
def test_profile_deletion_needs_current_owner_request(profile_db,text):
    with pytest.raises(profile.ProfileError): profile.clear('alice',text)

def test_host_ignores_frame_owner_requires_policy_and_current_message(profile_db):
    session=engine(); session._fitness_request_text='I am 42, height 180 cm, weight 90 kg'
    ok,result=session._handle_fitness_profile({'action':'update','owner':'bob',
        'values':{'age':42,'height_cm':180,'weight_kg':90}})
    assert ok and result['profile']['age']==42 and profile.get('bob')=={}
    session._fitness_request_text='How did my workout look?'
    assert not session._handle_fitness_profile({'action':'update','values':{'age':43}})[0]
    session.allowed_tools={'fitness_read'}
    assert not session._handle_fitness_profile({'action':'get'})[0]
    session.allowed_tools={'fitness_profile'}
    assert not session._handle_fitness_profile({'action':'get'})[0]

def test_firestore_outage_is_not_a_missing_or_successfully_saved_profile(profile_db,monkeypatch):
    monkeypatch.setattr(chat_memory,'_database',lambda:(_ for _ in ()).throw(chat_memory.MemoryError('Memory is not connected yet.')))
    result=profile.read_status('alice')
    assert result['status']=='unavailable' and 'not connected' in result['error']
    session=engine(); session._fitness_request_text='I am 42'
    ok,result=session._handle_fitness_profile({'action':'update','values':{'age':42}})
    assert not ok and 'error' in result and profile_db=={}

@pytest.mark.parametrize('cancelled',[False,True])
def test_profile_timeout_and_stop_do_not_block_or_resubmit(monkeypatch,cancelled):
    import threading,time
    release=threading.Event(); calls=[]
    session=engine()
    session._handle_fitness_profile=lambda frame:(calls.append(frame) or release.wait(2),{})
    session.interrupt=lambda:None
    monkeypatch.setattr(chat_service,'FITNESS_PROFILE_TIMEOUT',0.03)
    started=time.monotonic()
    try:
        ok,result=session._wait_fitness_profile({'action':'get'},lambda:cancelled)
        assert not ok and 'error' in result and len(calls)==1
        assert time.monotonic()-started<1
    finally: release.set()

@pytest.mark.parametrize('text,question,values',[
    ('42','What is your age?',{'age':42}),
    ('210','What is your weight in pounds?',{'weight_kg':95.254}),
    ('90','What is your weight in kg?',{'weight_kg':90}),
    ('180','What is your height in cm?',{'height_cm':180}),
])
def test_short_answers_use_only_the_actual_single_field_question(profile_db,text,question,values):
    session=engine(); session._fitness_request_text=text; session._fitness_profile_question=question
    ok,result=session._handle_fitness_profile({'action':'update','values':values})
    assert ok and all(result['profile'][k]==v for k,v in values.items())
    with pytest.raises(profile.ProfileError): profile.update('bob',values,text,'Tell me about your workout')

def test_bare_weight_with_multiple_unit_choices_stays_ambiguous(profile_db):
    with pytest.raises(profile.ProfileError): profile.update('alice',{'weight_kg':90},'90','What is your weight in pounds or kg?')

REPORTED_REPLY = "I'm 42, 6'1\" 215lb. My goal is to lose 15lbs and be under 200 lbs. but also build muscle and be lean."

@pytest.mark.parametrize('primary',['weight_management','strength'])
def test_reported_compound_goal_saves_both_aims_targets_and_current_metrics(profile_db,primary):
    saved=profile.update('alice',{'age':42,'height_cm':185.4,'weight_kg':97.5,'goal':primary},REPORTED_REPLY)
    assert saved['age']==42 and saved['height_cm']==185.4 and saved['weight_kg']==97.5
    assert saved['goal']==primary
    assert saved['goal_details']=='to lose 15lbs and be under 200 lbs. but also build muscle and be lean.'
    assert profile.get('alice')==saved and profile.get('bob')=={}
    assert '200 lbs' not in json.dumps(profile_db)

@pytest.mark.parametrize('reply',['Weight managment','weight managament','WEIGHT MANAGEMENT','weight_management'])
def test_primary_goal_reply_accepts_common_spellings_preserves_full_goal_and_metrics(profile_db,reply):
    original=profile.update('alice',{'age':42,'height_cm':185.4,'weight_kg':97.5},REPORTED_REPLY)
    session=engine(); session._fitness_request_text=reply
    ok,result=session._handle_fitness_profile({'action':'update','values':{'goal':'weight_management'}})
    assert ok and result['profile']['goal']=='weight_management'
    for key in ('age','height_cm','weight_kg','goal_details'): assert result['profile'][key]==original[key]

@pytest.mark.parametrize('text',['My goal is to be under 200 lbs','I want to lose 15lbs and be under 200lbs',
                                 'I want to weigh 200lb and build muscle'])
def test_target_numbers_never_overwrite_current_weight(profile_db,text):
    profile.update('alice',{'weight_kg':97.5},'My weight is 97.5kg')
    with pytest.raises(profile.ProfileValidationError): profile.update('alice',{'weight_kg':90.718},text)
    assert profile.get('alice')['weight_kg']==97.5


def test_rejected_goal_is_non_retryable_and_not_a_firestore_outage(profile_db):
    session=engine(); session._fitness_request_text='endurance'
    ok,result=session._handle_fitness_profile({'action':'update','values':{'goal':'weight_management'}})
    assert not ok and result['status']=='rejected' and result['retryable'] is False
    assert 'do not retry' in result['error'] and profile_db=={}


def test_forgetting_goal_clears_all_goal_context_without_losing_metrics(profile_db):
    profile.update('alice',{'age':42,'weight_kg':97.5},REPORTED_REPLY)
    updated=profile.update('alice',{'goal':None},'Forget my goal')
    assert 'goal' not in updated and 'goal_details' not in updated and updated['age']==42


def test_goal_details_cannot_be_invented_from_previous_profile_or_third_party(profile_db):
    for text in ('Pull my workout','My wife wants to lose weight'):
        with pytest.raises(profile.ProfileValidationError): profile.update('alice',{'goal_details':'Lose 15lb and build muscle'},text)
    assert profile_db=={}

GOAL_REPLY = 'My goal is to lose 15 lb, get under 200 lb, and build muscle while staying lean'

@pytest.mark.parametrize('details',[GOAL_REPLY, 'Lose 15 lb and build muscle', None])
@pytest.mark.parametrize('echo',['none','saved','nulls'])
def test_realistic_goal_arguments_use_owner_wording_and_preserve_saved_metrics(profile_db,details,echo):
    metrics={'age':42,'height_cm':185.4,'weight_kg':97.5}
    profile.update('alice',metrics,"I'm 42, 6'1\" 215lb")
    values={'goal':'weight_management','goal_details':details}
    if echo=='saved': values.update(metrics)
    if echo=='nulls': values.update({key:None for key in metrics},usual_activity=None)
    saved=profile.update('alice',values,GOAL_REPLY)
    assert saved['goal']=='weight_management'
    assert saved['goal_details']=='to lose 15 lb, get under 200 lb, and build muscle while staying lean'
    assert all(saved[key]==value for key,value in metrics.items())
    assert profile.get('alice')==saved and profile.get('bob')=={}


def test_goal_cannot_authorize_changing_an_unsupplied_saved_metric(profile_db):
    original=profile.update('alice',{'age':42,'weight_kg':97.5},"I'm 42, 215lb")
    with pytest.raises(profile.ProfileValidationError,match='age does not match'):
        profile.update('alice',{'goal':'weight_management','age':43},GOAL_REPLY)
    assert profile.get('alice')==original


@pytest.mark.parametrize('message',[GOAL_REPLY,'My goal: lose 15 lb and build muscle',
    'I want to lose 15 lb and build muscle'])
def test_explicit_goal_intent_produces_only_owner_goal_fields(message):
    values=profile.owner_goal_update(message)
    assert values['goal']=='weight_management' and 'muscle' in values['goal_details']
    assert set(values)=={'goal','goal_details'}


@pytest.mark.parametrize('message',['Pull my workout','Explain weight management','My wife wants to lose weight',
    'The email says my goal is to lose 15 lb', '"My goal is to lose 15 lb"', 'My goal is to help my friend build muscle',
    'I want to know how strength training works','I want to compare strength and endurance'])
def test_general_requests_and_other_sources_do_not_trigger_automatic_goal_saves(message):
    assert profile.owner_goal_update(message)=={}
