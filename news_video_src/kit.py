import math, numpy as np
from cutout import *
from PIL import Image, ImageDraw, ImageFont, ImageFilter
FB="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"; FC="/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf"
def font(sz,cond=False): return ImageFont.truetype(FC if cond else FB,int(sz))
def _sc(v,k):
    if isinstance(v,(int,float)): return v*k
    if isinstance(v,tuple): return tuple(_sc(x,k) for x in v)
    if isinstance(v,list): return [_sc(x,k) for x in v]
    return v
class SDraw:
    """draw wrapper that scales all coordinates by k"""
    def __init__(s,d,k): s.d=d; s.k=k
    def __getattr__(s,name):
        f=getattr(s.d,name)
        def g(*a,**kw):
            a=list(a)
            if a: a[0]=_sc(a[0],s.k)
            if name=="rounded_rectangle" and len(a)>1: a[1]=a[1]*s.k
            if "radius" in kw: kw["radius"]*=s.k
            if "width" in kw: kw["width"]=max(1,int(kw["width"]*s.k))
            return f(*a,**kw)
        return g
def full(fn,top,bot,seed,depth,shadow=True,op=0.45,sdist=10,blur=9,rough_amt=2.5,ang=8,mottle=0.15):
    m=new_mask(); fn(ImageDraw.Draw(m))
    return Layer(paint(m,top,bot,seed,ang=ang,mottle=mottle,rough_amt=rough_amt),depth,shadow,sdist,blur,op)
def sprimg(fn,size,top,bot,seed,res=2,rough_amt=1.2,mottle=0.15):
    w,h=size; m=Image.new("L",(int(w*res),int(h*res))); fn(SDraw(ImageDraw.Draw(m),res))
    return paint_local(m,top,bot,seed,0,rough_amt*res if rough_amt else 0,mottle)
def detail(img,fn,res=2):
    fn(SDraw(ImageDraw.Draw(img),res)); return img
def dyn(img,depth,res=2,shadow=True,sdist=8,blur=5,op=0.4):
    return Layer(img,depth,shadow,sdist,blur,op,res=res,cache=False)
def stat(img,depth,res=2,shadow=True,sdist=8,blur=5,op=0.4):
    return Layer(img,depth,shadow,sdist,blur,op,res=res,cache=True)
def glow(r,col,res=1,power=2.0):
    n=int(2*r*res); yy,xx=np.mgrid[0:n,0:n]; d=np.sqrt((xx-n/2)**2+(yy-n/2)**2)/(n/2)
    a=np.clip(1-d,0,1)**power
    im=np.zeros((n,n,4),np.uint8); im[...,:3]=col[:3]; im[...,3]=(a*255*(col[3]/255 if len(col)>3 else 1)).astype(np.uint8)
    return Image.fromarray(im,"RGBA")
def beam(length,w0,w1,col,alpha=60):
    """light cone pointing right from (0, w1/2)"""
    im=Image.new("RGBA",(int(length),int(w1)+4)); d=ImageDraw.Draw(im)
    for i in range(12):
        f=1-i/12; a=int(alpha*(i+1)/12/1.5)
        d.polygon([(0,w1/2-w0/2*f),(length,w1/2-w1/2*f),(length,w1/2+w1/2*f),(0,w1/2+w0/2*f)],fill=col+(a,))
    return im.filter(ImageFilter.GaussianBlur(4))
def rot(img,deg,pivot=None):
    return img.rotate(deg,resample=Image.BICUBIC,expand=True)
def place_c(L,cx,cy):
    """item tuple placing a layer's image centred at world (cx,cy)"""
    return (L,cx-L.img.width/L.res/2,cy-L.img.height/L.res/2)
# ---------- common sprites ----------
def person(d,h=100,pose=0.0,cap=False,suit=False):
    w=h*0.36
    d.ellipse([w/2-h*0.09,0,w/2+h*0.09,h*0.19])
    if cap: d.rectangle([w/2-h*0.11,h*0.0,w/2+h*0.11,h*0.06])
    d.rounded_rectangle([w/2-w*0.42,h*0.2,w/2+w*0.42,h*0.62],h*0.07)
    lg=h*0.06*math.sin(pose)
    d.polygon([(w/2-w*0.36,h*0.58),(w/2-w*0.04,h*0.58),(w/2-w*0.06+lg,h),(w/2-w*0.3+lg,h)])
    d.polygon([(w/2+w*0.04,h*0.58),(w/2+w*0.36,h*0.58),(w/2+w*0.3-lg,h),(w/2+w*0.06-lg,h)])
def person_img(h,seed,col=((40,38,55),(25,22,35)),pose=0.0,cap=False,res=2):
    return sprimg(lambda d:person(d,h,pose,cap),(int(h*0.36)+2,h+2),col[0],col[1],seed,res,1.0)
def palm(d,h=300,lean=0):
    pts=[]
    for i in range(11):
        f=i/10; x=60+lean*f*f*60+math.sin(f*3)*6; y=h-f*h*0.8; pts.append((x,y))
    left=[(x-9+(1-y/h)*2,y) for x,y in pts]; right=[(x+9-(1-y/h)*2,y) for x,y in pts][::-1]
    d.polygon(left+right)
def frond(d,L=150,droop=0.6):
    pts_top=[];pts_bot=[]
    for i in range(13):
        f=i/12; x=f*L; y=droop*L*f*f*0.7; w=16*math.sin(f*math.pi)*(1-0.3*f)
        pts_top.append((x,y-w)); pts_bot.append((x,y+w*0.6))
    d.polygon(pts_top+pts_bot[::-1])
    for i in range(1,12):
        f=i/12; x=f*L; y=droop*L*f*f*0.7
        d.polygon([(x,y),(x+8,y-26*(1-f)),(x+12,y-22*(1-f))]); d.polygon([(x,y),(x+8,y+24*(1-f)),(x+12,y+20*(1-f))])
