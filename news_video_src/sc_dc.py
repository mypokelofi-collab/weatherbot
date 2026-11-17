from kit import *
X0,Y0=PAD,PAD
class DC:
    def __init__(s):
        s.sky=full(lambda d:d.rectangle([0,0,CW,CH]),(25,35,85),(240,140,100),401,0.05,shadow=False,rough_amt=0,mottle=0.06)
        s.cl=full(lambda d:[cloud_shape(d,X0+x,Y0+y,sc) for x,y,sc in ((-500,120,1.2),(150,70,0.9),(700,150,1.1),(1300,60,1.0),(1900,140,1.2))],(170,110,150),(110,80,130),402,0.12,op=0.3)
        gy=Y0+520; s.gy=gy
        # distant obelisk + tree line
        s.mon=full(lambda d:d.polygon([(X0+1120,gy-40),(X0+1146,gy-40),(X0+1140,gy-330),(X0+1133,gy-350),(X0+1126,gy-330)]),(235,215,210),(170,150,160),403,0.15,op=0.3,rough_amt=1)
        def trees(d,base,seed,rmin,rmax):
            rr=np.random.default_rng(seed); x=-50
            while x<CW+50:
                r=rr.uniform(rmin,rmax); d.ellipse([x-r,base-r*1.6,x+r,base+r*0.2]); x+=r*1.1
            d.rectangle([0,base-10,CW,CH])
        s.trees1=full(lambda d:trees(d,gy-20,1,30,60),(55,70,90),(35,45,65),404,0.2,op=0.3)
        s.lawn=full(lambda d:d.rectangle([0,gy,CW,CH]),(70,110,80),(30,55,45),405,0.3,ang=0)
        # White House
        wx=X0+640; s.wx=wx; top=gy-230
        def wh(d):
            d.rectangle([wx-380,top+40,wx+380,gy+10])               # main block
            d.rectangle([wx-390,top+28,wx+390,top+44])               # cornice
            for x in range(int(wx-385),int(wx+385),14): d.rectangle([x,top+12,x+8,top+30])
            d.rectangle([wx-390,top+8,wx+390,top+14])
            d.polygon([(wx-130,top+20),(wx+130,top+20),(wx,top-45)])  # pediment
            d.rectangle([wx-140,top+20,wx+140,top+34])
            d.rectangle([wx-150,gy-6,wx+150,gy+14])                  # steps
        s.wh=full(wh,(250,245,240),(200,195,205),406,0.4,op=0.45,rough_amt=1.0,mottle=0.08)
        def whd(d):
            for i in range(6):
                x=wx-112+i*45; d.rectangle([x,top+38,x+14,gy-6],fill=(255,252,248,255)); d.rectangle([x-3,top+36,x+17,top+42],fill=(230,225,230,255))
                d.line([x+14,top+42,x+14,gy-6],fill=(190,185,200,255),width=3)
            for row in range(2):
                for i in range(12):
                    x=wx-360+i*62
                    if abs(x-wx)<150: continue
                    y=top+70+row*85
                    d.rectangle([x,y,x+24,y+42],fill=(255,210,130,255)); d.line([x+12,y,x+12,y+42],fill=(200,150,90,255),width=2)
                    d.polygon([(x-4,y-4),(x+28,y-4),(x+12,y-16)],fill=(225,220,228,255))
            for i in range(3): d.rectangle([wx-60+i*40,top+80,wx-40+i*40,top+150],fill=(255,215,140,255))
            d.rectangle([wx-26,gy-80,wx+26,gy-6],fill=(255,220,150,255))
        detail(s.wh.img,whd,1)
        s.flag=None
        s.bush=full(lambda d:trees(d,gy+70,7,26,44),(50,90,65),(25,50,40),407,0.55,op=0.45)
        def fence(d):
            for x in range(0,CW,26): d.rectangle([x,gy+150,x+6,gy+330]); d.polygon([(x-3,gy+150),(x+9,gy+150),(x+3,gy+132)])
            d.rectangle([0,gy+170,CW,gy+178]); d.rectangle([0,gy+300,CW,gy+310])
            for x in range(0,CW,240): d.rectangle([x-12,gy+120,x+18,gy+340]); d.ellipse([x-14,gy+105,x+20,gy+130])
        s.fence=full(fence,(40,40,48),(15,15,22),408,0.9,op=0.5,rough_amt=0)
        s.road=full(lambda d:d.rectangle([0,gy+110,CW,CH]),(70,68,80),(40,38,50),409,0.6,ang=0)
        def suv(d):
            d.rounded_rectangle([0,30,230,88],10); d.polygon([(30,30),(60,0),(185,0),(205,30)]); d.ellipse([25,70,70,115]); d.ellipse([160,70,205,115])
        s.suv=sprimg(suv,(232,116),(40,42,55),(20,22,30),410,2,0.6)
        detail(s.suv,lambda d:[d.rectangle([70,6,120,28],fill=(140,160,190,255)),d.rectangle([128,6,178,28],fill=(140,160,190,255)),d.rectangle([90,-2,140,6],fill=(60,60,70,255)),d.rectangle([0,52,230,58],fill=(230,230,235,255))])
        s.gr=glow(70,(255,40,40)); s.gb=glow(70,(50,110,255)); s.gs=glow(18,(255,255,255))
        s.agents=[person_img(110,420+i,pose=0) for i in range(4)]
        s.heli=heli_img()
        s.bulb=glow(30,(255,230,170,160))
    def items(s,t):
        gy=s.gy; L=tile(s.sky)+[(s.cl,t*7,0),(s.mon,0,0),(s.trees1,0,0),(s.lawn,0,0),(s.wh,0,0),(s.bush,0,0),(s.road,0,0)]
        # helicopter crossing high over roof
        hx=X0-300+t*95; hy=gy-420+14*math.sin(t*1.5)
        h=heli_frame(s.heli,t).rotate(-4+2*math.sin(t*1.5),resample=Image.BICUBIC,expand=True)
        L.append((dyn(h,0.45,sdist=40,blur=10,op=0.2),hx,hy))
        sp=Image.new("RGBA",(400,700)); ImageDraw.Draw(sp).polygon([(200,0),(60,700),(340,700)],fill=(255,250,220,22))
        L.append((Layer(sp.filter(ImageFilter.GaussianBlur(10)),0.45,shadow=False,cache=False),hx+80-200,hy+40))
        # SUVs with flashing light bars
        for i,(x,ph) in enumerate(((X0+180,0),(X0+880,0.5))):
            L.append((dyn(s.suv,0.65,sdist=8),x,gy+150))
            ph2=(t*3+ph)%1
            L.append((Layer(s.gr if ph2<0.5 else s.gb,0.66,shadow=False),x+115-70,gy+150-70))
            L.append((Layer(s.gs,0.66,shadow=False),x+115-18+(-15 if ph2<0.5 else 15),gy+150-18))
        for i,a in enumerate(s.agents):
            ax=X0+420+i*120+6*math.sin(t*0.5+i); L.append((dyn(a,0.7,sdist=8),ax,gy+170))
        # flash tint on fence when lights flash
        L.append((s.fence,0,0))
        return L
    def frame(s,t,dur):
        L=s.items(t); gy=s.gy
        if t<5:
            u=t/5; cam=Cam(s.wx,gy-90,zoom=mix(1.0,1.35,ease(u)),fd=0.4,panx=mix(-0.14,0.14,u),dolly=0.9,dof=0.6)
        elif t<9.5:
            u=(t-5)/4.5; cam=Cam(mix(X0+300,X0+340,u),gy+170,zoom=mix(2.3,2.6,ease(u)),fd=0.66,panx=mix(0.1,-0.1,u),pany=0.03,dolly=1.2,dof=1.3)
        elif t<13.5:
            u=(t-9.5)/4; hx=X0-300+t*95+120
            cam=Cam(hx,gy-330,zoom=mix(2.0,1.8,u),fd=0.45,panx=mix(-0.1,0.2,u),pany=-0.06,dolly=1.0,dof=1.0)
        else:
            u=(t-13.5)/(dur-13.5); cam=Cam(mix(X0+620,X0+640,u),gy+130,zoom=mix(2.4,1.6,ease(u)),fd=0.7,panx=mix(0.1,-0.06,u),pany=0.05,dolly=1.1,dof=1.2)
        return shoot(L,cam,bgimg=BG_DC)
