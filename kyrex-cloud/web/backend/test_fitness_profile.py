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
