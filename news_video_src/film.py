import sys, json, math, subprocess, gc, numpy as np
sys.path.insert(0,sys.path[0])
from kit import *
import scene_carrier as SCR
NEWS=sys.argv[1] if len(sys.argv)>1 else "../news"
tl=json.load(open(NEWS+"/timeline.json")); DUR=tl["dur"]; FPS=30; NF=int(DUR*FPS)
segs={s["key"]:s for s in tl["segs"]}; caps=tl["caps"]
# scene schedule (global seconds)
CARRIER_T=20.4
SCHED=[("intro",0,segs["iran"]["start"]),("radar",segs["iran"]["start"],CARRIER_T),("carrier",CARRIER_T,segs["dc"]["start"]),
       ("dc",segs["dc"]["start"],segs["asia"]["start"]),("asia",segs["asia"]["start"],segs["outage"]["start"]),
       ("outage",segs["outage"]["start"],segs["korea"]["start"]),("exam",segs["korea"]["start"],segs["bond"]["start"]),
       ("film",segs["bond"]["start"],segs["outro"]["start"]),("outro",segs["outro"]["start"],DUR)]
_cache={}
def scene(name):
    if name in _cache: return _cache[name]
    for k in list(_cache): del _cache[k]
    import cutout; cutout._bake.clear(); gc.collect()
    if name in ("intro","outro"):
        from sc_city import City; o=City() if name=="intro" else City("STAY TUNED","MORE AS THESE STORIES DEVELOP")
    elif name=="radar": from sc_city import Radar; o=Radar()
    elif name=="carrier": o="carrier"
    elif name=="dc": from sc_dc import DC; o=DC()
    elif name=="asia": from sc_dc import Asia; o=Asia()
    elif name=="outage": from sc_more import Outage; o=Outage()
    elif name=="exam": from sc_more import Exam; o=Exam()
    elif name=="film": from sc_more import Film; o=Film()
    _cache[name]=o; return o
def carrier_frame(t,dur):
    SCR.LAUNCH=2.6
    L,k=SCR.items(t)
    if t<2.4:
        u=t/2.4; cam=Cam(k["sx"]+430,k["sy"]+150,zoom=mix(1.0,1.2,ease(u)),fd=0.6,panx=mix(-0.16,0.16,u),pany=0.02,dolly=0.8,dof=0.5)
    elif t<5.0:
        u=(t-2.4)/2.6; cam=Cam(k["sx"]+600,k["sy"]+150,zoom=mix(2.3,2.7,ease(u)),fd=0.62,panx=mix(0.12,-0.12,u),pany=mix(0.06,0.02,u),dolly=1.2,dof=1.3)
    else:
        u=(t-5.0)/(dur-5.0); cam=Cam(mix(k["jx"]-80,k["jx"],ease(u)),k["jy"]+20,zoom=mix(2.0,1.5,ease(u)),fd=0.64,panx=mix(0.0,0.2,u),pany=mix(0.0,-0.1,u),dolly=1.0,dof=1.0)
    return shoot(L,cam,bgimg=SCR.BGI)
def scene_frame(t):
    for name,a,b in SCHED:
        if a<=t<b or name=="outro":
            lt=t-a; d=b-a; o=scene(name)
            if name=="intro": return o.frame(lt,d,"intro")
            if name=="outro": return o.frame(lt,d,"outro")
            if name=="carrier": return carrier_frame(lt,d)
            return o.frame(lt,d)
# ---------------- paper overlays ----------------
def strip(w,h,top,bot,seed,r=1.4):
    return sprimg(lambda d:d.rectangle([2,2,w-2,h-2]),(w,h),top,bot,seed,1,r,0.12)
def shadowed(img,dx=5,dy=6,blur=5,op=0.5):
    return with_shadow(img,blur,op,dx,dy)
BUG_RED=strip(150,40,(225,45,55),(185,25,40),1001)
dd=ImageDraw.Draw(BUG_RED); dd.text((75,21),"NEWS 24",font=font(24),fill=(255,250,240,255),anchor="mm"); BUG_RED=shadowed(BUG_RED)
BUG_LIVE=strip(92,32,(250,245,232),(230,222,205),1002); BUG_LIVE=shadowed(BUG_LIVE,3,4,3)
TICK_BG=strip(W+40,40,(250,245,232),(232,224,206),1003,1.0)
TICK_TAB=strip(160,46,(225,45,55),(180,25,40),1004); ImageDraw.Draw(TICK_TAB).text((80,24),"BREAKING",font=font(22),fill=(255,250,240,255),anchor="mm"); TICK_TAB=shadowed(TICK_TAB,4,0,4)
FT=font(21); ticker="     ✦     ".join(s["head"].upper() for s in tl["segs"][1:-1])+"     ✦     "
TW=FT.getlength(ticker)
FCAP=font(29)
def cap_strip(txt):
    w=int(FCAP.getlength(txt))+44
    s=strip(w,52,(30,28,38),(18,16,24),hash(txt)%1000+2000,1.2); s.putalpha(s.getchannel("A").point(lambda v:int(v*0.88)))
    ImageDraw.Draw(s).text((w/2,26),txt,font=FCAP,fill=(255,255,255,255),anchor="mm"); return shadowed(s,3,4,4,0.4)
