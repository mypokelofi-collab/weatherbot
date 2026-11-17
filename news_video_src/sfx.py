import json, numpy as np, soundfile as sf
from scipy.signal import lfilter, butter
tl=json.load(open("timeline.json")); S={s["key"]:s for s in tl["segs"]}
SR=44100; N=int(SR*tl["dur"]); out=np.zeros(N); rng=np.random.default_rng(2)
def add(t,x,g=1.0):
    i=int(t*SR); j=min(N,i+len(x));
    if i<N: out[i:j]+=g*x[:j-i]
def bp(x,lo,hi): b,a=butter(2,[lo/(SR/2),hi/(SR/2)],"band"); return lfilter(b,a,x)
def lp(x,f): b,a=butter(2,f/(SR/2)); return lfilter(b,a,x)
def env(n,a,r): e=np.ones(n); ai=int(a*SR); ri=int(r*SR); e[:ai]=np.linspace(0,1,ai); e[-ri:]=np.linspace(1,0,ri); return e
T=lambda d:np.arange(int(d*SR))/SR
# radar: alarm beeps
r0=S["iran"]["start"]
for k in range(int((20.4-r0)*2.2)):
    tt=T(0.12); add(r0+k/2.2,np.sin(2*np.pi*880*tt)*np.exp(-tt*10),0.05)
# distant rumble under radar scene
d=20.4-r0; add(r0,lp(rng.standard_normal(int(d*SR)),120)*env(int(d*SR),1,1.5),0.35)
# carrier jet launch
L=20.4+2.6; n=int(3.0*SR); nz=rng.standard_normal(n); tt=T(3.0)
roar=bp(nz,200,3000)*np.minimum(1,tt*3)*np.exp(-np.maximum(0,tt-1.2)*1.5)
add(L-0.2,roar,0.35); add(L,lp(rng.standard_normal(int(0.4*SR)),300)*np.exp(-T(0.4)*8),0.6)
# DC: two-tone siren, low
d0=S["dc"]["start"]; tt=T(9.0); f=np.where((tt*1.2)%1<0.5,650,850); ph=np.cumsum(f)/SR
add(d0+4.5,np.sign(np.sin(2*np.pi*ph))*0.5*env(len(tt),1,2),0.035)
# helicopter thump
tt=T(5.0); thump=(np.sin(2*np.pi*14*tt)>0.6).astype(float); hel=lp(rng.standard_normal(len(tt)),400)*thump*env(len(tt),1.2,1.5)
add(d0+9.2,hel,0.5)
# Asia: plane approach roar + tire screech
a0=S["asia"]["start"]; tt=T(7.0); add(a0+0.5,bp(rng.standard_normal(len(tt)),150,2500)*env(len(tt),3,2.5),0.18)
tt=T(0.6); sc=np.sin(2*np.pi*(1800+200*np.sin(tt*30))*tt)*np.exp(-tt*4)
add(a0+5.0,sc,0.05); add(a0+5.05,sc,0.04)
# Outage: split-flap clicks then error beeps
o0=S["outage"]["start"]
for i in range(6):
    for k in range(10):
        cl=rng.standard_normal(int(0.008*SR))*np.exp(-T(0.008)*600); add(o0+1.0+i*0.7+k*0.03,bp(cl,1500,6000),0.25)
for k in range(3):
    tt=T(0.18); add(o0+8.3+k*0.35,np.sign(np.sin(2*np.pi*440*tt))*np.exp(-tt*6),0.05)
# Exam: clock ticks + pencil scratches
e0=S["korea"]["start"]
for k in range(int(S["korea"]["end"]-e0)):
    cl=bp(rng.standard_normal(int(0.01*SR))*np.exp(-T(0.01)*500),2000,8000); add(e0+k+0.02,cl,0.25)
tt=T(4.0); add(e0+7.5,bp(rng.standard_normal(len(tt)),3000,9000)*(0.5+0.5*np.sin(2*np.pi*7*tt))*env(len(tt),0.3,0.5),0.04)
# Film: clapper snap
f0=S["bond"]["start"]; add(f0+4.4,bp(rng.standard_normal(int(0.08*SR))*np.exp(-T(0.08)*80),800,6000),0.9)
out=np.clip(out,-1,1)
sf.write("sfx.wav",np.stack([out,out],1).astype(np.float32),SR); print("ok",np.abs(out).max())
