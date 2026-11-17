import math, sys, subprocess, numpy as np
sys.path.insert(0,sys.path[0])
from cutout import *
from PIL import Image, ImageDraw
LAUNCH=3.9
HZ=PAD+430  # horizon in canvas coords
def full(fn,top,bot,seed,depth,**kw):
    m=new_mask(); fn(ImageDraw.Draw(m)); sh=kw.pop("shadow",True)
    lk={k:kw.pop(k) for k in ("op","sdist","blur") if k in kw}
    return Layer(paint(m,top,bot,seed,**kw),depth,shadow=sh,**lk)
# sky
sky=full(lambda d:d.rectangle([0,0,CW,CH]),(52,40,92),(250,150,90),1,0.05,shadow=False,rough_amt=0,ang=4,mottle=0.06)
# sun: soft glow painted + disc cutout
glow=Image.new("RGBA",(CW,CH)); gd=ImageDraw.Draw(glow)
for r in range(260,40,-10): gd.ellipse([PAD+880-r,HZ-90-r,PAD+880+r,HZ-90+r],fill=(255,210,140,7))
sunL=Layer(glow,0.06,shadow=False)
sun=full(lambda d:d.ellipse([PAD+835,HZ-140,PAD+925,HZ-50]),(255,245,210),(255,205,140),3,0.07,shadow=False)
def cloud(d,x,y,s):
    for k,(dx,dy,r) in enumerate([(0,0,40),(45,-18,52),(100,-5,44),(140,8,32),(-40,10,30)]):
        d.ellipse([x+dx*s-r*s,y+dy*s-r*s,x+dx*s+r*s,y+dy*s+r*s])
    d.rectangle([x-60*s,y,x+170*s,y+30*s])
cl1=full(lambda d:[cloud(d,PAD+x,PAD+y,s) for x,y,s in ((-500,60,1.0),(80,150,1.1),(700,110,0.9),(1500,40,1.1),(2000,130,0.9))],(255,200,190),(220,130,140),5,0.15,op=0.3)
cl2=full(lambda d:[cloud(d,PAD+x,PAD+y,s) for x,y,s in ((-300,250,0.9),(380,230,0.8),(1060,200,1.2),(1750,260,1.0),(2300,-60,1.3),(900,-120,1.2),(200,-200,1.0))],(250,175,160),(200,110,120),6,0.22,op=0.3)
# far hills (two cutout bands)
def hills(d,base,amp,f,ph):
    pts=[(0,CH)]+[(x,base-amp*(0.6+0.4*math.sin(x/f+ph))-amp*0.3*math.sin(x/(f*0.37)+ph*2)) for x in range(0,CW+10,10)]+[(CW,CH)]
    d.polygon(pts)
h1=full(lambda d:hills(d,HZ-8,38,140,1),(150,90,120),(120,70,100),7,0.25,op=0.35)
h2=full(lambda d:hills(d,HZ+2,22,90,3),(115,65,95),(95,55,85),8,0.32,op=0.4)
# sea base
sea=full(lambda d:d.rectangle([0,HZ,CW,CH]),(175,105,125),(25,40,80),9,0.35,ang=0,mottle=0.1)
# sun reflection streaks painted as small cutouts
refl=Image.new("L",(CW,CH)); rd=ImageDraw.Draw(refl)
for k in range(18):
    y=HZ+8+k*10; w=30+k*7; rd.rounded_rectangle([PAD+880-w/2+(k%3-1)*8,y,PAD+880+w/2,y+4],2)
reflL=Layer(paint(refl,(255,230,170),(255,170,120),10,rough_amt=1),0.38,shadow=False)
# wave strips: scalloped paper bands
def waveband(y,amp,per,ph):
    def f(d):
        pts=[(0,CH)]+[(x,y-amp*abs(math.sin(x/per+ph))) for x in range(0,CW+6,6)]+[(CW,CH)]
        d.polygon(pts)
    return f
cols=[((160,95,130),(60,60,110)),((120,80,130),(40,50,100)),((80,70,125),(30,40,90)),((55,60,115),(20,30,75))]
WY=[HZ+30,HZ+225,HZ+300,HZ+375]
waves=[full(waveband(WY[i],8+i*4,30+i*14,i),cols[i][0],cols[i][1],20+i,0.45+i*0.18,op=0.45,sdist=6+i*3) for i in range(4)]
# escort ships (sprites)
def escort(d):
    d.polygon([(10,60),(250,60),(232,82),(30,82)]); d.rectangle([90,30,160,60]); d.rectangle([110,12,140,30]); d.line([125,0,125,12],fill=255,width=4)
    d.polygon([(170,48),(215,52),(215,60),(170,60)])
