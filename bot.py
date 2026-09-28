import os,time,json,hmac,hashlib,logging,threading,uuid,atexit
from decimal import Decimal,ROUND_DOWN
from datetime import datetime,timedelta,time as dtime
from zoneinfo import ZoneInfo
from urllib.parse import urlencode,parse_qs,urlparse
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
import requests,websocket
from dotenv import load_dotenv
try: import fcntl
except ImportError: fcntl=None
load_dotenv()
IST=ZoneInfo('Asia/Kolkata'); BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DATA=os.getenv('RAILWAY_VOLUME_MOUNT_PATH',BASE_DIR); BASE=os.getenv('DELTA_BASE_URL','https://api.india.delta.exchange').rstrip('/'); WS=os.getenv('DELTA_PUBLIC_WS_URL','wss://public-socket.india.delta.exchange'); PORT=int(os.getenv('PORT') or os.getenv('DASHBOARD_PORT') or 8000)
STATE=os.path.join(DATA,'account_states'); HIST=os.path.join(DATA,'account_history'); CLIENTS=os.path.join(DATA,'clients_config.json'); LOCK=os.path.join(DATA,'delta_bot_process.lock'); os.makedirs(STATE,exist_ok=True); os.makedirs(HIST,exist_ok=True)
SYMBOL='XAUTUSD'; SESSION=dtime(5,30); START=dtime(5,45); LEVS=range(100,9,-10); PARTS=10; FRACTION=Decimal('0.10'); RECONNECT=5
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s',force=True); LOCK_HANDLE=None; SERVER_IP='Detecting...'
def now(): return datetime.now(IST)
def weekend(t=None):
 t=t or now(); return (t.weekday()==5 and t.time()>=SESSION) or t.weekday()==6 or (t.weekday()==0 and t.time()<SESSION)
def sess(t=None):
 t=t or now(); x=t.replace(hour=5,minute=30,second=0,microsecond=0); return x if t.time()>=SESSION else x-timedelta(days=1)
def af(v,d=None):
 try:return float(v)
 except:return d
def ai(v,d=0):
 try:return int(v)
 except:return d
def sf(v): return ''.join(c if c.isalnum() or c in '-_' else '_' for c in str(v)) or 'account'
def sfile(i):return os.path.join(STATE,sf(i)+'.json')
def hfile(i):return os.path.join(HIST,sf(i)+'.json')
def writej(p,d):
 q=p+'.tmp'; open(q,'w',encoding='utf8').write(json.dumps(d,indent=2,default=str)); os.replace(q,p)
def clients():
 try:return json.load(open(CLIENTS,encoding='utf8')) if os.path.exists(CLIENTS) else {}
 except:return {}
def ip():
 global SERVER_IP
 try: SERVER_IP=requests.get('https://api.ipify.org?format=json',timeout=5).json().get('ip') or SERVER_IP; logging.warning('RAILWAY OUTBOUND IP --> %s',SERVER_IP)
 except Exception as e:logging.warning('IP ERROR %s',e)
