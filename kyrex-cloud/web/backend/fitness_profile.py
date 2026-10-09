"""Conversational, owner-scoped fitness preferences in the existing Firestore project.

Only current owner messages authorize edits. Device records, prompts and model
arguments never supply owner identity or authority to mutate this profile.
"""
from __future__ import annotations
import hashlib
import re
from difflib import get_close_matches
from datetime import datetime, timezone
import connectors
import chat_memory

FIELDS = {'age', 'height_cm', 'weight_kg', 'goal', 'goal_details', 'usual_activity'}
TIMEOUT = 4

class ProfileError(Exception):
    pass

class ProfileValidationError(ProfileError):
    """Changing the same write or retrying it cannot resolve missing evidence."""
    pass

def validate(values):
    if not isinstance(values, dict) or not values or set(values) - FIELDS:
        raise ProfileValidationError('Provide only age, height_cm, weight_kg, goal, goal_details or usual_activity.')
    result = {}
    for field, value in values.items():
        if value is None:
            result[field] = None
        elif field in ('age','height_cm','weight_kg'):
            low, high = {'age':(18,120),'height_cm':(80,260),'weight_kg':(20,500)}[field]
            if (isinstance(value,bool) or not isinstance(value,(int,float)) or not low <= value <= high
                    or (field == 'age' and value != int(value))):
                raise ProfileValidationError(f'Invalid {field}; use adult age in whole years, height in cm and weight in kg.')
            result[field] = int(value) if field == 'age' else round(value,3)
        elif field == 'goal_details':
            if not isinstance(value,str) or not value.strip() or len(value)>500:
                raise ProfileValidationError('Goal details must be 1–500 characters from the owner’s goal statement.')
            result[field] = ' '.join(value.split())
        else:
            allowed = {'goal':{'general_fitness','endurance','strength','weight_management'},
                       'usual_activity':{'hiit','strength','running','cycling','walking','other'}}[field]
            if not isinstance(value,str) or value not in allowed:
                raise ProfileValidationError(f'Choose a supported {field}.')
            result[field] = value
    return result

