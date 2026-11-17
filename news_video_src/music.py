import json, numpy as np, soundfile as sf, sys
D=sys.argv[1]; tl=json.load(open(D+"/timeline.json")); SR=44100; dur=tl["dur"]; N=int(SR*dur)
t=np.arange(N)/SR; m=np.zeros(N); rng=np.random.default_rng(1)
bpm=112; beat=60/bpm
# chord progression (Am - F - C - G) bass + pad
roots=[110.0,87.31,130.81,98.0]; thirds=[[1,1.189,1.498],[1,1.26,1.498],[1,1.26,1.498],[1,1.26,1.498]]
bar=beat*4
idx=((t//(bar*2))%4).astype(int)
for i in range(4):
    mask=idx==i; f=roots[i]
    for r in thirds[i]:
        for det in (0.997,1.003):
            m[mask]+=0.05*np.sign(np.sin(2*np.pi*f*2*r*det*t[mask]))*0.5
    m[mask]+=0.18*np.sin(2*np.pi*f/2*t[mask])
# lowpass pad (simple one-pole)
def lp(x,a):
    y=np.empty_like(x); s=0.0
    from scipy.signal import lfilter
    return lfilter([1-a],[1,-a],x)
m=lp(m,0.96)
# pulse: 16th-note ticks + kick on beats
def env(n,k): return np.exp(-np.arange(n)/SR*k)
nb=int(dur/beat)
for b in range(nb):
    s=int(b*beat*SR); n=int(0.25*SR)
    if s+n>N: break
    kt=np.arange(n)/SR; f=55+90*np.exp(-kt*30)
    m[s:s+n]+=0.45*np.sin(2*np.pi*np.cumsum(f)/SR)*env(n,9)
    for q in range(4):
        s2=s+int(q*beat/4*SR); n2=int(0.03*SR)
        if s2+n2<N: m[s2:s2+n2]+=(0.05 if q else 0.08)*rng.standard_normal(n2)*env(n2,150)
    if b%2==1:  # snare-ish
        n3=int(0.15*SR); m[s:s+n3]+=0.12*rng.standard_normal(n3)*env(n3,25)
# arpeggio pinging
for b in range(nb*2):
    s=int(b*beat/2*SR); n=int(0.2*SR); i=int((b*beat/2)//(bar*2))%4
    if s+n>N: break
    f=roots[i]*4*[1,1.5,2,1.5][b%4]; tt=np.arange(n)/SR
    m[s:s+n]+=0.05*np.sin(2*np.pi*f*tt)*env(n,14)
m*=0.55
# SFX: whoosh + impact at each segment start
for sg in tl["segs"]:
    s=int(max(sg["start"]-0.5,0)*SR); n=int(0.6*SR)
    if s+n>N: continue
    nz=rng.standard_normal(n); sweep=np.linspace(0.995,0.6,n)
    y=np.zeros(n); acc=0.0
    for j in range(n): acc=sweep[j]*acc+(1-sweep[j])*nz[j]; y[j]=acc
    m[s:s+n]+=1.6*y*np.sin(np.linspace(0,np.pi,n))**2
    s2=s+n-int(0.05*SR); n2=int(1.2*SR)
    if s2+n2<N:
        tt=np.arange(n2)/SR
        m[s2:s2+n2]+=0.5*np.sin(2*np.pi*(45+60*np.exp(-tt*8))*tt)*env(n2,3)
# intro stinger: big hit + bell
for f in (440,554.37,659.25,880):
    m[:int(2.5*SR)]+=0.08*np.sin(2*np.pi*f*t[:int(2.5*SR)])*env(int(2.5*SR),1.8)
# fades
fi=int(0.05*SR); fo=int(3*SR); m[:fi]*=np.linspace(0,1,fi); m[-fo:]*=np.linspace(1,0,fo)
m/=np.max(np.abs(m))*1.05
sf.write(D+"/music.wav",np.stack([m,m],1).astype(np.float32),SR)