def lock():
 global LOCK_HANDLE
 if fcntl is None:return True
 LOCK_HANDLE=open(LOCK,'w');
 try:fcntl.flock(LOCK_HANDLE.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError: logging.critical('ANOTHER BOT PROCESS IS RUNNING'); LOCK_HANDLE.close(); LOCK_HANDLE=None; return False
 LOCK_HANDLE.write(str(os.getpid()));LOCK_HANDLE.flush();atexit.register(unlock);return True
def unlock():
 global LOCK_HANDLE
 if LOCK_HANDLE:
  try:fcntl.flock(LOCK_HANDLE.fileno(),fcntl.LOCK_UN)
  except:pass
  try:LOCK_HANDLE.close()
  except:pass
  LOCK_HANDLE=None
class Delta:
 def __init__(self,key,secret,name):
  self.key=key;self.secret=secret;self.name=name;self.s=requests.Session();self.s.headers.update({'Content-Type':'application/json','Accept':'application/json','User-Agent':'MultiBot/100.0'})
 def api(self,m,path,params=None,body=None,auth=False):
  bt=json.dumps(body,separators=(',',':')) if body is not None else ''; q='?'+urlencode(params,doseq=True) if params else ''; ts=str(int(time.time())); h={}
  if auth:
   sig=hmac.new(self.secret.encode(),(m.upper()+ts+path+q+bt).encode(),hashlib.sha256).hexdigest();h={'api-key':self.key,'signature':sig,'timestamp':ts,'User-Agent':'MultiBot/100.0'}
  r=self.s.request(m.upper(),BASE+path,params=params,data=bt if body is not None else None,headers=h,timeout=(3,8));r.raise_for_status();d=r.json()
  if d.get('success') is False:raise RuntimeError(d)
  return d
 def product(self):return self.api('GET',f'/v2/products/{SYMBOL}').get('result')
 def pos(self,pid):
  r=self.api('GET','/v2/positions',{'product_id':int(pid)},auth=True).get('result',{}); p=r if isinstance(r,dict) else next((x for x in r if isinstance(x,dict) and ai(x.get('product_id'))==int(pid)),{})
  return {'size':ai(p.get('size')),'entry_price':af(p.get('entry_price') or p.get('avg_price')),'leverage':ai(p.get('leverage') or p.get('user_leverage'),0) or None,'liquidation_price':af(p.get('liquidation_price')),'mark_price':af(p.get('mark_price')),'margin':af(p.get('margin')),'unrealized_pnl':af(p.get('unrealized_pnl'),0)}
 def balance(self):
  r=self.api('GET','/v2/wallet/balances',auth=True).get('result',[]);r=[r] if isinstance(r,dict) else r
  for x in r:
   if str(x.get('asset_symbol','')).upper() in ('USD','USDT'):return Decimal(str(x.get('available_balance') or x.get('balance')))
  raise RuntimeError('USD/USDT balance not found')
 def lev(self,pid,x):return self.api('POST',f'/v2/products/{pid}/orders/leverage',body={'leverage':str(x)},auth=True)
 def size(self,p,price,lev):
  cv=Decimal(str(p.get('contract_value') or p.get('contract_value_usd') or '0.001'));inc=Decimal(str(p.get('lot_size') or p.get('order_size_increment') or '1'));mn=Decimal(str(p.get('min_order_size') or p.get('minimum_order_size') or inc));raw=self.balance()*FRACTION*lev/price/cv;n=(raw/inc).to_integral_value(rounding=ROUND_DOWN)*inc;return max(int(n),int(mn))
 def oid(self,x):return f'{x}_{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}'[-32:]
 def market(self,pid,side,size,reduce=False):return self.api('POST','/v2/orders',body={'product_id':int(pid),'product_symbol':SYMBOL,'size':int(abs(size)),'side':side,'order_type':'market_order','reduce_only':reduce,'client_order_id':self.oid('ord')},auth=True)
 def cancel(self,pid):
  try:return self.api('DELETE','/v2/orders/all',body={'product_id':int(pid)},auth=True)
  except:return None
 def ticker(self):
  try:return Decimal(str(self.api('GET',f'/v2/tickers/{SYMBOL}').get('result',{}).get('close')))
  except:return None
 def hl(self,start):
  try:
   d=self.api('GET','/v2/history/candles',{'resolution':'1m','symbol':SYMBOL,'start':int(start.timestamp()),'end':int(now().timestamp())}).get('result',[]);hi=lo=None
   for c in d:
    if isinstance(c,dict):h=af(c.get('high'));l=af(c.get('low'))
    elif isinstance(c,list) and len(c)>=4:h=af(c[2]);l=af(c[3])
    else:continue
    if h is not None:hi=Decimal(str(h)) if hi is None or h>float(hi) else hi
    if l is not None:lo=Decimal(str(l)) if lo is None or l<float(lo) else lo
   return hi,lo
  except Exception:return None,None
class Bot:
 def __init__(self,aid,name,typ,key,secret,sub=None):
  self.aid=aid;self.name=name;self.typ=typ;self.sub=sub or {};self.id=f'{aid}_{SYMBOL}_breakout_targets';self.client=Delta(key,secret,name);self.product=None;self.pid=0;self.session=None;self.high=None;self.low=None;self.enabled=False;self.armed=False;self.pos=None;self.entry=None;self.sizev=0;self.remaining=0;self.sl=0.;self.risk=0.;self.lev=100;self.targets=[0.]*PARTS;self.hit=[False]*PARTS;self.parts=[0]*PARTS;self.fresh=False;self.old_high=None;self.old_low=None;self.uncertain=False;self.order=False;self.last_recon=0;self.stop_reason=None;self.load();self.save()
 def expired(self):
  e=self.sub.get('expiry');
  try:return self.typ!='primary' and e and now().date()>datetime.strptime(e,'%Y-%m-%d').date()
  except:return False
 def load(self):
  if not os.path.exists(sfile(self.id)):return
  try:
   d=json.load(open(sfile(self.id),encoding='utf8'));self.session=datetime.fromisoformat(d['session']) if d.get('session') else None;self.high=Decimal(str(d['high'])) if d.get('high') is not None else None;self.low=Decimal(str(d['low'])) if d.get('low') is not None else None;self.pos=d.get('pos');self.entry=d.get('entry');self.sizev=ai(d.get('size'));self.remaining=ai(d.get('remaining'));self.sl=af(d.get('sl'),0);self.risk=af(d.get('risk'),0);self.lev=ai(d.get('lev'),100);self.targets=[af(x,0) for x in d.get('targets',[0]*PARTS)][:PARTS];self.targets+= [0.]*(PARTS-len(self.targets));self.hit=[bool(x) for x in d.get('hit',[False]*PARTS)][:PARTS];self.hit += [False]*(PARTS-len(self.hit));self.parts=[ai(x) for x in d.get('parts',[0]*PARTS)][:PARTS];self.parts += [0]*(PARTS-len(self.parts));self.fresh=bool(d.get('fresh'));self.old_high=Decimal(str(d['old_high'])) if d.get('old_high') is not None else None;self.old_low=Decimal(str(d['old_low'])) if d.get('old_low') is not None else None;self.enabled=bool(d.get('enabled'));self.armed=bool(d.get('armed'));self.uncertain=bool(d.get('uncertain'))
  except Exception as e:logging.warning('[%s] STATE LOAD %s',SYMBOL,e)
 def save(self):writej(sfile(self.id),{'session':self.session.isoformat() if self.session else None,'high':str(self.high) if self.high is not None else None,'low':str(self.low) if self.low is not None else None,'pos':self.pos,'entry':self.entry,'size':self.sizev,'remaining':self.remaining,'sl':self.sl,'risk':self.risk,'lev':self.lev,'targets':self.targets,'hit':self.hit,'parts':self.parts,'fresh':self.fresh,'old_high':str(self.old_high) if self.old_high is not None else None,'old_low':str(self.old_low) if self.old_low is not None else None,'enabled':self.enabled,'armed':self.armed,'uncertain':self.uncertain})
 def product_ready(self):
  if self.pid:return True
  try:self.product=self.client.product();self.pid=int(self.product['id']);return True
  except Exception as e:logging.error('[%s] PRODUCT %s',SYMBOL,e);return False
 def reconcile(self):
  if not self.product_ready():return False
  try:p=self.client.pos(self.pid)
  except:self.uncertain=True;self.save();return False
  if p['size']:
   self.pos='LONG' if p['size']>0 else 'SHORT';self.sizev=abs(p['size']);self.remaining=self.sizev;self.entry=p['entry_price'] or self.entry;self.uncertain=False
  else:
   self.pos=None;self.entry=None;self.sizev=0;self.remaining=0;self.uncertain=False
  self.last_recon=time.time();self.save();return True
 def start(self):
  if self.expired():return {'success':False,'message':'Subscription expired.'}
  if not self.product_ready() or not self.reconcile():self.enabled=False;return {'success':False,'message':'Exchange reconciliation failed.'}
  self.enabled=True;self.stop_reason=None;self.save();return {'success':True,'bot_enabled':True,'message':'XAUTUSD Bot Started.'}
 def waitflat(self):
  end=time.time()+10
  while time.time()<end:
   try:
    if self.client.pos(self.pid)['size']==0:return True
   except:pass
   time.sleep(.25)
  return False
 def stop(self):
  self.enabled=False;self.stop_reason='MANUAL STOP'
  try:
   if self.product_ready():
    p=self.client.pos(self.pid)
    if p['size']:
     self.client.cancel(self.pid);self.client.market(self.pid,'sell' if p['size']>0 else 'buy',abs(p['size']),True)
     if not self.waitflat():self.uncertain=True;self.save();return {'success':False,'message':'Exchange did not confirm flat.'}
   self.pos=None;self.entry=None;self.sizev=0;self.remaining=0;self.sl=0;self.save();return {'success':True,'bot_enabled':False,'message':'XAUTUSD Bot Stopped.'}
  except Exception as e:self.uncertain=True;self.save();return {'success':False,'message':str(e)}
 def session_change(self,t):
  s=sess(t)
  if self.session==s:return True
  if self.product_ready():
   try:
    p=self.client.pos(self.pid)
    if p['size']:
     self.client.cancel(self.pid);self.client.market(self.pid,'sell' if p['size']>0 else 'buy',abs(p['size']),True)
     if not self.waitflat():self.uncertain=True;self.save();return False
   except:self.uncertain=True;self.save();return False
  self.session=s;self.high,self.low=self.client.hl(s);self.pos=None;self.entry=None;self.sizev=0;self.remaining=0;self.sl=0;self.risk=0;self.targets=[0.]*PARTS;self.hit=[False]*PARTS;self.parts=[0]*PARTS;self.fresh=False;self.old_high=None;self.old_low=None;self.armed=False;self.uncertain=False;self.save();return True
 def liq(self,entry,lev,direction):
  mm=Decimal(str(self.product.get('maintenance_margin',0) or 0))/100;fee=Decimal(str(self.product.get('taker_commission_rate',0) or 0));mm+=fee+Decimal('.0010');e=Decimal(str(entry));l=Decimal(str(lev));return e*(1-1/l+mm) if direction=='LONG' else e*(1+1/l-mm)
 def chooselev(self,entry,sl,direction):
  for x in LEVS:
   q=self.liq(entry,x,direction)
   if (direction=='LONG' and q<Decimal(str(sl))) or (direction=='SHORT' and q>Decimal(str(sl))):return x,q
  return None,None
 def targets_for(self,d,e,sl):
  e=Decimal(str(e));sl=Decimal(str(sl));r=e-sl if d=='LONG' else sl-e
  if r<=0:return None,None
  return float(r),[float(e+r*i) if d=='LONG' else float(e-r*i) for i in range(1,PARTS+1)]
 def enter(self,d,price,sl):
  if self.order or self.uncertain:return False
  if (d=='LONG' and sl>=price) or (d=='SHORT' and sl<=price):return False
  try:
   if self.client.pos(self.pid)['size']:self.reconcile();return False
   risk,targets=self.targets_for(d,price,sl);lev,liq=self.chooselev(price,sl,d)
   if lev is None:logging.warning('[%s] NO SAFE LEVERAGE 100x-10x',SYMBOL);return False
   self.order=True;self.client.lev(self.pid,lev);sz=self.client.size(self.product,Decimal(str(price)),Decimal(str(lev)));self.client.market(self.pid,'buy' if d=='LONG' else 'sell',sz);end=time.time()+10;p=None
   while time.time()<end:
    p=self.client.pos(self.pid)
    if (d=='LONG' and p['size']>0) or (d=='SHORT' and p['size']<0):break
    time.sleep(.25)
   if not p or (d=='LONG' and p['size']<=0) or (d=='SHORT' and p['size']>=0):self.uncertain=True;self.save();return False
   actual=p['entry_price'] or price;risk,targets=self.targets_for(d,actual,sl);self.pos=d;self.entry=actual;self.sizev=abs(p['size']);self.remaining=self.sizev;self.sl=float(sl);self.risk=risk;self.lev=lev;self.targets=targets;self.hit=[False]*PARTS;base=self.sizev//PARTS;rem=self.sizev%PARTS;self.parts=[base+(1 if i<rem else 0) for i in range(PARTS)];self.fresh=False;self.old_high=None;self.old_low=None;self.save();logging.info('[%s] ENTRY %s Entry=%s Size=%s Lev=%sx SL=%s Liq=%s',SYMBOL,d,self.entry,self.sizev,self.lev,self.sl,liq);return True
  except Exception as e:logging.error('[%s] ENTRY %s',SYMBOL,e);return False
  finally:self.order=False
 def record(self,reason,exit_price,size):
  if not self.entry:return
  cv=Decimal(str(self.product.get('contract_value') or self.product.get('contract_value_usd') or '.001'));p=(Decimal(str(exit_price))-Decimal(str(self.entry)))*Decimal(size)*cv if self.pos=='LONG' else (Decimal(str(self.entry))-Decimal(str(exit_price)))*Decimal(size)*cv;h=json.load(open(hfile(self.id),encoding='utf8')) if os.path.exists(hfile(self.id)) else [];h.append({'id':uuid.uuid4().hex,'date':now().strftime('%Y-%m-%d %H:%M'),'account':self.name,'symbol':SYMBOL,'direction':self.pos,'entry_price':self.entry,'exit_price':float(exit_price),'size':int(size),'pnl':float(p),'reason':reason,'leverage':self.lev});writej(hfile(self.id),h)
 def target(self,i,price):
  if self.hit[i] or self.remaining<=0 or self.order:return False
  close=self.remaining if i==PARTS-1 else min(self.parts[i],self.remaining)
  if close<=0:self.hit[i]=True;self.save();return False
  self.order=True
  try:
   self.client.market(self.pid,'sell' if self.pos=='LONG' else 'buy',close,True);end=time.time()+10;new=None
   while time.time()<end:
    new=abs(self.client.pos(self.pid)['size'])
    if new<=self.remaining-close:break
    time.sleep(.25)
   if new is None or new>self.remaining-close:self.uncertain=True;self.save();return False
   self.hit[i]=True;closed=self.remaining-new;self.remaining=new;self.sizev=new;self.record(f'TARGET_{i+1}',price,closed)
   if new==0:self.flat('ALL_TARGETS_HIT',price)
   self.save();return True
  except Exception as e:self.uncertain=True;self.save();logging.error('[%s] TARGET %s %s',SYMBOL,i+1,e);return False
  finally:self.order=False
 def flat(self,reason,price):
  old=self.pos;old_entry=self.entry;old_size=self.sizev
  if old and old_entry and reason not in [f'TARGET_{i}' for i in range(1,11)]:self.record(reason,price,old_size)
  self.pos=None;self.entry=None;self.sizev=0;self.remaining=0;self.sl=0;self.risk=0;self.targets=[0.]*PARTS;self.hit=[False]*PARTS;self.parts=[0]*PARTS
  self.fresh=True;self.old_high=self.high;self.old_low=self.low;self.save();logging.info('[%s] FLAT -> WAIT FOR NEW HIGH/LOW BEYOND OLD RANGE | High=%s Low=%s',SYMBOL,self.old_high,self.old_low)
 def slcheck(self,price):
  if self.pos=='LONG' and price<=self.sl:return self.closeall('DAY_LOW_SL',price)
  if self.pos=='SHORT' and price>=self.sl:return self.closeall('DAY_HIGH_SL',price)
  return False
 def closeall(self,reason,price):
  if not self.pos or self.order:return False
  self.order=True
  try:
   p=self.client.pos(self.pid)
   if p['size']:
    self.client.cancel(self.pid);self.client.market(self.pid,'sell' if p['size']>0 else 'buy',abs(p['size']),True)
    if not self.waitflat():self.uncertain=True;self.save();return False
   self.record(reason,price,self.remaining);self.flat(reason,price);return True
  except Exception as e:self.uncertain=True;self.save();logging.error('[%s] SL CLOSE %s',SYMBOL,e);return False
  finally:self.order=False
 def eval(self,price):
  with threading.RLock():
   if not self.enabled or self.expired():return
   t=now()
   if weekend():
    if self.pos:self.closeall('WEEKEND_CLOSE',price)
    return
   if self.uncertain:
    if time.time()-self.last_recon>=5:self.reconcile()
    return
   if not self.session_change(t):return
   if self.high is None or self.low is None:return
   if t.time()<START:self.armed=False;return
   if not self.armed:self.armed=True;self.save();return
   price=float(price)
   if self.pos:
    if self.slcheck(price):return
    if self.pos=='LONG':
     for i in range(PARTS):
      if not self.hit[i] and price>=self.targets[i]:self.target(i,price);break
    else:
     for i in range(PARTS):
      if not self.hit[i] and price<=self.targets[i]:self.target(i,price);break
    return
   # No old breakout reuse after a completed trade.
   if self.fresh:
    oh=float(self.old_high if self.old_high is not None else self.high);ol=float(self.old_low if self.old_low is not None else self.low)
    if price>oh:
     self.high=Decimal(str(price));self.enter('LONG',price,float(self.low));return
    if price<ol:
     self.low=Decimal(str(price));self.enter('SHORT',price,float(self.high));return
    return
   if price>float(self.high):self.high=Decimal(str(price));self.enter('LONG',price,float(self.low));return
   if price<float(self.low):self.low=Decimal(str(price));self.enter('SHORT',price,float(self.high));return
   if price>float(self.high):self.high=Decimal(str(price))
   if price<float(self.low):self.low=Decimal(str(price))
   self.save()
 def data(self):
  try:p=self.client.pos(self.pid)
  except:p={'size':self.sizev,'entry_price':self.entry,'leverage':self.lev,'liquidation_price':None,'mark_price':None,'margin':None,'unrealized_pnl':0}
  h=json.load(open(hfile(self.id),encoding='utf8')) if os.path.exists(hfile(self.id)) else []
  return {'id':self.id,'account_id':self.aid,'account_name':self.name,'symbol':SYMBOL,'strategy':'Day High/Low Breakout + 10 Targets','bot_enabled':self.enabled,'session_start':self.session.isoformat() if self.session else None,'day_high':float(self.high) if self.high is not None else None,'day_low':float(self.low) if self.low is not None else None,'position':'LONG' if p['size']>0 else ('SHORT' if p['size']<0 else None),'position_size':abs(p['size']),'entry_price':p.get('entry_price'),'strategy_stop_loss':self.sl,'leverage':p.get('leverage') or self.lev,'liquidation_price':p.get('liquidation_price'),'mark_price':p.get('mark_price'),'margin':p.get('margin'),'unrealized_pnl':p.get('unrealized_pnl',0),'trade_risk':self.risk,'remaining_size':self.remaining,'waiting_for_fresh_range':self.fresh,'targets':[{'number':i+1,'rr':f'1:{i+1}','price':self.targets[i],'hit':self.hit[i],'part_size':self.parts[i]} for i in range(PARTS)],'execution_uncertain':self.uncertain,'server_ip':SERVER_IP,'history':h[-100:]}
BOTS={};BL=threading.RLock()
def loadbots():
 global BOTS
 x={};key=os.getenv('DELTA_API_KEY','').strip();sec=os.getenv('DELTA_API_SECRET','').strip()
 if key and sec:
  b=Bot(os.getenv('ACCOUNT_ID','primary'),os.getenv('ACCOUNT_NAME','Primary Account'),'primary',key,sec);x[b.id]=b
 for aid,c in clients().items():
  if isinstance(c,dict) and c.get('api_key') and c.get('api_secret'):
   b=Bot(aid,c.get('name','Client'),'client',c['api_key'],c['api_secret'],{'expiry':c.get('subscription_expiry')});x[b.id]=b
 with BL:BOTS=x
def getbot(i):
 with BL:return BOTS.get(i)
def startall():
 with BL:bs=list(BOTS.values())
 for b in bs:
  try:r=b.start();logging.info('[%s] AUTO START %s',SYMBOL,r)
  except Exception:logging.exception('AUTO START')
class Handler(SimpleHTTPRequestHandler):
 def __init__(self,*a,**k):super().__init__(*a,directory=BASE_DIR,**k)
 def log_message(self,f,*a):logging.info('HTTP | '+f,*a)
 def js(self,d,s=200):
  raw=json.dumps(d,default=str).encode();self.send_response(s);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('Access-Control-Allow-Origin','*');self.end_headers();self.wfile.write(raw)
 def body(self):
  try:return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))).decode())
  except:return {}
 def do_GET(self):
  p=urlparse(self.path);q=parse_qs(p.query)
  if p.path=='/api/health':return self.js({'success':True,'online':True,'time':now().isoformat(),'server_ip':SERVER_IP})
  if p.path in ('/api/dashboard','/api/accounts'):
   with BL:bs=list(BOTS.values())
   if p.path=='/api/accounts':return self.js({'success':True,'accounts':[{'id':b.id,'account_id':b.aid,'name':b.name,'symbol':SYMBOL,'enabled':b.enabled} for b in bs]})
   return self.js({'success':True,'time':now().isoformat(),'bots':[b.data() for b in bs]})
  bid=q.get('id',[None])[0];b=getbot(bid) if bid else (next(iter(BOTS.values())) if BOTS else None)
  if p.path=='/api/state':return self.js({'success':bool(b),'bot':b.data() if b else None},200 if b else 404)
  if p.path=='/api/history':
   if not b:return self.js({'success':False,'message':'Bot not found'},404)
   try:h=json.load(open(hfile(b.id),encoding='utf8'))
   except:h=[]
   return self.js({'success':True,'history':h})
  return super().do_GET()
 def do_POST(self):
  p=urlparse(self.path);d=self.body();b=getbot(d.get('id') or d.get('unique_id'))
  if p.path=='/api/start':return self.js(b.start() if b else {'success':False,'message':'Bot not found'},200 if b else 404)
  if p.path=='/api/stop':return self.js(b.stop() if b else {'success':False,'message':'Bot not found'},200 if b else 404)
  if p.path=='/api/reconcile':
   if not b:return self.js({'success':False,'message':'Bot not found'},404)
   ok=b.reconcile();return self.js({'success':ok,'bot':b.data()})
  if p.path=='/api/reload':loadbots();return self.js({'success':True,'message':'Accounts reloaded'})
  if p.path=='/api/settings':return self.js({'success':True,'message':'Leverage is automatic 100x to 10x based on Day SL. Margin 10%.'})
  return self.js({'success':False,'message':'Unknown API endpoint'},404)
 def do_OPTIONS(self):self.send_response(204);self.send_header('Access-Control-Allow-Origin','*');self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS');self.send_header('Access-Control-Allow-Headers','Content-Type');self.end_headers()