_cs={}
FL3H=font(36); FL3T=font(20)
def l3_imgs(seg):
    tag=strip(int(FL3T.getlength(seg["tag"]))+34,38,(225,45,55),(180,25,40),3000+len(seg["tag"]))
    ImageDraw.Draw(tag).text((tag.width/2,20),seg["tag"],font=FL3T,fill=(255,250,240,255),anchor="mm")
    hw=int(FL3H.getlength(seg["head"]))+50
    head=strip(hw,62,(250,245,232),(232,224,206),3100+len(seg["head"]))
    ImageDraw.Draw(head).text((25,32),seg["head"],font=FL3H,fill=(30,28,40,255),anchor="lm")
    return shadowed(tag,4,5,4),shadowed(head,5,6,6)
_l3={}
TEAR=None
def tear_img():
    global TEAR
    if TEAR is None:
        w=int(W*1.25); m=Image.new("L",(w,H)); d=ImageDraw.Draw(m)
        rr=np.random.default_rng(5); pts=[(60+rr.uniform(-25,25),y) for y in range(0,H+20,20)]
        pts2=[(w-60+rr.uniform(-25,25),y) for y in range(H+20,-20,-20)]
        d.polygon(pts+pts2)
        TEAR=paint_local(m,(250,244,228),(232,222,200),4000,0,2.0,0.14)
        dd=ImageDraw.Draw(TEAR); dd.rectangle([w*0.3,0,w*0.3+26,H],fill=(215,40,50,235))
        TEAR=with_shadow(TEAR,10,0.5,14,0)
    return TEAR
def overlay(fr,t):
    fr=fr.convert("RGBA")
    # transitions: paper sheet sweeps across each scene cut
    for name,a,b in SCHED[1:]:
        u=(t-(a-0.4))/0.8
        if 0<=u<=1:
            tr=tear_img(); x=int(mix(-tr.width,W+40,ease(u)*0.5+u*0.5)); fr.alpha_composite(tr,(x,0) if x>=0 else (0,0),(0,0,tr.width,tr.height) if x>=0 else (-x,0,tr.width,tr.height))
    # bug
    fr.alpha_composite(BUG_RED,(18,14)); fr.alpha_composite(BUG_LIVE,(172,18))
    d=ImageDraw.Draw(fr); pa=int(150+105*math.sin(t*5)); d.ellipse([184,29,196,41],fill=(230,30,40,pa)); d.text((204,35),"LIVE",font=font(18),fill=(40,30,40,255),anchor="lm")
    # lower third
    for s in tl["segs"][1:-1]:
        a=s["start"]+0.9; b=s["end"]-0.7
        if a<=t<b:
            if s["key"] not in _l3: _l3[s["key"]]=l3_imgs(s)
            tag,head=_l3[s["key"]]; ui=ease((t-a)/0.5); uo=ease((t-(b-0.4))/0.4)
            x=int(-head.width*(1-ui)-head.width*uo)+30
            fr.alpha_composite(head,(max(-head.width+1,x),510)) if x>-head.width else None
            if x+6>-tag.width: fr.alpha_composite(tag,(x+6,478))
    # captions
    for c0,c1,txt in caps:
        if c0<=t<c1:
            if txt not in _cs: _cs[txt]=cap_strip(txt)
            im=_cs[txt]; fr.alpha_composite(im,(int(W/2-im.width/2),606)); break
    # ticker
    fr.alpha_composite(TICK_BG,(-20,682))
    tk=Image.new("RGBA",(W,40)); td=ImageDraw.Draw(tk); off=(t*95)%TW
    for k in range(3): td.text((-off+k*TW,20),ticker,font=FT,fill=(30,28,40,255),anchor="lm")
    fr.alpha_composite(tk,(0,682)); fr.alpha_composite(TICK_TAB,(-6,676))
    # fade in/out
    out=fr.convert("RGB"); f=min(1,t/0.4,(DUR-t)/1.0)
    if f<1: out=Image.fromarray((np.asarray(out)*max(f,0)).astype(np.uint8))
    return out
def render_range(f0,f1,path):
    p=subprocess.Popen(["ffmpeg","-y","-loglevel","error","-f","rawvideo","-pix_fmt","rgb24","-s",f"{W}x{H}","-r",str(FPS),"-i","-","-c:v","libx264","-preset","medium","-crf","19","-pix_fmt","yuv420p",path],stdin=subprocess.PIPE)
    for f in range(f0,f1):
        t=f/FPS; im=finish(scene_frame(t),t); im=overlay(im,t)
        p.stdin.write(np.asarray(im).tobytes())
        if f%150==0: print(path,f,f1,flush=True)
    p.stdin.close(); p.wait()
if __name__=="__main__":
    mode=sys.argv[2]
    if mode=="sample":
        ts=[float(x) for x in sys.argv[3].split(",")]
        ims=[overlay(finish(scene_frame(t),t),t) for t in ts]
        n=len(ims); s=Image.new("RGB",(640*min(n,4),360*((n+3)//4)))
        for i,im in enumerate(ims): s.paste(im.resize((640,360)),((i%4)*640,(i//4)*360))
        s.save(sys.argv[4])
    else:
        f0,f1=int(sys.argv[3]),int(sys.argv[4]); render_range(f0,f1,sys.argv[5])