_fr={}; _rot={}; _pads={}
def palm_layer(x,y,h,t,depth,seed,ph=0.0,dark=False):
    """returns item list for palm with swaying fronds"""
    key=(h,seed,dark)
    if key not in _fr:
        tc=((70,50,60),(40,30,45)) if dark else ((150,110,80),(95,70,55))
        fc=((40,60,55),(20,35,35)) if dark else ((70,140,80),(35,90,60))
        trunk=sprimg(lambda d:palm(d,h),(120,h+4),tc[0],tc[1],seed,2,1.0)
        fr=sprimg(lambda d:frond(d,h*0.5),(int(h*0.5)+20,int(h*0.5*0.6)+40),fc[0],fc[1],seed+1,2,0.8)
        _fr[key]=(trunk,fr)
    trunk,fr=_fr[key]
    items=[(dyn(trunk,depth,sdist=8,op=0.35),x-60,y-h)]
    top=(x+math.sin(3)*6*0+ (60-60)+0, y-h*0.8)
    top=(x+ (math.sin(0.8*3)*6), y-h*0.8)
    R=max(fr.width,fr.height)
    if key not in _pads:
        p_=Image.new("RGBA",(2*R,2*R)); p_.alpha_composite(fr,(R,int(R-fr.height*0.30))); _pads[key]=p_
    pad=_pads[key]
    if False: pad=Image.new("RGBA",(2*R,2*R)); pad.alpha_composite(fr,(R,int(R-fr.height*0.30)))
    for k,ang in enumerate((-25,10,40,145,175,205,80,-60,235)):
        a=int(round((ang+6*math.sin(t*1.6+ph+k))/1.5)*1.5*10)/10
        ck=(key,a,depth)
        if ck not in _rot:
            im=pad.rotate(-a,resample=Image.BILINEAR,expand=False); bb=im.getchannel("A").getbbox()
            im=im.crop(bb); _rot[ck]=(Layer(im,depth+0.001,True,6,5,0.3,res=2,cache=True),bb[0],bb[1])
        Lr,ox,oy=_rot[ck]
        items.append((Lr,top[0]-R/2+ox/2,top[1]-R/2+oy/2))
    return items
def heli_img(res=2,col=((60,62,80),(35,38,55))):
    def f(d):
        d.ellipse([0,25,60,62]); d.polygon([(50,38),(140,36),(150,24),(152,46),(50,52)]); d.rectangle([22,62,40,66]); d.rectangle([2,67,62,70]); d.rectangle([26,16,32,26])
    return sprimg(f,(156,74),col[0],col[1],91,res,0.6)
def heli_frame(base,t,res=2):
    im=Image.new("RGBA",(base.width+80*res,base.height+20*res)); im.alpha_composite(base,(40*res,10*res))
    d=ImageDraw.Draw(im); bl=int(85*res*abs(math.cos(t*30))); cx=(40+29)*res; y=(10+15)*res
    d.line([cx-bl,y,cx+bl,y],fill=(25,25,35,190),width=3*res)
    d.ellipse([(40+148)*res-14*res*abs(math.sin(t*40)),(10+33)*res-14*res,(40+148)*res+14*res*abs(math.sin(t*40)),(10+33)*res+14*res],outline=(25,25,35,140),width=2*res)
    return im
def jet_mask(d,w,h):
    d.polygon([(0,h*0.55),(w*0.92,h*0.45),(w,h*0.55),(w*0.92,h*0.62),(0,h*0.62)])
    d.polygon([(w*0.35,h*0.55),(w*0.6,h*0.55),(w*0.3,h),(w*0.18,h)])
    d.polygon([(w*0.02,h*0.55),(w*0.14,h*0.55),(w*0.05,0),(0,0)])
    d.ellipse([w*0.62,h*0.35,w*0.8,h*0.52])
def puff_img(seed=80,col=((255,245,235),(210,200,215))):
    return sprimg(lambda d:d.ellipse([3,3,57,57]),(60,60),col[0],col[1],seed,1,2.5,0.2)
def fade(img,a):
    im=img.copy(); im.putalpha(im.getchannel("A").point(lambda v:int(v*a))); return im
def cloud_shape(d,x,y,s):
    for dx,dy,r in [(0,0,40),(45,-18,52),(100,-5,44),(140,8,32),(-40,10,30)]:
        d.ellipse([x+dx*s-r*s,y+dy*s-r*s,x+dx*s+r*s,y+dy*s+r*s])
    d.rectangle([x-60*s,y,x+170*s,y+30*s])
def hills(d,base,amp,f,ph):
    pts=[(0,CH)]+[(x,base-amp*(0.6+0.4*math.sin(x/f+ph))-amp*0.3*math.sin(x/(f*0.37)+ph*2)) for x in range(0,CW+10,10)]+[(CW,CH)]
    d.polygon(pts)
def stars_layer(n,seed,depth,ymax):
    rr=np.random.default_rng(seed); im=Image.new("RGBA",(CW,CH)); d=ImageDraw.Draw(im)
    for _ in range(n):
        x,y=rr.random()*CW,rr.random()*ymax; r=rr.random()*1.6+0.6
        d.ellipse([x-r,y-r,x+r,y+r],fill=(255,250,230,int(120+rr.random()*135)))
    return Layer(im,depth,shadow=False)
def tile(L,y=0,dx=0):
    return [(L,dx,y),(L,dx+CW-4,y),(L,dx-CW+4,y)]