def wsopen(ws):ws.send(json.dumps({'type':'subscribe','payload':{'channels':[{'name':'trades','symbols':[SYMBOL]}]}}));logging.info('WEBSOCKET SUBSCRIBED | %s',SYMBOL)
def trade(msg):
 try:d=json.loads(msg)
 except:return None,None
 a=[d];
 if isinstance(d.get('payload'),dict):a.append(d['payload'])
 if isinstance(d.get('data'),dict):a.append(d['data'])
 for x in a:
  if isinstance(x,dict):
   s=x.get('sy') or x.get('symbol') or x.get('product_symbol') or x.get('s');p=x.get('p') or x.get('price') or x.get('last_price') or x.get('close')
   if s and p:
    try:return str(s).upper(),float(p)
    except:return None,None
 return None,None
def wsmsg(ws,msg):
 s,p=trade(msg)
 if s!=SYMBOL:return
 with BL:bs=[b for b in BOTS.values() if b.enabled]
 for b in bs:
  try:b.eval(p)
  except Exception:logging.exception('[%s] EVAL',SYMBOL)
def wsloop():
 while True:
  try:websocket.WebSocketApp(WS,on_open=wsopen,on_message=wsmsg,on_error=lambda w,e:logging.error('WS %s',e),on_close=lambda w,c,m:logging.warning('WS CLOSED %s %s',c,m)).run_forever(ping_interval=20,ping_timeout=10)
  except Exception as e:logging.error('WS LOOP %s',e)
  time.sleep(RECONNECT)
def main():
 if not lock():return
 logging.info('==================================================');logging.info(' XAUTUSD BREAKOUT TARGET BOT');logging.info(' SESSION 05:30 | TRADING 05:45');logging.info(' HIGH BREAK = LONG | LOW BREAK = SHORT');logging.info(' LONG SL = DAY LOW | SHORT SL = DAY HIGH');logging.info(' LEVERAGE AUTO 100x -> 10x | MARGIN 10%%');logging.info(' 10 PARTS | TARGETS 1:1 TO 1:10 | NO REVERSAL');logging.info(' AFTER FLAT: MUST BREAK NEW HIGH/LOW, OLD BREAKOUT REUSED NEVER');logging.info('==================================================');ip();loadbots();startall();threading.Thread(target=wsloop,daemon=True).start();server=ThreadingHTTPServer(('0.0.0.0',PORT),Handler);logging.info('DASHBOARD PORT %s',PORT)
 try:server.serve_forever()
 except KeyboardInterrupt:pass
 finally:server.server_close()
if __name__=='__main__':main()
