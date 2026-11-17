"""Painted paper-cutout engine: textured layers, rough edges, cast shadows, parallax camera."""
import math, numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageChops
W,H=1280,720; PAD=560; CW,CH=W+2*PAD,H+2*PAD
R=np.random.default_rng(11)
def noise(h,w,scale,seed=None):
    g=np.random.default_rng(seed) if seed is not None else R
    sh=(max(2,int(h/scale)),max(2,int(w/scale)))
    a=Image.fromarray((g.random(sh)*255).astype(np.uint8)).resize((w,h),Image.BICUBIC)
    return np.asarray(a).astype(np.float32)/255
def streaks(h,w,ang,seed=None):
    g=np.random.default_rng(seed) if seed is not None else R
    a=Image.fromarray((g.random((h//2,w//2))*255).astype(np.uint8)).resize((w,h))
    a=a.rotate(ang,resample=Image.BILINEAR,expand=False)
    a=a.filter(ImageFilter.BoxBlur(1)).resize((w//8,h//2)).resize((w,h),Image.BICUBIC).rotate(-ang,resample=Image.BILINEAR)
    return np.asarray(a).astype(np.float32)/255
_grain=None
def grain():
    global _grain
    if _grain is None: _grain=noise(CH,CW,1.2,5)*0.5+noise(CH,CW,6,6)*0.5
    return _grain
def rough(mask,amt=2.5,seed=None):
    """jagged hand-cut edge"""
    m=np.asarray(mask.filter(ImageFilter.GaussianBlur(amt))).astype(np.float32)/255
    n=noise(mask.size[1],mask.size[0],3,seed)
    m=(m+(n-0.5)*0.35)>0.5
    return Image.fromarray((m*255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.7))
def paint(mask,top,bot,seed=0,ang=8,mottle=0.18,edge=0.35,rough_amt=2.5,box=None):
    """mask: L image (full canvas). returns RGBA painted cutout"""
    m=rough(mask,rough_amt,seed) if rough_amt else mask
    w,h=m.size; bb=m.getbbox() or (0,0,w,h)
    y=np.zeros((h,1),np.float32); y0,y1=bb[1],max(bb[3],bb[1]+1)
    t=np.clip((np.arange(h)[:,None]-y0)/(y1-y0),0,1)
    col=np.array(top,np.float32)*(1-t[...,None])+np.array(bot,np.float32)*t[...,None]
    col=np.repeat(col,w,1)
    n1=noise(h,w,60,seed+1); s=streaks(h,w,ang,seed+2); g=grain()[:h,:w]
    shade=1+(n1-0.5)*mottle*2+(s-0.5)*0.22+(g-0.5)*0.10
    col*=shade[...,None]
    # darker rim inside the edge (paper thickness / paint pooling)
    ma=np.asarray(m).astype(np.float32)/255
    inner=np.asarray(m.filter(ImageFilter.GaussianBlur(5))).astype(np.float32)/255
    rim=np.clip((ma-inner)*2.2,0,1)
    col*=(1-rim*edge)[...,None]
    # thin light highlight on top edge
    up=np.roll(ma,4,0); hl=np.clip(ma-up,0,1)
    col+=hl[...,None]*60
    rgba=np.dstack([np.clip(col,0,255),ma*255]).astype(np.uint8)
    return Image.fromarray(rgba,"RGBA")
def shadow_of(img,blur=9,op=0.55):
    a=img.getchannel("A").filter(ImageFilter.GaussianBlur(blur)).point(lambda v:int(v*op))
    s=Image.new("RGBA",img.size,(10,8,20,0)); s.putalpha(a); return s
class Layer:
    def __init__(s,img,depth,shadow=True,sdist=10,blur=9,op=0.5,res=1,cache=None):
        s.res=res; s.img=img; s.depth=depth; s.sh=shadow_of(img,blur*res,op) if shadow else None; s.sdist=int(sdist*res); s.cache=cache
def new_mask(): return Image.new("L",(CW,CH),0)
def place(dst,img,x,y,rot=0,pivot=None,scale=1.0):
    """paste a sprite RGBA at (x,y) center with rotation/scale"""
    if scale!=1.0: img=img.resize((max(1,int(img.width*scale)),max(1,int(img.height*scale))),Image.BICUBIC)
    if rot: img=img.rotate(rot,resample=Image.BICUBIC,expand=True)
    dst.alpha_composite(img,(int(x-img.width/2),int(y-img.height/2)))
def sprite(fn,size,top,bot,seed=0,ang=8,rough_amt=1.5,mottle=0.15):
    """draw a sprite mask with fn(draw) in its own canvas, paint it"""
    m=Image.new("L",size,0); fn(ImageDraw.Draw(m))
    global CW,CH
    return paint_local(m,top,bot,seed,ang,rough_amt,mottle)
def paint_local(m,top,bot,seed,ang,rough_amt,mottle):
    w,h=m.size
    mm=rough(m,rough_amt,seed) if rough_amt else m
    ma=np.asarray(mm).astype(np.float32)/255
    bb=mm.getbbox() or (0,0,w,h); t=np.clip((np.arange(h)[:,None]-bb[1])/max(1,bb[3]-bb[1]),0,1)
    col=np.repeat(np.array(top,np.float32)*(1-t[...,None])+np.array(bot,np.float32)*t[...,None],w,1)
    n1=noise(h,w,max(8,min(w,h)/6),seed+1); g=noise(h,w,1.2,seed+3)
    col*=(1+(n1-0.5)*mottle*2+(g-0.5)*0.12)[...,None]
    inner=np.asarray(mm.filter(ImageFilter.GaussianBlur(3))).astype(np.float32)/255
    col*=(1-np.clip((ma-inner)*2,0,1)*0.35)[...,None]
    up=np.roll(ma,2,0); col+=np.clip(ma-up,0,1)[...,None]*50
    return Image.fromarray(np.dstack([np.clip(col,0,255),ma*255]).astype(np.uint8),"RGBA")
def with_shadow(spr,blur=5,op=0.5,dx=6,dy=8):
    s=shadow_of(spr,blur,op); out=Image.new("RGBA",(spr.width+dx+4*blur,spr.height+dy+4*blur))
    out.alpha_composite(s,(2*blur+dx,2*blur+dy)); out.alpha_composite(spr,(2*blur,2*blur)); return out
def composite(layers_dyn,cam,zoom=1.0):
    """layers_dyn: list of (Layer, extra_dx, extra_dy). cam: (x,y) camera offset in px"""
    fr=Image.new("RGBA",(CW,CH),(0,0,0,255))
    for L,dx,dy in layers_dyn:
        ox=int(-cam[0]*L.depth+dx); oy=int(-cam[1]*L.depth+dy)
        if L.sh is not None: fr.alpha_composite(L.sh,(0,0)) if False else fr.paste(L.sh,(ox+L.sdist,oy+L.sdist),L.sh)
        fr.paste(L.img,(ox,oy),L.img)
    cw,ch=W/zoom,H/zoom; x0=(CW-cw)/2; y0=(CH-ch)/2
    return fr.crop((int(x0),int(y0),int(x0+cw),int(y0+ch))).resize((W,H),Image.BICUBIC).convert("RGB")
_vig=None
def finish(im,t):
    """vignette + warm light + paper grain"""
    global _vig
    if _vig is None:
        yy,xx=np.mgrid[0:H,0:W]; r=np.sqrt(((xx-W/2)/W)**2+((yy-H/2)/H)**2)
        _vig=np.clip(1-(r-0.25)*0.9,0.45,1)[...,None]
    a=np.asarray(im).astype(np.float32)*_vig
    g=grain()[:H,:W][...,None]; a*=0.94+g*0.12
    return Image.fromarray(np.clip(a,0,255).astype(np.uint8))
# masks: shapes default to filled white
_D=ImageDraw.Draw
class _MaskDraw:
    def __init__(s,d): s._d=d
    def __getattr__(s,k):
        f=getattr(s._d,k)
        if k in ("polygon","rectangle","ellipse","rounded_rectangle","chord","pieslice","regular_polygon"):
            def g(*a,**kw):
                if "fill" not in kw and "outline" not in kw: kw["fill"]=255
                return f(*a,**kw)
            return g
        return f
def _Draw(im,mode=None):
    d=_D(im,mode)
    return _MaskDraw(d) if im.mode=="L" else d
ImageDraw.Draw=_Draw

# ---------- v2 camera: focal point, opposite parallax, dolly scale, depth of field ----------
_bake={}
def baked(L,blur=0):
    k=(id(L),blur)
    if k in _bake: return _bake[k]
    if L.sh is not None:
        d=L.sdist; b=Image.new("RGBA",(L.img.width+d,L.img.height+d)); b.alpha_composite(L.sh,(d,d)); b.alpha_composite(L.img,(0,0))
    else: b=L.img
    bb=b.getchannel("A").getbbox() or (0,0,1,1)
    b=b.crop((max(0,bb[0]-20),max(0,bb[1]-20),min(b.width,bb[2]+20),min(b.height,bb[3]+20))); o=(max(0,bb[0]-20),max(0,bb[1]-20))
    if blur: b=b.filter(ImageFilter.GaussianBlur(blur))
    if (L.cache if L.cache is not None else L.img.width*L.img.height>200000): _bake[k]=(b,o)
    return b,o
class Cam:
    def __init__(s,px,py,zoom=1.0,fd=0.5,panx=0.0,pany=0.0,dolly=0.0,dof=1.0,roll=0.0):
        s.px,s.py,s.zoom,s.fd,s.panx,s.pany,s.dolly,s.dof,s.roll=px,py,zoom,fd,panx,pany,dolly,dof,roll
def shoot(items,cam,bg=(20,18,30),bgimg=None):
    """items: (Layer, x, y) with image top-left at canvas (x,y). Focus point (px,py) maps to screen centre.
       Layers in front of focus depth fd shift opposite to layers behind it (panx/pany), scale by dolly, blur by distance."""
    fr=bgimg.copy() if bgimg is not None else Image.new("RGBA",(W,H),bg+(255,))
    for L,x,y in items:
        dd=L.depth-cam.fd
        s=cam.zoom*math.exp(dd*cam.dolly)
        shx=-cam.panx*dd*650; shy=-cam.pany*dd*650
        lv=abs(dd)*cam.dof*cam.zoom*9
        bl=0 if lv<1.2 else (2 if lv<3 else (4 if lv<6 else 7))
        im,o=baked(L,bl*L.res if bl else 0); r=L.res; x=x+o[0]/r; y=y+o[1]/r
        cx=W/2+shx; cy=H/2+shy
        a=r/s; c=r*(cam.px-x-cx/s); f=r*(cam.py-y-cy/s)
        # quick reject if layer is off screen
        x0=(x-cam.px)*s+cx; y0=(y-cam.py)*s+cy; x1=x0+im.width*s/r; y1=y0+im.height*s/r
        if x1<0 or y1<0 or x0>W or y0>H: continue
        bx0=max(0,int(x0)-1); by0=max(0,int(y0)-1); bx1=min(W,int(x1)+2); by1=min(H,int(y1)+2)
        if bx1-bx0<1 or by1-by0<1: continue
        t=im.transform((bx1-bx0,by1-by0),Image.AFFINE,(a,0,c+a*bx0,0,a,f+a*by0),resample=Image.BILINEAR)
        fr.alpha_composite(t,(bx0,by0))
    if cam.roll: fr=fr.rotate(cam.roll,resample=Image.BILINEAR,center=(W/2,H/2)).crop((0,0,W,H))
    return fr.convert("RGB")
def ease(x): x=min(max(x,0),1); return x*x*(3-2*x)
def mix(a,b,x): return a+(b-a)*x

def screen_grad(c1,c2):
    a=np.linspace(0,1,H)[:,None,None]
    return Image.fromarray(np.repeat((np.array(c1)*(1-a)+np.array(c2)*a),W,1).astype(np.uint8)).convert("RGBA")