def owner_fields(text, question=''):
    """Verify common natural replies with explicit units; never guess bare weight."""
    original = str(text or '')
    text = original.lower().replace('’', "'").replace('′', "'").replace('″','"')
    # Ambiguous multi-person messages need a clearer first-person answer.
    if re.search(r'\b(wife|husband|son|daughter|friend|their|he|she|email|article|record|says|said)\b', text): return {}
    fields = {}
    age = re.search(r"\b(?:i\s*(?:am|'m)|my age\s*(?:is|:)?|age\s*[:=]?)\s*(\d{1,3})(?![\d.])\b",text)
    if not age: age = re.search(r'\b(\d{1,3})\s*(?:years? old|yrs? old|y/?o)\b',text)
    if age: fields['age'] = int(age[1])
    height = re.search(r"(?<!\d)(\d{1,2})\s*(?:feet|foot|ft\b|')\s*(?:(\d{1,2}(?:\.\d+)?)\s*(?:inches|inch|in\b|\")?)?",text)
    if height: fields['height_cm'] = round((int(height[1])*12 + float(height[2] or 0))*2.54,3)
    else:
        height = re.search(r'\b(\d{2,3}(?:\.\d+)?)\s*(?:cm|centimeters?|centimetres?)\b',text)
        if height: fields['height_cm'] = float(height[1])
        else:
            height = re.search(r'\b(\d(?:\.\d+)?)\s*(?:m|meters?|metres?)\b',text)
            if height: fields['height_cm'] = round(float(height[1])*100,3)
    boundary = re.search(r'\b(?:my\s+(?:main\s+)?goals?\s*(?:is|are|:)|i\s+(?:want|would like)\s+to|goals?\s*[:=])',text)
    weights = []
    for weight in re.finditer(r'\b(\d{1,3}(?:\.\d+)?)\s*(kg|kilograms?|lb[s]?|pounds?)\b',text):
        before = text[:weight.start()].rstrip()
        # Targets (including "under 200 lb") are not current body weight.
        if re.search(r'(?:lose|losing|loss of|drop|dropping|cut|under|below|less than|get to|target)\s*(?:about|around|another)?\s*$',before): continue
        if boundary and weight.start()>boundary.start() and not re.search(r'\b(?:i (?:currently |now )?weigh|my (?:current )?weight (?:is|is now|:))\s*$',before): continue
        weights.append(round(float(weight[1])*(1 if weight[2].startswith('k') else 0.45359237),3))
    if len(set(weights)) == 1: fields['weight_kg'] = weights[0]
    # Correct only close spellings of the goal word, not arbitrary user facts.
    goal_text = re.sub(r'\bweight[ _-]+([a-z]+)\b', lambda match:
        'weight management' if get_close_matches(match[1],['management'],n=1,cutoff=0.85)
        else match[0], text)
    goals = (
        ('weight_management',r'\b(?:weight management|weight loss|fat loss|lose (?:some |extra |more |\d+\s*(?:lb[s]?|pounds?|kg) of )?weight|lose\s+\d+\s*(?:lb[s]?|pounds?|kg)|weight_management)\b'),
        ('strength',r'\b(?:strength|muscle|stronger)\b'),
        ('endurance',r'\b(?:endurance|stamina)\b'),
        ('general_fitness',r'\b(?:general fitness|general_fitness|overall fitness)\b'),
    )
    matches = sorted((match.start(),goal) for goal,pattern in goals
                     if (match := re.search(pattern,goal_text)))
    if matches:
        fields['goal'] = matches[0][1]
        fields['_goal_options'] = {goal for _,goal in matches}
        statement = re.search(r"\b(?:my\s+(?:main\s+)?goals?\s*(?:is|are|:)|i\s+(?:want|would like)\s+to|goals?\s*[:=])\s*(.+)",original,re.I)
        details = statement[1] if statement else original if len(matches)>1 else None
        if details and len(details)<=500: fields['goal_details'] = ' '.join(details.split())
    activities = [('hiit',r'\b(?:hiit|circuits?)\b'),('strength',r'\b(?:strength training|lifting|weightlifting)\b'),
                  ('running',r'\b(?:running|jogging)\b'),('cycling',r'\b(?:cycling|biking)\b'),('walking',r'\bwalking\b'),
                  ('other',r'\busual (?:workout|activity)\s*(?:is|:)?\s*other\b')]
    matches = [activity for activity,pattern in activities if re.search(pattern,text)]
    if len(matches) == 1: fields['usual_activity'] = matches[0]
    # A short reply can use units from the actual preceding assistant question,
    # but only when that question asks for one field with unambiguous units.
    if re.fullmatch(r'\s*\d{1,3}(?:\.\d+)?\s*',text):
        question = str(question or '').lower()
        asked = {field for field,pattern in (('age',r'\bage\b|how old'),('height_cm',r'\bheight\b|how tall'),
                                             ('weight_kg',r'\bweight\b|how much do you weigh')) if re.search(pattern,question)}
        if asked == {'age'} and float(text).is_integer(): fields['age'] = int(float(text))
        elif asked == {'weight_kg'}:
            pounds = bool(re.search(r'\bpounds?\b|\blbs?\b',question))
            kilos = bool(re.search(r'\bkilograms?\b|\bkg\b',question))
            if pounds != kilos: fields['weight_kg'] = round(float(text)*(0.45359237 if pounds else 1),3)
        elif asked == {'height_cm'} and re.search(r'\bcm\b|\bcentimeters?\b',question): fields['height_cm'] = float(text)
    for field, words in (('age','age'),('height_cm','height'),('weight_kg','weight'),('goal',r'goals?(?!\s+details)'),('goal_details','goal details'),('usual_activity','usual (?:workout|activity)')):
        if re.search(r'\b(?:forget|remove|clear|delete)\s+(?:my |the )?'+words+r'\b',text): fields[field] = None
    return fields

