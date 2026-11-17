from kit import *
X0,Y0=PAD,PAD   # stage origin
class City:
    """night skyline, broadcast tower with signal rings, hanging paper sign"""
    def __init__(s,text="BREAKING NEWS",sub="TUESDAY · NOVEMBER 17, 2026"):
        s.sky=full(lambda d:d.rectangle([0,0,CW,CH]),(14,18,48),(70,45,95),201,0.05,shadow=False,rough_amt=0,mottle=0.06)
        s.stars=stars_layer(260,3,0.06,Y0+520)
        s.moon=full(lambda d:d.ellipse([X0+180,Y0+60,X0+270,Y0+150]),(255,250,225),(235,225,190),202,0.08,shadow=False)
        s.cl=full(lambda d:[cloud_shape(d,X0+x,Y0+y,sc) for x,y,sc in ((-400,140,1.1),(300,90,0.9),(900,170,1.2),(1600,110,1.0),(2100,180,0.9))],(90,80,130),(60,50,100),203,0.14,op=0.3)
        def bld(d,base,seed,hmin,hmax,wmin,wmax):
            rr=np.random.default_rng(seed); x=0
            while x<CW:
                w=rr.uniform(wmin,wmax); h=rr.uniform(hmin,hmax); d.rectangle([x,base-h,x+w,CH])
                if rr.random()<0.3: d.polygon([(x,base-h),(x+w/2,base-h-rr.uniform(20,60)),(x+w,base-h)])
                if rr.random()<0.2: d.rectangle([x+w/2-3,base-h-50,x+w/2+3,base-h])
                x+=w+rr.uniform(0,10)
        base=Y0+560
        s.b1=full(lambda d:bld(d,base-30,1,120,300,60,140),(55,50,95),(35,30,70),204,0.25,op=0.35)
        s.b2=full(lambda d:bld(d,base,2,80,220,70,160),(40,36,75),(25,22,50),205,0.4,op=0.4)
        s.b3=full(lambda d:bld(d,base+60,3,60,170,90,200),(28,26,55),(15,14,35),206,0.7,op=0.5)
        # windows as separate light layers with flicker groups
        rr=np.random.default_rng(9)
        s.win=[Image.new("RGBA",(CW,CH)) for _ in range(3)]
        for (L,lo,hi,yb) in ((s.b1,0,0,base-30),(s.b2,0,0,base),(s.b3,0,0,base+60)):
            pass
        def windows(layer,depthimg,n):
            a=np.asarray(layer.img.getchannel("A"))
            for _ in range(n):
                x=int(rr.uniform(0,CW-10)); y=int(rr.uniform(Y0+200,Y0+700))
                if a[y,x]>200 and a[y+10,x+8]>200 and a[y-6,x]>200:
                    g=int(rr.integers(0,3)); ImageDraw.Draw(s.win[g]).rectangle([x,y,x+7,y+9],fill=(255,215,130,230))
        windows(s.b1,None,1500); windows(s.b2,None,1500)
        s.winL=[Layer(w,0.4,shadow=False) for w in s.win]
        s.win3=Image.new("RGBA",(CW,CH)); a=np.asarray(s.b3.img.getchannel("A"))
        for _ in range(1500):
            x=int(rr.uniform(0,CW-14)); y=int(rr.uniform(Y0+300,Y0+760))
            if a[y,x]>200 and a[y+14,x+12]>200 and a[y-8,x]>200: ImageDraw.Draw(s.win3).rectangle([x,y,x+11,y+14],fill=(255,205,120,235))
        s.win3L=Layer(s.win3,0.7,shadow=False)
        # broadcast tower
        tx=X0+760; s.tx=tx; s.ty=Y0+150
        def tower(d):
            d.polygon([(tx-50,base-120),(tx+50,base-120),(tx+8,Y0+150),(tx-8,Y0+150)])
            d.rectangle([tx-3,Y0+80,tx+3,Y0+150]); d.ellipse([tx-22,Y0+230,tx+22,Y0+262])
        s.tower=full(tower,(180,70,80),(110,40,60),207,0.32,op=0.35,rough_amt=1.2)
        s.ring=glow(1,(255,255,255))
        s.red=glow(26,(255,60,60))
        # hanging paper sign
        s.text=text; s.sub=sub
        def sign(d): d.rectangle([0,0,760,150])
        sg=sprimg(sign,(760,150),(220,40,50),(170,25,40),210,2,1.5,0.18)
        detail(sg,lambda d:d.rectangle([0,150-36,760,150],fill=(245,238,220,255)))
        dd=ImageDraw.Draw(sg); f=font(84); dd.text((760,112),text,font=f,fill=(255,250,240,255),anchor="mm")
        dd.text((760,265),sub,font=font(30),fill=(40,30,40,255),anchor="mm")
        s.sign=sg
    def items(s,t,sign_t0=1.0):
        rings=Image.new("RGBA",(900,900)); d=ImageDraw.Draw(rings)
        for k in range(4):
            r=((t*0.6+k/4)%1)*420+10; a=int(200*(1-r/430))
            d.ellipse([450-r,450-r*0.55,450+r,450+r*0.55],outline=(255,220,200,a),width=4)
        L=tile(s.sky)+[(s.stars,0,0),(s.moon,0,0),(s.cl,t*8,0),(s.b1,0,0)]
        L+=[(s.winL[0] if True else None,0,0)]
        L+=[(Layer(rings,0.31,shadow=False,cache=False),s.tx-450,Y0+80-450)]
        L+=[(s.tower,0,0)]
        if (t*1.2)%1<0.5: L.append((Layer(s.red,0.33,shadow=False),s.tx-26,Y0+80-26))
        L+=[(s.b2,0,0)]
        for g in range(1,3):
            if (int(t*1.3+g*7)%5)!=0: L.append((s.winL[g],0,0))
        L+=[(s.b3,0,0),(s.win3L,0,0)]
        # sign drops in on strings with a damped swing
        u=t-sign_t0
        if u>0:
            drop=min(1,u/0.6); y=Y0+150-(1-ease(drop))*700; sw=8*math.exp(-u*1.2)*math.sin(u*5)
            sg=s.sign.rotate(sw,resample=Image.BICUBIC,expand=True)
            sx=X0+640
            strings=Image.new("RGBA",(800,900)); sd=ImageDraw.Draw(strings)
            sd.line([200,0,200+sw*-6,900],fill=(230,220,200,200),width=3); sd.line([600,0,600+sw*-6,900],fill=(230,220,200,200),width=3)
            L.append((Layer(strings,0.85,shadow=False,cache=False),sx-400,y-900+20))
            L.append((dyn(sg,0.85,sdist=18,blur=10,op=0.5),sx-sg.width/4,y-sg.height/4+75))
        return L
    def frame(s,t,dur,mode="intro"):
        L=s.items(t,3.3 if mode=="intro" else 0.4)
        if mode=="intro":
            if t<3.2:
                u=t/3.2; cam=Cam(s.tx,Y0+170,zoom=mix(2.6,2.2,ease(u)),fd=0.32,panx=mix(-0.1,0.1,u),dolly=1.0,dof=1.1)
            else:
                u=(t-3.2)/(dur-3.2); cam=Cam(mix(X0+700,X0+640,ease(u)),mix(Y0+300,Y0+330,u),zoom=mix(1.5,1.0,ease(u)),fd=0.85,panx=mix(0.15,-0.1,u),pany=0.03,dolly=0.9,dof=0.7)
        else:
            u=t/dur; cam=Cam(X0+640,Y0+330,zoom=mix(1.25,0.98,ease(u)),fd=0.85,panx=mix(-0.12,0.12,u),dolly=0.8,dof=0.7)
        return shoot(L,cam,bgimg=BG_NIGHT)