esc=paint_local(Image.new("L",(260,90)),(0,0,0),(0,0,0),0,0,0,0)
m=Image.new("L",(260,90)); escort(ImageDraw.Draw(m)); esc=paint_local(m,(80,75,100),(55,50,75),30,0,1.0,0.15)
escL1=Layer(esc.resize((150,52)),0.42,sdist=4,blur=4); escL2=Layer(esc.resize((110,38)),0.40,sdist=3,blur=3)
# carrier sprite (big)
cw_,ch_=820,300
m=Image.new("L",(cw_,ch_)); d=ImageDraw.Draw(m)
d.polygon([(30,180),(760,180),(810,160),(780,215),(720,255),(60,255)])          # hull
d.polygon([(0,165),(820,165),(800,182),(20,182)])                               # flight deck
d.rectangle([470,80,545,170]); d.rectangle([482,45,530,82]); d.line([506,0,506,45],fill=255,width=5); d.line([480,18,532,18],fill=255,width=4)
hull=paint_local(m,(120,125,145),(60,62,80),40,0,1.2,0.15)
dk=ImageDraw.Draw(hull)
dk.line([(60,235),(740,235)],fill=(140,45,50,255),width=8)                       # boot stripe
for i in range(4): dk.rectangle([488+i*14,95,498+i*14,105],fill=(255,215,140,255))
for i in range(3): dk.rectangle([488+i*16,55,498+i*16,63],fill=(255,215,140,255))
for x in range(60,760,70): dk.line([x,172,x+40,172],fill=(240,230,200,200),width=2)  # deck markings
# parked jets on deck
def jetmask(sz):
    m=Image.new("L",sz); d=ImageDraw.Draw(m); w,h=sz
    d.polygon([(0,h*0.55),(w*0.92,h*0.45),(w,h*0.55),(w*0.92,h*0.62),(0,h*0.62)])
    d.polygon([(w*0.35,h*0.55),(w*0.6,h*0.55),(w*0.3,h),(w*0.18,h)])
    d.polygon([(w*0.02,h*0.55),(w*0.14,h*0.55),(w*0.05,0),(0,0)])
    d.ellipse([w*0.62,h*0.35,w*0.8,h*0.52]); return m
pj=paint_local(jetmask((70,30)),(150,155,170),(100,105,120),50,0,0.8,0.1)
for i in range(6): hull.alpha_composite(pj,(70+i*62,140))
carrier=Layer(hull,0.6,sdist=10,blur=8,op=0.55)
# wake: white paper chevrons
wk=Image.new("L",(520,120)); wd=ImageDraw.Draw(wk)
rr=np.random.default_rng(4)
for k in range(40):
    x=500-k*12-rr.random()*10; spread=6+k*1.4; y=60+(rr.random()-0.5)*spread*1.6; r=14-k*0.25
    wd.ellipse([x-r*1.8,y-r*0.6,x+r*1.8,y+r*0.6])