def authorize_update(values, owner_text, question=''):
    values = validate(values)
    supplied = owner_fields(owner_text,question)
    for field,value in values.items():
        expected = supplied.get(field)
        if field == 'goal' and value in supplied.get('_goal_options',set()): continue
        if field not in supplied or (isinstance(value,(int,float)) and expected is not None
                and isinstance(expected,(int,float)) and abs(value-expected)>0.03) or (
                not isinstance(value,(int,float)) and value != expected) or (value is not None and expected is None):
            raise ProfileValidationError('Save only details from the current owner message. Ask for the unclear field; do not retry the same write.')
    # Preserve the owner's full compound goal even when the model submits only
    # metrics or the legacy primary category. A short primary-goal choice later
    # leaves these details untouched.
    if 'goal_details' in supplied and supplied.get('goal') is not None:
        values.setdefault('goal',supplied['goal'])
        values['goal_details'] = supplied['goal_details']
    if 'goal' in values and values['goal'] is None: values['goal_details'] = None
    return values

def authorize_clear(owner_text):
    if not re.search(r'^\s*(?:(?:please|can you|could you|would you|i want to)\s+)*(?:forget|clear|delete|remove|reset)\s+(?:all |my |the )*(?:fitness |workout )?profile\b',str(owner_text or ''),re.I):
        raise ProfileValidationError('Ask the owner to explicitly request forgetting their fitness profile.')

def _document(owner):
    if not isinstance(owner,str) or not owner: raise ProfileError('A signed-in owner is required.')
    try:
        return chat_memory._database().collection('kyrex_fitness_profiles').document(hashlib.sha256(owner.encode()).hexdigest())
    except chat_memory.MemoryError as exc:
        raise ProfileError(str(exc).replace('Memory','Fitness profile').replace('memory','fitness profile')) from None

def get(owner):
    try:
        snapshot = _document(owner).get(timeout=TIMEOUT,retry=None)
        if not snapshot.exists: return {}
        data = snapshot.to_dict()
        result = {}
        for field in FIELDS:
            if field not in data: continue
            saved = connectors.unseal_tokens(data[field])
            if 'value' not in saved: raise ProfileError('Fitness profile could not be read. Retry or reset the profile.')
            if saved['value'] is not None:
                try: result[field] = validate({field:saved['value']})[field]
                except ProfileValidationError: raise ProfileError('Fitness profile could not be read. Retry or reset the profile.') from None
        if isinstance(data.get('updated_at'),str): result['updated_at'] = data['updated_at']
        return result
    except ProfileError: raise
    except Exception: raise ProfileError('Fitness profile is temporarily unavailable.') from None

def update(owner, values, owner_text, question=''):
    values = authorize_update(values,owner_text,question)
    try:
        fields = {field:connectors.seal_tokens({'value':value}) for field,value in values.items()}
        fields['updated_at'] = datetime.now(timezone.utc).isoformat()
        # Each field is encrypted separately so partial saves merge atomically;
        # concurrent age/weight edits cannot overwrite other profile fields.
        _document(owner).set(fields,merge=True,timeout=TIMEOUT,retry=None)
        try: return get(owner)
        except ProfileError: raise ProfileError('Fitness profile update could not be confirmed. Read the profile before retrying.') from None
    except ProfileError: raise
    except Exception: raise ProfileError('Fitness profile update could not be confirmed. Read the profile before retrying.') from None

def clear(owner, owner_text):
    authorize_clear(owner_text)
    try:
        _document(owner).delete(timeout=TIMEOUT,retry=None)
        return {}
    except ProfileError: raise
    except Exception: raise ProfileError('Fitness profile deletion could not be confirmed. Read the profile before retrying.') from None

def read_status(owner):
    try: return {'status':'ok','profile':get(owner)}
    except ProfileError as exc: return {'status':'unavailable','profile':{},'error':str(exc)}