BG_DC=screen_grad((25,35,85),(220,130,100))

class Asia:
    def __init__(s):
        s.sky=full(lambda d:d.rectangle([0,0,CW,CH]),(250,170,110),(255,215,150),501,0.05,shadow=False,rough_amt=0,mottle=0.06)
        s.sun=full(lambda d:d.ellipse([X0+880,Y0+190,X0+1010,Y0+320]),(255,250,220),(255,225,160),502,0.07,shadow=False)
        s.cl=full(lambda d:[cloud_shape(d,X0+x,Y0+y,sc) for x,y,sc in ((-400,110,1.2),(250,60,0.9),(800,120,1.0),(1400,50,1.2),(2000,130,1.0))],(255,235,215),(250,190,170),503,0.12,op=0.25)
        gy=Y0+500; s.gy=gy
        s.mt=full(lambda d:hills(d,gy-40,120,180,1.0),(150,120,160),(120,95,140),504,0.18,op=0.3)
        s.sea=full(lambda d:d.rectangle([0,gy-30,CW,CH]),(110,170,200),(70,130,170),505,0.22,ang=0)
        s.land=full(lambda d:d.rectangle([0,gy,CW,CH]),(140,170,90),(90,120,60),506,0.3,ang=0)
        def term(d): d.rectangle([X0+950,gy-90,X0+1350,gy+5]); d.polygon([(X0+930,gy-90),(X0+1370,gy-90),(X0+1330,gy-120),(X0+970,gy-120)]); d.rectangle([X0+1000,gy-200,X0+1030,gy-90]); d.rectangle([X0+985,gy-225,X0+1045,gy-200])
        s.term=full(term,(240,235,225),(190,185,180),507,0.32,op=0.4,rough_amt=1)
        detail(s.term.img,lambda d:[d.rectangle([X0+965+i*36,gy-70,X0+990+i*36,gy-30],fill=(120,170,200,255)) for i in range(10)],1)
        s.runway=full(lambda d:d.rectangle([0,gy+60,CW,gy+150]),(95,95,105),(70,70,80),508,0.45,ang=0,rough_amt=1)
        detail(s.runway.img,lambda d:[d.rectangle([x,gy+102,x+60,gy+108],fill=(240,240,230,255)) for x in range(0,CW,120)],1)
        s.carpet=full(lambda d:d.polygon([(X0+880,gy+150),(X0+940,gy+150),(X0+1000,gy+330),(X0+900,gy+330)]),(200,30,45),(150,20,35),509,0.62,op=0.4,rough_amt=1)
        s.front=full(lambda d:d.rectangle([0,gy+150,CW,CH]),(120,150,80),(70,100,50),510,0.6,ang=0)
        def plane(d):
            d.rounded_rectangle([0,40,520,100],30); d.polygon([(480,40),(560,60),(540,95),(500,100)])  # nose
            d.polygon([(0,48),(40,48),(-10,-40),(-40,-40)] if False else [(10,50),(75,50),(40,0),(4,0)])  # tail fin
            d.polygon([(200,80),(330,80),(250,150),(215,150)])      # wing
            d.rectangle([250,125,300,145]); d.ellipse([275,120,305,150])
        s.plane=sprimg(plane,(565,160),(250,250,252),(200,205,215),511,2,0.5,0.08)
        def pd(d):
            d.rectangle([20,74,520,84],fill=(40,90,180,255)); d.rectangle([20,86,520,90],fill=(200,40,50,255))
            for x in range(90,470,22): d.rounded_rectangle([x,55,x+11,66],4,fill=(60,70,90,255))
            d.polygon([(500,52),(530,58),(528,66),(500,66)],fill=(50,60,80,255))
            d.polygon([(10,50),(75,50),(40,0),(4,0)],fill=(40,90,180,255))
        s.plane=detail(s.plane.crop((0,0,s.plane.width,s.plane.height)),pd)
        s.gear=sprimg(lambda d:[d.rectangle([12,0,18,26]),d.ellipse([0,18,30,48])],(30,48),(40,40,45),(20,20,25),512,2,0.4)
        s.puff=puff_img(513,((250,250,250),(200,200,210)))
        def flagm(d,w,h,ph):
            pts=[(x, 6*math.sin(x/w*6-ph)*x/w) for x in range(0,w+1,6)]
            d.polygon([(x,y) for x,y in pts]+[(x,y+h) for x,y in pts[::-1]])
        s.flagm=flagm
        s.guard=[person_img(70,520+i,cap=True,col=((235,235,240),(190,190,200))) for i in range(8)]
        s.pole=sprimg(lambda d:[d.rectangle([0,6,6,320]),d.ellipse([-3,0,9,12])],(10,320),(220,220,225),(150,150,160),514,2,0)
    def plane_pos(s,t):
        gy=s.gy; td=5.0
        if t<td: u=t/td; x=mix(X0-700,X0+300,u); y=mix(Y0-40,gy-20,ease(min(1,u*1.05))); a=mix(-6,-2,u)
        else:
            u=min(1,(t-td)/6); x=X0+300+ (1-(1-u)**2)*620; y=gy-20; a=0
        return x,y,a
    def flag(s,t,k):
        w,h=110,70; m=Image.new("L",(w*2+4,h*2+40)); s.flagm(SDraw(ImageDraw.Draw(m),2),w,h,t*5+k)
        m=m.crop((0,0,w*2+4,h*2+30))
        img=paint_local(m,(40,80,190),(30,60,160),600+k,0,0,0.1)
        a=np.asarray(img).copy(); hh=a.shape[0]; ys=np.arange(hh)[:,None]
        top=np.argmax(np.asarray(m)>0,axis=0)[None,:]
        red=(ys>top+h)                                     # lower half red
        a[red[...,None].repeat(4,2)[...,:3].any(2)]=a[red[...,None].repeat(4,2)[...,:3].any(2)]
        a[...,0]=np.where(red,200,a[...,0]); a[...,1]=np.where(red,40,a[...,1]); a[...,2]=np.where(red,50,a[...,2])
        img=Image.fromarray(a,"RGBA"); d=ImageDraw.Draw(img)
        d.polygon([(0,6),(0,6+2*h),(90,6+h)],fill=(245,245,245,255)); d.ellipse([22,6+h-14,50,6+h+14],fill=(255,205,40,255))
        img.putalpha(m)
        return img
    def items(s,t):
        gy=s.gy; L=tile(s.sky)+[(s.sun,0,0),(s.cl,t*6,0),(s.mt,0,0),(s.sea,0,0),(s.land,0,0),(s.term,0,0)]
        for x in (X0-100,X0+120,X0+1500): L+=palm_layer(x,gy+5,220,t,0.34,30,x*0.01,False)
        L.append((s.runway,0,0))
        px,py,pa=s.plane_pos(t)
        p=s.plane.rotate(pa,resample=Image.BICUBIC,expand=True)
        if t>4.2:
            g=s.gear; p2=Image.new("RGBA",(p.width,p.height+40)); p2.alpha_composite(g,(int(220*2),int(140*2)) if False else (440,270)); p2.alpha_composite(g,(860,270)); p2.alpha_composite(p,(0,0)); p=p2
        L.append((dyn(p,0.47,sdist=30 if t<4.8 else 10,blur=10,op=0.3),px,py-80))
        # tire smoke on touchdown
        for k in range(8):
            age=(t-5.0-k*0.05)
            if 0<age<1.6:
                sc=0.5+age*1.6; pp=s.puff.resize((int(60*sc),int(60*sc)))
                L.append((Layer(fade(pp,0.85*(1-age/1.6)),0.48,shadow=False,cache=False),X0+300+260-age*40-k*12-pp.width/2,gy+60-pp.height/2-age*20))
        L.append((s.front,0,0))
        L.append((s.carpet,0,0))
        for i,gd in enumerate(s.guard): L.append((dyn(gd,0.63,sdist=6),X0+830-i*18 if i<4 else X0+990+(i-4)*18,gy+190+(i%4)*28))
        for k,x in enumerate((X0+700,X0+780,X0+1080)):
            L.append((dyn(s.pole,0.75,sdist=8),x,gy-20))
            L.append((dyn(s.flag(t,k),0.75,sdist=10,op=0.3),x+4,gy-14))
        for x in (X0-250,X0+1380): L+=palm_layer(x,gy+330,420,t,0.88,40,x*0.02,True)
        return L
    def frame(s,t,dur):
        L=s.items(t); gy=s.gy; px,py,pa=s.plane_pos(t)
        if t<4.4:
            u=t/4.4; cam=Cam(mix(X0+520,X0+600,u),gy-120,zoom=mix(1.0,1.2,ease(u)),fd=0.47,panx=mix(-0.12,0.12,u),dolly=0.8,dof=0.5)
        elif t<8.2:
            u=(t-4.4)/3.8; cam=Cam(px+300,gy+20,zoom=2.4,fd=0.47,panx=mix(0.25,-0.05,u),pany=0.02,dolly=1.2,dof=1.3)
        else:
            u=(t-8.2)/(dur-8.2); cam=Cam(mix(X0+760,X0+860,u),gy+60,zoom=mix(2.2,1.7,ease(u)),fd=0.75,panx=mix(-0.12,0.12,u),pany=0.04,dolly=1.1,dof=1.2)
        return shoot(L,cam,bgimg=BG_ASIA)
BG_ASIA=screen_grad((250,170,110),(255,215,150))