BG_NIGHT=screen_grad((14,18,48),(60,40,90))

class Radar:
    """desert air base at night: radome, rotating radar, beacons, searchlights, fire & smoke on horizon"""
    def __init__(s):
        s.sky=full(lambda d:d.rectangle([0,0,CW,CH]),(10,12,35),(80,45,70),301,0.05,shadow=False,rough_amt=0,mottle=0.06)
        s.stars=stars_layer(200,5,0.06,Y0+450)
        hz=Y0+470; s.hz=hz
        s.glow=Image.new("RGBA",(CW,CH)); gd=ImageDraw.Draw(s.glow)
        for r in range(380,20,-14): gd.ellipse([X0+250-r*1.7,hz-r*0.7,X0+250+r*1.7,hz+r*0.4],fill=(255,110,40,9))
        s.glowL=Layer(s.glow,0.12,shadow=False)
        s.dune1=full(lambda d:hills(d,hz,30,160,0.5),(120,75,70),(80,50,55),302,0.2,op=0.3)
        s.ground=full(lambda d:d.rectangle([0,hz+20,CW,CH]),(95,70,65),(45,35,40),303,0.35,ang=0)
        s.dune2=full(lambda d:hills(d,hz+250,40,220,2),(75,55,55),(35,28,32),304,0.8,op=0.5)
        # radome
        rx=X0+820; ry=hz+30; s.rx,s.ry=rx,ry
        s.radome=full(lambda d:[d.rectangle([rx-95,ry-30,rx+95,ry+25]),d.chord([rx-125,ry-155,rx+125,ry+95],180,360)],(235,232,240),(170,165,185),305,0.45,op=0.45,rough_amt=1.2)
        detail(s.radome.img,lambda d:[d.arc([rx-125+k*25,ry-155,rx+125-k*25,ry+95],180,360,fill=(180,175,195,255),width=2) for k in range(1,5)],1)
        # radar tower
        tx=X0+430; ty=hz+20; s.tx,s.ty=tx,ty
        def tw(d):
            d.polygon([(tx-28,ty+20),(tx+28,ty+20),(tx+11,ty-180),(tx-11,ty-180)]); d.rectangle([tx-24,ty-196,tx+24,ty-178])
        s.tower=full(tw,(110,110,125),(60,60,75),306,0.5,op=0.45,rough_amt=1.0)
        detail(s.tower.img,lambda d:[d.line([tx-25+k*3,ty+18-k*33,tx+25-k*3,ty-15-k*33],fill=(80,80,95,255),width=2) for k in range(6)],1)
        # fence (foreground, blurred in close shots)
        def fence(d):
            for x in range(0,CW,46): d.rectangle([x,hz+215,x+5,hz+330])
            d.rectangle([0,hz+225,CW,hz+229]); d.rectangle([0,hz+262,CW,hz+265]); d.rectangle([0,hz+300,CW,hz+303])
            for x in range(0,CW,23): d.line([x,hz+229,x+23,hz+300],fill=255,width=1); d.line([x+23,hz+229,x,hz+300],fill=255,width=1)
        s.fence=full(fence,(90,85,95),(50,45,55),307,0.9,op=0.4,rough_amt=0)
        # hangar building
        s.bld=full(lambda d:[d.rectangle([X0+1010,hz-50,X0+1260,hz+40]),d.chord([X0+1000,hz-110,X0+1270,hz+10],180,360)],(85,80,95),(50,45,60),308,0.42,op=0.4,rough_amt=1.2)
        detail(s.bld.img,lambda d:[d.rectangle([X0+1030+i*32,hz-20,X0+1048+i*32,hz-4],fill=(255,205,120,255)) for i in range(7)],1)
        s.red=glow(30,(255,40,40)); s.redc=glow(9,(255,120,120),power=1)
        s.beam=beam(1100,16,220,(220,230,255),70)
        s.puff=puff_img(311,((110,95,100),(70,60,70)))
        def fl(d,w,h):
            pts=[(w*0.5+ (w*0.5)*math.sin(math.pi*f)*(1-f)**0.6*(1 if side else -1), h*(1-f)) for side in (1,0) for f in ([i/12 for i in range(13)] if side else [1-i/12 for i in range(13)])]
            d.polygon(pts)
        s.flame=[sprimg(lambda d,k=k:fl(d,34+k*5,100+k*12),(34+k*5,102+k*12),(255,235,140),(255,80,20),320+k,2,0.6,0.1) for k in range(6)]
        s.fglow=glow(160,(255,120,40,140))
        def arr(d): d.rectangle([0,0,200,34])
        s.arr=sprimg(arr,(200,34),(170,175,190),(110,115,130),330,2,0.5)
        detail(s.arr,lambda d:[d.line([x,2,x,32],fill=(90,95,110,255),width=2) for x in range(10,200,16)])
        # patrol jeep
        def jeep(d):
            d.rectangle([0,20,120,52]); d.polygon([(70,20),(80,0),(118,0),(120,20)]); d.ellipse([12,40,40,68]); d.ellipse([82,40,110,68])
        s.jeep=sprimg(jeep,(122,70),(85,95,70),(50,55,40),340,2,0.8)
    def items(s,t):
        hz=s.hz; L=tile(s.sky)+[(s.stars,0,0),(s.glowL,0,0)]
        # smoke column
        for k in range(16):
            age=((t*0.18+k/16)%1); x=X0+250+math.sin(k*3+t*0.4)*25+age*160; y=hz-10-age*420; sc=0.6+age*2.6
            p=s.puff.resize((int(60*sc),int(60*sc))); L.append((Layer(fade(p,0.9*(1-age)),0.13,shadow=False,cache=False),x-p.width/2,y-p.height/2))
        L.append((Layer(fade(s.fglow,0.75+0.25*math.sin(t*9)),0.13,shadow=False,cache=False),X0+250-160,hz-120))
        for k in range(11):
            fl=s.flame[(k*7)%6]; h=0.6+0.5*abs(math.sin(t*(6+k%3)+k*1.9)); f=fl.resize((fl.width,max(4,int(fl.height*h))))
            L.append((dyn(f,0.14,shadow=False),X0+175+((k*37)%140),hz+16-f.height/2))
        L+=[(s.dune1,0,0)]
        # searchlights
        for bx,ph in ((X0+600,0),(X0+1100,1.7)):
            a=-90+32*math.sin(t*0.6+ph); b=s.beam.rotate(-a,resample=Image.BICUBIC,expand=True)
            L.append((Layer(b,0.3,shadow=False,cache=False),bx-b.width/2+math.cos(math.radians(a))*550,hz+10-b.height/2+math.sin(math.radians(a))*550))
        L+=[(s.ground,0,0),(s.bld,0,0),(s.radome,0,0),(s.tower,0,0)]
        a=t*2.0; c=math.cos(a); w=max(4,int(abs(c)*s.arr.width))
        ar=s.arr.resize((w,s.arr.height)) if c>0 else s.arr.resize((w,s.arr.height)).point(lambda v:v*0.7)
        if c<=0: ar.putalpha(s.arr.resize((w,s.arr.height)).getchannel("A"))
        L.append((dyn(ar,0.505,sdist=6),s.tx-w/4,s.ty-196-s.arr.height/2))
        on=(t*2.2)%1<0.5
        for bx,by in ((s.tx,s.ty-200),(s.rx,s.ry-158),(X0+1135,s.hz-115)):
            L.append((Layer(s.red if on else s.redc,0.52,shadow=False),bx-(30 if on else 9),by-(30 if on else 9)))
        jx=X0-200+((t*90)%1700)
        L.append((dyn(s.jeep,0.6,sdist=6),jx,hz+120))
        if on: L.append((Layer(glow(14,(80,140,255)),0.61,shadow=False),jx+90,hz+108))
        L+=[(s.fence,0,0),(s.dune2,0,0)]
        return L
    def frame(s,t,dur):
        L=s.items(t); hz=s.hz
        if t<3.6:
            u=t/3.6; cam=Cam(mix(X0+560,X0+640,u),hz+30,zoom=mix(1.05,1.3,ease(u)),fd=0.5,panx=mix(-0.15,0.15,u),dolly=0.9,dof=0.6)
        elif t<7.0:
            u=(t-3.6)/3.4; cam=Cam(s.tx+mix(10,-10,u),s.ty-150,zoom=mix(2.6,3.0,ease(u)),fd=0.5,panx=mix(0.1,-0.1,u),pany=0.04,dolly=1.2,dof=1.4)
        else:
            u=(t-7.0)/(dur-7.0); cam=Cam(mix(X0+380,X0+300,u),hz-120,zoom=mix(1.8,1.6,ease(u)),fd=0.13,panx=mix(-0.06,0.06,u),pany=-0.04,dolly=1.0,dof=1.0)
        return shoot(L,cam,bgimg=BG_RADAR)
BG_RADAR=screen_grad((10,12,35),(70,40,65))