wake=Layer(paint_local(wk,(255,250,240),(220,215,230),60,0,1.0,0.08),0.62,sdist=3,blur=3,op=0.3)
# launch jet + smoke puff sprites
lj=paint_local(jetmask((130,52)),(235,238,245),(170,175,190),70,0,0.8,0.1); ljL=Layer(lj,0.64,sdist=14,blur=6,op=0.4)
pm=Image.new("L",(60,60)); ImageDraw.Draw(pm).ellipse([4,4,56,56]); puff=paint_local(pm,(255,245,235),(210,200,215),80,0,2.5,0.2)
# helicopter
hm=Image.new("L",(150,70)); d=ImageDraw.Draw(hm); d.ellipse([0,25,60,62]); d.polygon([(50,38),(140,36),(150,26),(150,46),(50,52)]); d.rectangle([22,62,40,66]); d.line([5,68,60,68],fill=255,width=3); d.rectangle([26,16,32,26])
heli=paint_local(hm,(60,62,80),(35,38,55),90,0,0.8,0.1)
def items(t):
    L=[(sky,0,0),(sky,CW-4,0),(sky,-CW+4,0),(sunL,0,0),(sun,0,0),(cl1,t*6,0),(cl2,t*10,0),(h1,0,0),(h2,0,0),(sea,0,0),(reflL,0,3*math.sin(t*2)),
       (escL2,PAD+1020+t*4,HZ-14),(escL1,PAD+60+t*5,HZ-20),(waves[0],0,2*math.sin(t*1.7))]
    bob=5*math.sin(t*1.2); sx=PAD+120+t*16; sy=HZ-45+bob
    L+=[(wake,sx-420,sy+178),(carrier,sx,sy)]
    # launching jet cycle
    lt=max(0.0,t-LAUNCH)
    if lt<1.3: jx=sx+420+lt*lt*150; jy=sy+128
    else: k=lt-1.3; jx=sx+420+1.69*150+k*95; jy=sy+128-k*k*28-k*60
    for i in range(10):
        if lt<1.3 and i>3: break
        pass
    L+=[(waves[1],0,4*math.sin(t*1.5+1))]
    # helicopter
    hx=PAD+t*80-80; hy=PAD+120+12*math.sin(t*1.8)
    hl=Image.new("RGBA",(260,120)); hl.alpha_composite(heli.rotate(4*math.sin(t*1.8),resample=Image.BICUBIC,expand=False),(50,40))
    bl=int(80*abs(math.cos(t*35))); ImageDraw.Draw(hl).line([80-bl,50,80+bl,50],fill=(30,30,40,170),width=4)
    L+=[(Layer(hl,0.7,sdist=16,blur=8,op=0.25),hx,hy)]
    jl=Layer(lj.rotate(min(lt-1.3,1.2)*14 if lt>1.3 else 0,resample=Image.BICUBIC,expand=True),0.64,sdist=12+int(max(0,lt-1.3)*25),blur=6,op=0.35)
    # smoke/steam puffs trailing
    for i in range(8):
        age=i*0.12; pt=lt-age
        if pt<0: continue
        if pt<1.3: px=sx+420+pt*pt*150; py=sy+130
        else: k=pt-1.3; px=sx+420+1.69*150+k*95; py=sy+130-k*k*28-k*60
        s=0.4+i*0.15
        p=puff.resize((int(60*s),int(60*s))); p.putalpha(p.getchannel("A").point(lambda v:int(v*(1-i/9))))
        L.append((Layer(p,0.63,shadow=False),px-10-p.width/2,py+10))
    L+=[(jl,jx,jy)]
    L+=[(waves[2],0,5*math.sin(t*1.3+2)),(waves[3],0,7*math.sin(t*1.1+3))]
    return L,dict(sx=sx,sy=sy,jx=jx+65,jy=jy+26)
BGI=screen_grad((55,40,95),(200,120,110))
def frame(t):
    L,k=items(t)
    if t<3.5:
        u=t/3.5
        cam=Cam(k["sx"]+430,k["sy"]+150,zoom=mix(1.0,1.25,ease(u)),fd=0.6,panx=mix(-0.18,0.18,u),pany=0.02,dolly=0.8,dof=0.5)
    elif t<6.6:
        u=(t-3.5)/3.1
        cam=Cam(k["sx"]+600,k["sy"]+150,zoom=mix(2.3,2.8,ease(u)),fd=0.62,panx=mix(0.12,-0.12,u),pany=mix(0.06,0.02,u),dolly=1.2,dof=1.3)
    else:
        u=(t-6.6)/3.0
        cam=Cam(mix(k["jx"]-80,k["jx"],ease(u)),k["jy"]+20,zoom=mix(2.0,1.4,ease(u)),fd=0.64,panx=mix(0.0,0.22,u),pany=mix(0.0,-0.12,u),dolly=1.0,dof=1.0)
    return finish(shoot(L,cam,bgimg=BGI),t)
if __name__=="__main__":
    out=sys.argv[1]; secs=float(sys.argv[2]) if len(sys.argv)>2 else 7
    p=subprocess.Popen(["ffmpeg","-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r","30","-i","-","-c:v","libx264","-crf","19","-pix_fmt","yuv420p",out+".mp4"],stdin=subprocess.PIPE)
    for f in range(int(secs*30)):
        im=frame(f/30); p.stdin.write(np.asarray(im).tobytes())
        if f==75: im.save(out+".jpg",quality=92)
    p.stdin.close(); p.wait()
