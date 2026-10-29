"""Owner text requests create exact-sender previews, never approve them."""
import re
import time
from email.utils import getaddresses
import gmail_cleanup as cleanup

INTENT = re.compile(r'^(?:please\s+)?(?:(?:can|could|would) you\s+)?(?:delete|trash|remove)\s+(?:all\s+)?(?:the\s+)?(?:e-?mails?|messages)\s+from\s+(.+?)[.!?]?$',re.I)


def target(text, conv):
    match = INTENT.fullmatch(str(text or '').strip())
    if match: return match[1].strip()
    pending = conv.get('gmail_cleanup_pending') or {}
    latest = next((m for m in reversed(conv.get('messages',[])) if m.get('role')=='assistant'),{})
    if pending.get('assistant') == latest.get('id') and time.time()-pending.get('created',0)<1800:
        try: return cleanup.address(str(text).strip())
        except ValueError: pass
    return None


def resolve_sender(store, owner, value):
    try: return cleanup.address(value),[]
    except ValueError: pass
    if not re.fullmatch(r'[A-Za-z0-9 .&_-]{2,80}',value):
        raise ValueError('Provide the exact sender email address for the cleanup preview.')
    _,token = store.auth(owner)
    page = cleanup.transport('GET',cleanup.API+'/messages',token,
        {'q':'"'+value+'" -in:trash -in:spam -in:sent -in:drafts','maxResults':30})
    found = set()
    for hit in (page.get('messages') or [])[:30]:
        identity = hit.get('id')
        if not isinstance(identity,str) or not re.fullmatch('[A-Za-z0-9_-]{1,512}',identity): raise ValueError('Invalid email lookup.')
        result = cleanup.transport('GET',cleanup.API+'/messages/'+identity,token,{'format':'metadata','metadataHeaders':['From']})
        headers = result.get('payload',{}).get('headers',[])
        values = [h.get('value','') for h in headers if str(h.get('name','')).lower()=='from']
        if result.get('id')!=identity or len(values)!=1: continue
        senders = getaddresses(values)
        if len(senders)!=1: continue
        name,sender = senders[0]
        normalize = lambda s: re.sub('[^a-z0-9]','',s.lower())
        if normalize(value) not in normalize(name+' '+sender): continue
        try: found.add(cleanup.address(sender))
        except ValueError: pass
    return '',sorted(found)


async def stream(chat, owner, conv, text, target_value, request_id, cancel, workspace_id):
    import asyncio
    identity = None
    try:
        if workspace_id not in (chat._WORKSPACE_UNSET,None,''):
            raise ValueError('Email cleanup is unavailable in repository workspace chat.')
        bot_id = conv.get('bot_id','')
        if bot_id: chat.resolve_bot_for_user(owner,bot_id)
        cleanup.bot_allowed(owner,bot_id)
        store = cleanup.CleanupStore(chat._task_store())
        exact,senders = await asyncio.to_thread(resolve_sender,store,owner,target_value)
        if not exact:
            answer = ('Which exact sender should I clean up? Reply with one email address. '
                      + ('Observed senders: '+', '.join(senders)+'.' if senders else 'No exact sender was resolved from the available metadata.'))
        else:
            if cancel is not None and cancel.is_set():
                yield {'type':'status','status':'cancelled','content':''}; return
            review = await asyncio.to_thread(store.create,owner,conv['conversation_id'],bot_id,exact,
                                            f"{conv['conversation_id']}:{request_id}" if request_id else None)
            identity = review['id']
            answer = 'Preparing an email cleanup preview. Review the sender, mailbox and exact message list before approving Trash.'
    except (ValueError,cleanup.connectors.ConnectorError) as exc: answer = str(exc)
    except Exception: answer = 'Email cleanup could not be prepared. No email was moved.'
    conv = chat.get_conversation(owner,conv['conversation_id']) or conv
    chat._append_message(owner,conv,'user',text,identity=f'turn-{request_id}-user' if request_id else None)
    reply = chat._append_message(owner,conv,'assistant',answer,identity=f'turn-{request_id}-assistant' if request_id else None)
    if identity:
        reply['gmail_cleanup'] = {'id':identity}; conv.pop('gmail_cleanup_pending',None)
    elif 'Reply with one email address' in answer or 'reply with one email address' in answer:
        conv['gmail_cleanup_pending'] = {'assistant':reply['id'],'created':time.time()}
    chat._write(owner,conv)
    yield {'type':'conversation','conversation_id':conv['conversation_id']}
    yield {'type':'status','status':'complete','content':answer,**({'gmail_cleanup':{'id':identity}} if identity else {})}
